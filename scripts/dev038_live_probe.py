"""DEV-038 live half: a throwaway device that drives the production API.

Runs ON THE MAC against `https://agent.example.invalid`. It exists because the
§13.2 items it closes cannot be closed offline by construction: they are claims
about what the *deployed* service refuses, and every offline counterparty is
written from the same assumptions as the code it is checking.

It never touches Henson's enrolled iPhone. Every run enrols its own device from
a fresh one-time code and the operator revokes it afterwards; the private key
lives only in this process's memory and is never written to disk.

Usage (the code comes from the ECS operator CLI, and is single-use):

    python scripts/dev038_live_probe.py --code <one-time-code> <probe>

The probes are deliberately separate subcommands rather than one script run:
each one wants a different device scope state, and interleaving them would make
it impossible to say which refusal came from which layer. Each probe performs
its own operator steps over SSH so that the interval between "mint a token" and
"revoke the device" stays seconds rather than however long a human takes -- with
a 10-minute token TTL, a slow manual step makes revocation and expiry
indistinguishable.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.auth.device_keys import (
    b64u_encode,
    build_signing_input,
    der_to_jose,
    encode_device_public_key,
)


BASE_URL = "https://agent.example.invalid"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

#: The operator half runs over SSH from this same process rather than as a
#: manual pause. Not for convenience: the interesting probes need an operator
#: action to happen *between* two client calls with a live token in hand, and a
#: human pause makes the interval unbounded, which turns "revocation took
#: effect" into "the token might simply have expired".
SSH = ["ssh", "-i", "~/.ssh/personal_agent_example_key", "deploy@192.0.2.10"]
OPERATOR = (
    "/opt/personal-agent/operator-cli.sh personal-agent-device "
    "--database /var/lib/personal-agent-api/agent.sqlite"
)


def operator(command: str) -> str:
    """Run one operator CLI command on the ECS and return its output."""
    argv = [part.replace("~", str(__import__("pathlib").Path.home())) for part in SSH]
    result = subprocess.run(
        [*argv, f"{OPERATOR} {command}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"operator command failed: {result.stderr.strip()}")
    return result.stdout


class ProbeDevice:
    """One throwaway device: a software P-256 key and a short-lived token."""

    def __init__(self, base_url: str = BASE_URL, pace_seconds: float = 11.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=TIMEOUT)
        # DEV-033 puts /v1/enrollments/claim and /v1/auth/ behind a 6r/m zone
        # with burst 3. A probe that ignores that measures Nginx, not the
        # service -- the first run of this script did exactly that and got a
        # 429 instead of an answer. Pacing here keeps every auth-zone request
        # inside the limit, so a refusal means the application refused.
        self._pace_seconds = pace_seconds
        self._last_auth_call = 0.0
        self._private = ec.generate_private_key(ec.SECP256R1())
        self.device_id: str | None = None
        self.token: str | None = None

    def close(self) -> None:
        self._client.close()

    # --- enrolment ----------------------------------------------------------

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_auth_call
        if self._last_auth_call and elapsed < self._pace_seconds:
            time.sleep(self._pace_seconds - elapsed)
        self._last_auth_call = time.monotonic()

    def enrol(self, code: str, display_name: str) -> dict[str, Any]:
        self._pace()
        response = self._client.post(
            "/v1/enrollments/claim",
            json={
                "code": code,
                "public_key": encode_device_public_key(self._private.public_key()),
                "display_name": display_name,
            },
        )
        response.raise_for_status()
        body = response.json()
        self.device_id = body["device_id"]
        return body

    def authenticate(self) -> str:
        """Challenge -> signature -> a 10-minute access token."""
        assert self.device_id is not None
        self._pace()
        challenge = self._client.post(
            "/v1/auth/challenges", json={"device_id": self.device_id}
        )
        challenge.raise_for_status()
        issued = challenge.json()
        signing_input = build_signing_input(
            challenge_id=issued["challenge_id"],
            nonce_b64u=issued["nonce"],
            device_id=self.device_id,
        )
        der = self._private.sign(signing_input, ec.ECDSA(_sha256()))
        self._pace()
        token = self._client.post(
            "/v1/auth/tokens",
            json={
                "challenge_id": issued["challenge_id"],
                "device_id": self.device_id,
                "signature": b64u_encode(der_to_jose(der)),
                "nonce": issued["nonce"],
            },
        )
        token.raise_for_status()
        self.token = token.json()["access_token"]
        return self.token

    def authenticate_with_audience(self, audience: str) -> httpx.Response:
        """Sign the challenge over a *different* audience and submit it.

        The signing input is rebuilt here rather than taken from
        `build_signing_input`, because the point is to produce the bytes a
        client for another service would produce.
        """
        assert self.device_id is not None
        self._pace()
        challenge = self._client.post(
            "/v1/auth/challenges", json={"device_id": self.device_id}
        )
        challenge.raise_for_status()
        issued = challenge.json()
        tampered = "\n".join(
            [
                "personal-agent-auth-v1",
                issued["challenge_id"],
                issued["nonce"],
                self.device_id,
                audience,
            ]
        ).encode("utf-8")
        der = self._private.sign(tampered, ec.ECDSA(_sha256()))
        self._pace()
        return self._client.post(
            "/v1/auth/tokens",
            json={
                "challenge_id": issued["challenge_id"],
                "device_id": self.device_id,
                "signature": b64u_encode(der_to_jose(der)),
                "nonce": issued["nonce"],
            },
        )

    # --- authenticated calls ------------------------------------------------

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def capabilities(self) -> httpx.Response:
        return self._client.get("/v1/capabilities", headers=self._headers())

    def canonical_conversation_id(self) -> str:
        """The server's Timeline id. `CAP-001`: the client never invents one.

        A locally generated UUID is refused with `TIMELINE_MISMATCH`, which is
        correct and was worth hitting once: it means a probe cannot accidentally
        write into a conversation that does not exist.
        """
        response = self.capabilities()
        response.raise_for_status()
        return response.json()["conversation_id"]

    def chat(
        self,
        text: str,
        *,
        conversation_id: str,
        idempotency_key: str,
    ) -> httpx.Response:
        return self._client.post(
            "/v1/chat/messages",
            headers=self._headers(idempotency_key),
            json={"conversation_id": conversation_id, "text": text},
        )

    def operation(self, operation_id: str) -> httpx.Response:
        return self._client.get(
            f"/v1/operations/{operation_id}", headers=self._headers()
        )


def _sha256():
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()


def _show(label: str, response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        body = None
    print(f"--- {label}: HTTP {response.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return body


# --- probes -------------------------------------------------------------------


def probe_enrol(device: ProbeDevice, args) -> int:
    """Enrol and read the effective catalog. Prints the device id to revoke."""
    claimed = device.enrol(args.code, args.display_name)
    print(f"device_id={claimed['device_id']}")
    device.authenticate()
    _show("capabilities", device.capabilities())
    return 0


def probe_scope_refusal(device: ProbeDevice, args) -> int:
    """A live refusal at the device-scope layer.

    The operator narrows this device's scopes to read-only first. The write is
    then refused by the bridge before anything is dispatched, so this is a
    production refusal at a layer only the allow path had ever exercised.
    """
    device.enrol(args.code, args.display_name)
    print(f"device_id={device.device_id}")
    # The token is minted BEFORE the narrowing, deliberately. If scopes were
    # baked into the token this write would still succeed; the refusal is
    # therefore evidence that the device row is re-read per call, which is the
    # property a short TTL is explicitly not relied upon to provide.
    device.authenticate()
    _show("capabilities before narrowing", device.capabilities())
    print("--- narrowing scopes to read-only")
    print(
        operator(
            f"set-scopes --device-id {device.device_id} "
            "--scope meta.capabilities.read --scope finance.expense.read"
        )
    )
    capabilities = _show("capabilities (narrowed device)", device.capabilities())
    if capabilities is not None:
        tools = capabilities.get("tools") or capabilities.get("effective_tools")
        print(f"effective tools: {tools}")
    response = device.chat(
        args.text,
        conversation_id=device.canonical_conversation_id(),
        idempotency_key=str(uuid.uuid4()),
    )
    _show("write attempt", response)
    return 0


def probe_replay(device: ProbeDevice, args) -> int:
    """The same `Idempotency-Key` twice; the second must add no ledger record.

    Whether it lands as a replay or a duplicate refusal, the assertion is the
    same and is checked in Feishu afterwards: the external record count does not
    move. This script prints both responses; the count is read separately.
    """
    device.enrol(args.code, args.display_name)
    print(f"device_id={device.device_id}")
    device.authenticate()
    key = str(uuid.uuid4())
    conversation_id = device.canonical_conversation_id()
    print(f"idempotency_key={key} conversation_id={conversation_id}")
    first = device.chat(
        args.text, conversation_id=conversation_id, idempotency_key=key
    )
    body = _show("first submission", first)
    if body is not None and body.get("state") == "accepted":
        body = _await_operation(device, body["operation_id"])
    if body is None or body.get("state") != "succeeded":
        print("first submission did not succeed; not replaying")
        return 1
    second = device.chat(
        args.text, conversation_id=conversation_id, idempotency_key=key
    )
    replayed = _show("replay with the same key", second)
    if replayed is not None and replayed.get("state") == "accepted":
        replayed = _await_operation(device, replayed["operation_id"])
    if replayed is not None and body is not None:
        same = replayed.get("record_id") == body.get("record_id")
        print(f"same record_id as the first submission: {same}")
    print("now count executions and receipts in Finance; they must not have moved")
    return 0


def _await_operation(device: ProbeDevice, operation_id: str, tries: int = 30):
    """Poll the durable operation until it leaves `accepted`.

    The operation id is the contract for a detached turn; a timeout on the HTTP
    call never means the write was cancelled, so the probe must resolve it here
    rather than treat 202 as an outcome.
    """
    for _ in range(tries):
        time.sleep(3)
        response = device.operation(operation_id)
        if response.status_code != 200:
            _show("operation poll", response)
            return None
        body = response.json()
        if body.get("state") != "accepted":
            _show("operation resolved", response)
            return body
    print("operation never resolved")
    return None


def probe_after_revocation(device: ProbeDevice, args) -> int:
    """Everything this device holds must stop working the moment it is revoked.

    The token is minted first and deliberately still inside its 10-minute
    lifetime when the operator revokes the device, so this measures revocation
    rather than expiry -- the two are indistinguishable if you wait.
    """
    device.enrol(args.code, args.display_name)
    print(f"device_id={device.device_id}")
    device.authenticate()
    _show("capabilities before revocation", device.capabilities())
    conversation_id = device.canonical_conversation_id()
    print("--- revoking")
    print(operator(f"revoke --device-id {device.device_id}"))
    _show("capabilities after revocation", device.capabilities())
    _show(
        "chat after revocation",
        device.chat(
            args.text,
            conversation_id=conversation_id,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    print("--- refreshing the token after revocation")
    try:
        device.authenticate()
    except httpx.HTTPStatusError as error:
        print(f"refresh refused: HTTP {error.response.status_code}")
        print(error.response.text)
    else:
        print("REFRESH SUCCEEDED -- this is a defect")
        return 1
    return 0


def probe_wrong_audience(device: ProbeDevice, args) -> int:
    """The audience is really bound into the signature, on the deployed service.

    A correct signature over the *right* five lines is minted first, so the
    device is known good; then the same challenge flow is signed over an input
    whose audience line was changed. Only the second must be refused, otherwise
    a signature captured for another service could be replayed at this one.
    """
    device.enrol(args.code, args.display_name)
    print(f"device_id={device.device_id}")
    device.authenticate()
    print("--- control: a correctly-audienced signature was accepted")

    refused = device.authenticate_with_audience("some-other-service")
    print(f"--- tampered audience: HTTP {refused.status_code}")
    print(refused.text)
    if refused.status_code == 200:
        print("TAMPERED AUDIENCE ACCEPTED -- this is a defect")
        return 1
    return 0


def _switch(action: str, reason: str) -> str:
    """Flip the deployed kill switch over SSH, as the operator would."""
    argv = [part.replace("~", str(__import__("pathlib").Path.home())) for part in SSH]
    command = (
        "sudo /opt/personal-agent/.venv/bin/personal-agent-write-switch "
        "--path /etc/personal-agent/write-switch.json "
        f"{action}"
    )
    if action != "status":
        command += f" --reason '{reason}'"
    result = subprocess.run(
        [*argv, command], capture_output=True, text=True, timeout=120
    )
    return f"exit={result.returncode} {result.stdout.strip()}{result.stderr.strip()}"


def probe_kill_switch(device: ProbeDevice, args) -> int:
    """DEV-039's live drill: writes off, reads alive, then writes back on.

    The same token is used throughout and is never refreshed, so the catalog
    changing across the flip is evidence that the switch is re-read per call
    rather than baked into anything.
    """
    device.enrol(args.code, args.display_name)
    print(f"device_id={device.device_id}")
    device.authenticate()

    print("--- switch state at the start")
    print(_switch("status", ""))

    def catalog() -> list[str]:
        response = device.capabilities()
        response.raise_for_status()
        return sorted(tool["alias"] for tool in response.json()["tools"])

    print(f"catalog with writes DISABLED: {catalog()}")
    conversation_id = device.canonical_conversation_id()
    _show(
        "write attempt with writes DISABLED",
        device.chat(
            args.text,
            conversation_id=conversation_id,
            idempotency_key=str(uuid.uuid4()),
        ),
    )

    print("--- enabling, without restarting anything")
    print(_switch("enable", "DEV-039 live drill"))
    print(f"catalog with writes ENABLED (same token): {catalog()}")
    _show(
        "write attempt with writes ENABLED",
        device.chat(
            args.text,
            conversation_id=conversation_id,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    return 0


# --- breakpoint-level restart drill (DEV-040 §13.2) --------------------------


FINANCE_DB = "/var/lib/personal-data-mcp/finance.sqlite"
AGENT_DB = "/var/lib/personal-agent-api/agent.sqlite"
FAULT_BREAKPOINT = (
    "sudo /opt/personal-agent/.venv/bin/personal-agent-fault-breakpoint "
    "--path /etc/personal-agent/fault-breakpoint.json"
)
#: The write-path states at which a live SIGKILL must leave the operation
#: consistent. `before_prepare` is deliberately excluded: a kill there produces
#: an Agent-side `needs_manual_review` (post-submit with no Finance execution --
#: the conservative classification, and the exact false alarm whose resolution
#: path does not exist yet), so it stays covered by the offline fault matrix.
BREAKPOINTS = ["prepared", "submitting", "committed_unverified"]


def _remote(command: str) -> str:
    """Run one command on the ECS and return stdout, raising on failure."""
    argv = [part.replace("~", str(__import__("pathlib").Path.home())) for part in SSH]
    result = subprocess.run(
        [*argv, command], capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"remote command failed: {result.stderr.strip()}"
        )
    return result.stdout


#: Every observation this drill makes is a *read*, so each one opens the database
#: read-only. Not a style choice: the drill deliberately kills the service, and a
#: root-owned read-write connection opened during that window can recover the WAL
#: and leave `-wal`/`-shm` owned by root -- which the service user then cannot
#: write, turning an observation into an outage.
_READ_ONLY_CONNECT = "sqlite3.connect('file:{path}?mode=ro', uri=True)"


def _sudo_python(script: str) -> str:
    """Run a short Python snippet on the ECS as root. Base64 avoids quote hell."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return _remote(
        f"echo {encoded} | base64 -d | sudo /opt/personal-agent/.venv/bin/python -"
    )


