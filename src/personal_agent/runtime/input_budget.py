"""Optional v2 token budget, using pinned official offline DeepSeek data."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

TOKENIZER_SHA256 = '89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614'


@lru_cache(maxsize=2)
def _tokenizer(path):
    from tokenizers import Tokenizer
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError('adk_tokenizer_hash_mismatch')
    return Tokenizer.from_str(raw.decode())


@dataclass(frozen=True)
class InputBudget:
    tokenizer: object
    model: str
    limit: int = 200_000
    output_limit: int = 8192
    version: str = 'deepseek-v4-official-89085f12-v1'

    def estimate(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def total(self, text, image_tokens=0):
        # Text/schema tokenization is local; provider chat framing is not public.
        # Explicit 15% margin plus 1024 covers framing, not a claim of exact usage.
        return (self.estimate(text) * 115 + 99) // 100 + 1024 + image_tokens

    def excerpt(self, text, tokens):
        if self.estimate(text) <= tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.estimate(text[:mid]) <= tokens: lo = mid
            else: hi = mid - 1
        return text[:lo]

    def check_request(self, body, image_tokens=0):
        if body.get('model') != self.model:
            raise ValueError('input_budget_model_mismatch')
        outputs = [body[k] for k in ('max_tokens', 'max_completion_tokens') if k in body]
        if not outputs or any(type(v) is not int or not 0 < v <= self.output_limit for v in outputs):
            raise ValueError('input_budget_output_limit')
        # Image bytes are already witnessed separately; don't tokenize base64.
        texts = []
        frames = []
        for message in body.get('messages', []):
            frame = {k:v for k,v in message.items() if k != 'content'}
            content = message.get('content')
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if part.get('type') == 'text': texts.append(part.get('text', ''))
                    elif part.get('type') != 'image_url': frame.setdefault('parts', []).append(part)
            elif content is not None:
                raise ValueError('unsupported_input_content')
            frames.append(frame)
        # Message text reaches the tokenizer as text, not JSON-escaped a second time.
        structure = {**{k:v for k,v in body.items() if k != 'messages'}, 'messages': frames}
        text = '\n'.join(texts) + json.dumps(structure, ensure_ascii=False, separators=(',', ':'))
        count = self.total(text, image_tokens)
        if count > self.limit:
            raise ValueError('capacity_exceeded')
        return count


def input_budget_from_env(env=None):
    env = os.environ if env is None else env
    raw = env.get('ADK_INPUT_TOKEN_LIMIT')
    if raw is None:
        return None  # Existing deployments retain their old profile until enabled.
    limit = int(raw)
    window = int(env.get('MODEL_CONTEXT_TOKENS', '0'))
    model = env.get('MODEL_ID', 'deepseek-flash')
    if env.get('MODEL_PROVIDER') != 'deepseek' or model not in {'deepseek-flash', 'deepseek-v4-flash'}:
        raise ValueError('unsupported_input_budget_model')
    if not 1 <= limit <= 200_000 or window < limit + 8192:
        raise ValueError('invalid_input_budget_window')
    path = env.get('ADK_TOKENIZER_PATH')
    if not path:
        raise ValueError('adk_tokenizer_required')
    return InputBudget(_tokenizer(path), model, limit)
