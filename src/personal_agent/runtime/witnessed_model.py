"""One witnessed model attempt through the locked ADK -> LiteLLM -> SDK path.

The Runner owner reserves the attempt, projects the request, and consumes the
receipt in after_model before authorizing a batch. No provider output is trusted
to assign its own request/attempt identity. This is not a retry/model loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import litellm
from contextlib import aclosing

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm
from openai import AsyncOpenAI
from pydantic import PrivateAttr

from personal_agent.runtime.a2_witness import A2Witness, A2Violation, body_images, verify
from personal_agent.runtime.glm_gateway import _thinking_request_params, _prompt_tokens
from personal_agent.runtime.model_providers import PROVIDERS, canonical_api_base
from personal_agent.runtime.response_witness import (
    AttemptBinding, ResponseViolation, ResponseWitnessTransport,
)


def _private_sdk_defaults():
    """This service never exports private model traffic to SDK telemetry.

    Process-wide, sticky defaults: restoring callbacks after an attempt would
    reopen a concurrent attempt's logging channel. Host audit is separate.
    """
    for name in ("callbacks", "input_callback", "success_callback", "failure_callback",
                 "_async_input_callback", "_async_success_callback", "_async_failure_callback",
                 "audit_log_callbacks", "pre_call_rules", "post_call_rules"):
        setattr(litellm, name, [])
    litellm.set_verbose = False
    litellm.suppress_debug_info = True
    litellm.turn_off_message_logging = True
    litellm.log_raw_request_response = False
    litellm.redact_messages_in_exceptions = True
    litellm.redact_user_api_key_info = True
    # ADK/OpenAI also have debug request loggers, outside LiteLLM callbacks.
    for name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router",
                 "google_adk.google.adk.models.lite_llm", "openai._base_client"):
        logging.getLogger(name).disabled = True


class WitnessedLiteLlm(BaseLlm):
    _input_budget: object = PrivateAttr(default=None)
    _binding: AttemptBinding = PrivateAttr()
    _key: str = PrivateAttr()
    _provider_name: str = PrivateAttr()
    _transport: object = PrivateAttr()
    _timeout: float = PrivateAttr()
    _images: tuple = PrivateAttr()
    _text_required: bool = PrivateAttr()
    _used: bool = PrivateAttr(default=False)
    _guard: object = PrivateAttr(default=None)
    _delivered: object = PrivateAttr(default=None)

    def __init__(self, *, model: str, provider_name: str, api_key: str,
                 binding: AttemptBinding, transport=None, timeout: float = 25,
                 expected_images: tuple = (), text_required: bool = False, input_budget=None):
        if provider_name not in PROVIDERS or not model.startswith("openai/"):
            raise ResponseViolation("unsupported_provider")
        if not 0 < timeout <= 25:
            raise ResponseViolation("invalid_timeout")
        super().__init__(model=model)
        self._provider_name = provider_name
        self._key = api_key
        self._input_budget = input_budget
        self._binding = binding
        self._transport = transport
        self._timeout = timeout
        self._images = expected_images
        self._text_required = text_required

    @property
    def binding(self):
        return self._binding

    async def _check_input_budget(self, request):
        if self._input_budget is None:
            return
        from personal_agent.runtime.response_witness import strict_json
        try:
            self._input_budget.check_request(strict_json(request.content),
                sum(i.token_upper_bound for i in self._images))
        except ValueError as exc:
            if self._guard is not None:
                self._guard.invalidate(str(exc))
            raise ResponseViolation(str(exc)) from None

    async def generate_content_async(self, llm_request, stream=False):
        if self._used or stream:
            raise ResponseViolation("attempt_reused_or_streaming")
        if llm_request.model not in (None, self.model):
            raise ResponseViolation("model_projection_mismatch")
        _private_sdk_defaults()
        self._used = True
        # Host projection admits only text and authorized inline images. Remote
        # file parts could cause SDK-side fetching outside the pinned transport.
        for content in llm_request.contents:
            for part in content.parts or []:
                if set(part.model_dump(exclude_none=True)) - {"text", "inline_data"}:
                    raise ResponseViolation("unsupported_input_part")
                if part.inline_data and part.inline_data.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise ResponseViolation("unsupported_input_media")
        provider = PROVIDERS[self._provider_name]
        base = canonical_api_base(provider)
        guard = ResponseWitnessTransport(binding=self._binding,
            endpoint=base + "chat/completions", transport=self._transport,
            expected_model=self.model.removeprefix("openai/"))
        self._guard = guard
        image_witness = A2Witness(pinned_host=provider.host) if self._images else None
        hooks = {"request": ([image_witness] if image_witness else []) + [self._check_outbound_images, self._check_input_budget]}
        client = httpx.AsyncClient(transport=guard, event_hooks=hooks,
            follow_redirects=False, trust_env=False, timeout=self._timeout)
        sdk = AsyncOpenAI(api_key=self._key, base_url=base, http_client=client, max_retries=0)
        try:
            model = LiteLlm(model=self.model, api_key="witnessed-client-only", api_base=base,
                client=sdk, timeout=self._timeout, num_retries=0,
                extra_body=_thinking_request_params(self.model, self._provider_name))
            async with asyncio.timeout(self._timeout):
                responses = []
                async with aclosing(model.generate_content_async(llm_request, stream=False)) as output:
                    async for response in output:
                        responses.append(response)
                        if len(responses) > 1:
                            raise ResponseViolation("sdk_response_count")
                if len(responses) != 1:
                    raise ResponseViolation("sdk_response_count")
                response = responses[0]
                # Recheck SDK conversion before delivery; callback consumes the
                # same receipt and rechecks after any intermediate mutation.
                guard.check(response, binding=self._binding)
                if image_witness:
                    verify(image_witness, expected_images=self._images,
                           prompt_tokens=_prompt_tokens(response), text_required=self._text_required)
                self._delivered = response
            yield response
        except asyncio.CancelledError:
            guard.invalidate("attempt_cancelled")
            raise
        except Exception as exc:
            code = (str(exc) if isinstance(exc, ResponseViolation) else
                    guard.failure or ("image_witness_failed" if isinstance(exc, A2Violation) else "model_provider_failed"))
            guard.invalidate(code)
            raise ResponseViolation(code) from None
        finally:
            self._delivered = None
            guard.invalidate("attempt_closed")
            await sdk.close()
            if image_witness:
                image_witness.attempts.clear()

    async def _check_outbound_images(self, request):
        sent = [(mime, hashlib.sha256(data).hexdigest()) for mime, data in body_images(request.content)]
        expected = [(p.mime_type, p.content_sha256) for p in self._images]
        if sent != expected:
            raise ResponseViolation("outbound_image_mismatch")

    def verify_response(self, response, *, binding: AttemptBinding):
        if response is not self._delivered or self._guard is None:
            if self._guard:
                self._guard.invalidate("response_replayed")
            raise ResponseViolation("response_replayed")
        return self._guard.consume(response, binding=binding)