def _query(database: str, sql: str, parameter: str) -> str:
    """One read-only single-row query on an ECS database, as root."""
    script = (
        "import sqlite3;"
        f"c={_READ_ONLY_CONNECT.format(path=database)};"
        f"r=c.execute({sql!r},({parameter!r},)).fetchone();"
        "print('' if r is None else ' '.join(str(v) for v in r))"
    )
    return _sudo_python(script).strip()


def _finance_state(idempotency_key: str) -> str | None:
    return (
        _query(
            FINANCE_DB,
            "SELECT state FROM tool_executions WHERE idempotency_key=?",
            idempotency_key,
        )
        or None
    )


def _finance_counts(idempotency_key: str) -> tuple[int, int]:
    out = _query(
        FINANCE_DB,
        "SELECT (SELECT COUNT(*) FROM tool_executions WHERE idempotency_key=?1), "
        "(SELECT COUNT(*) FROM external_receipts WHERE idempotency_key=?1)",
        idempotency_key,
    )
    executions, receipts = out.split()
    return int(executions), int(receipts)


def _agent_operation_id(idempotency_key: str) -> str | None:
    # `operations` has no `id`: its primary key is `operation_id`.
    return (
        _query(
            AGENT_DB,
            "SELECT operation_id FROM operations WHERE idempotency_key=?",
            idempotency_key,
        )
        or None
    )


#: Long enough for two confirming reads plus the SSH round trip that carries the
#: kill, and short enough to stay well inside the Agent's 30s write-call budget
#: (`DEFAULT_WRITE_CALL_TIMEOUT`). An arm that outlives that budget converts the
#: intended crash into a transport timeout -- a different scenario, scored by the
#: same assertions, which is how a drill comes to certify something it never ran.
ARM_SECONDS = 10


def _arm_breakpoint(name: str, seconds: int = ARM_SECONDS) -> str:
    return _remote(
        f"{FAULT_BREAKPOINT} arm --breakpoint {name} --seconds {seconds} "
        "--reason 'DEV-040 breakpoint drill'"
    )


def _disarm_breakpoint() -> str:
    return _remote(f"{FAULT_BREAKPOINT} disarm --reason 'DEV-040 breakpoint drill'")


def _kill_finance() -> str:
    # `systemctl kill -s KILL` also signals auxiliary/control processes and
    # errors with "Failed to send signal SIGKILL to auxiliary processes: Invalid
    # argument" on this systemd, which the drill read as a kill failure (the
    # main process WAS killed, but the non-zero exit aborted the probe after the
    # pause and left the write to resume). Target the main PID directly instead.
    return _remote(
        "sudo kill -9 $(systemctl show -p MainPID --value personal-data-mcp)"
    )


def _authenticate(device: ProbeDevice) -> None:
    """Mint a fresh token, tolerating the ~12s of 502 the API serves while it
    restarts after a Finance kill (`Requires=` propagation). A 502 there is not
    an auth refusal; anything else still raises."""
    for attempt in range(6):
        try:
            device.authenticate()
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 502 and attempt < 5:
                time.sleep(5)
                continue
            raise


#: Finance's terminal execution states (`TERMINAL_EXECUTION_STATES`). Reaching
#: one while waiting for the pause means the write ran to completion, so the arm
#: never took effect and there is nothing left to kill.
TERMINAL_FINANCE_STATES = (
    "succeeded",
    "failed_safe",
    "needs_manual_review",
    "cancelled_pre_submit",
)


def _wait_paused(
    idempotency_key: str, target: str, timeout: float = 120.0
) -> bool:
    """Confirm the armed pause is holding the write at `target`.

    Two consecutive reads at the target state mean the write is genuinely stuck
    there (the pause), not just passing through. A state that has advanced past
    the breakpoint means the arm did not take effect, which the drill must fail
    loudly rather than misread.

    The timeout is deliberately NOT bounded by the arm: it covers the latency
    from firing the chat to the write reaching the breakpoint. The chat returns
    202 after the sync bound, and the model turn then continues detached before
    the tool call is dispatched -- measured on the deployed service, a write
    reached Finance ~63s after the chat was fired, long after the original 10s
    wait had given up (the write still landed `succeeded`). Misreading a resumed
    write is guarded against by the terminal-state check below, not by the
    timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = _finance_state(idempotency_key)
        except RuntimeError:
            state = None
        if state == target:
            time.sleep(0.3)
            try:
                if _finance_state(idempotency_key) == target:
                    return True
            except RuntimeError:
                pass
        if state in TERMINAL_FINANCE_STATES:
            return False
        time.sleep(0.3)
    return False


def _await_terminal(device: ProbeDevice, operation_id: str, tries: int = 60) -> dict | None:
    """Poll the public operation until it leaves `accepted`.

    Tolerates transient non-200s: the Agent API itself restarts after a Finance
    kill (Requires=), and a 502 during that window is not evidence.
    """
    for _ in range(tries):
        time.sleep(3)
        try:
            response = device.operation(operation_id)
        except Exception:
            continue
        if response.status_code != 200:
            continue
        body = response.json()
        if body.get("state") != "accepted":
            _show("operation resolved", response)
            return body
    return None


def probe_breakpoint_restart(device: ProbeDevice, args) -> int:
    """§13.2: SIGKILL the Finance MCP at each ordering-critical breakpoint.

    For each breakpoint: arm the pause, drive one real chat write to it, confirm
    the write is held at the target state, SIGKILL the process, disarm, and after
    the systemd restart + startup recovery assert the operation reaches
    `succeeded` with exactly one external record and one receipt.
    """
    device.enrol(args.code, args.display_name)
    print(f"device_id={device.device_id}")
    # The token must exist before any authenticated read. The per-breakpoint
    # re-authenticate below refreshes it; the one here mints the first.
    _authenticate(device)
    conversation_id = device.canonical_conversation_id()
    failures = 0
    for index, breakpoint in enumerate(BREAKPOINTS, start=1):
        key = str(uuid.uuid4())
        # The duplicate gate matches on stored values (date + amount + name +
        # category). A fixed text would collide with a previous drill run's row
        # on the same ledger day and park the write as `waiting_for_duplicate_decision`
        # instead of reaching the breakpoint -- measured on the deployed service.
        # Deriving the amount from the fresh key keeps every drill write unique.
        amount = 5 + (int(key[:8], 16) % 95)
        text = f"午饭演练{index} {amount} 个人"
        print(f"\n=== breakpoint {breakpoint} (idempotency_key={key}) ===")
        try:
            _authenticate(device)  # a fresh 10-minute token per breakpoint
            print(_arm_breakpoint(breakpoint).strip() or "(armed)")

            holder: list = []

            def fire():
                try:
                    holder.append(
                        device.chat(
                            text,
                            conversation_id=conversation_id,
                            idempotency_key=key,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - informational only
                    holder.append(exc)

            thread = threading.Thread(target=fire, daemon=True)
            thread.start()

            if not _wait_paused(key, breakpoint):
                print(f"FAIL: the write never paused at {breakpoint}")
                failures += 1
                continue
            print(f"confirmed the write is paused at {breakpoint}")
            print("--- SIGKILL personal-data-mcp")
            print(_kill_finance().strip() or "(killed)")
            print(_disarm_breakpoint().strip() or "(disarmed)")
            thread.join(timeout=45)

            operation_id = _agent_operation_id(key)
            print(f"operation_id={operation_id}")
            if operation_id is None:
                print("FAIL: no Agent operation for the idempotency key")
                failures += 1
                continue

            _authenticate(device)
            terminal = _await_terminal(device, operation_id)
            if terminal is None:
                print("FAIL: operation never reached a terminal state")
                failures += 1
                continue
            executions, receipts = _finance_counts(key)
            state = terminal.get("state")
            print(
                f"operation state={state} executions={executions} receipts={receipts}"
            )
            if state != "succeeded":
                print(f"FAIL: expected succeeded, got {state}")
                failures += 1
            elif executions != 1 or receipts != 1:
                print(
                    "FAIL: expected exactly one execution and one external receipt "
                    f"for {key}"
                )
                failures += 1
            else:
                print(f"PASS: {breakpoint} -> succeeded, exactly one external record")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {type(exc).__name__}: {exc}")
            failures += 1
        finally:
            # Arming is an acquisition and must be released on *every* exit path,
            # including Ctrl-C. A breakpoint left armed on production pauses every
            # matching write for the whole arm window, and nothing else in the
            # system will take it back down.
            try:
                _disarm_breakpoint()
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: could not disarm the breakpoint: {exc}")

    print("\n--- revoking the throwaway device")
    print(operator(f"revoke --device-id {device.device_id}"))
    print(f"failures={failures}")
    return 0 if failures == 0 else 1


PROBES = {
    "enrol": probe_enrol,
    "kill-switch": probe_kill_switch,
    "wrong-audience": probe_wrong_audience,
    "scope-refusal": probe_scope_refusal,
    "replay": probe_replay,
    "after-revocation": probe_after_revocation,
    "breakpoint-restart": probe_breakpoint_restart,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe", choices=sorted(PROBES))
    parser.add_argument("--code", required=True, help="one-time enrollment code")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--display-name", default="DEV-038 probe (throwaway)")
    parser.add_argument("--conversation-id", default=None)
    parser.add_argument("--text", default="咖啡 18 个人支出")
    parser.add_argument(
        "--pace-seconds",
        type=float,
        default=11.0,
        help="minimum gap between rate-limited auth-zone requests",
    )
    args = parser.parse_args(argv)
    if args.conversation_id is None:
        args.conversation_id = str(uuid.uuid4())

    device = ProbeDevice(args.base_url, pace_seconds=args.pace_seconds)
    try:
        return PROBES[args.probe](device, args)
    except httpx.HTTPStatusError as error:
        print(f"HTTP {error.response.status_code}: {error.response.text}", file=sys.stderr)
        return 1
    finally:
        device.close()


if __name__ == "__main__":
    raise SystemExit(main())
