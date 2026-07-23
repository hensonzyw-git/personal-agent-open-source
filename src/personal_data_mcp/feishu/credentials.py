"""The Feishu app credentials, loaded from the injected environment.

`app_id` and `app_secret` are not in the ledger config and are never read from a
file by this code. They are injected as systemd credentials in production and
sit in the environment (technical design 9.1); a local `.env.finance.local` is
loaded into the environment by the shell, not opened here. This module reads the
environment it is given, so the secret never has to be handled as a file path in
the codebase.

The dedicated `Personal Agent Finance` app is the only one this path uses. The
older broad-permission bot app is not accepted here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


APP_ID_ENV: Final[str] = "PERSONAL_DATA_MCP_FEISHU_APP_ID"
APP_SECRET_ENV: Final[str] = "PERSONAL_DATA_MCP_FEISHU_APP_SECRET"


class MissingCredentialError(RuntimeError):
    """The Feishu app credentials are not present in the environment."""


@dataclass(frozen=True)
class FeishuCredentials:
    app_id: str
    app_secret: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let the secret reach a repr, a traceback frame or a log.
        return f"FeishuCredentials(app_id=«redacted», app_secret=«redacted»)"


def load_credentials(env: dict[str, str] | None = None) -> FeishuCredentials:
    env = env if env is not None else dict(os.environ)
    app_id = env.get(APP_ID_ENV)
    app_secret = env.get(APP_SECRET_ENV)
    if not app_id or not app_secret:
        raise MissingCredentialError(
            f"set {APP_ID_ENV} and {APP_SECRET_ENV} in the injected environment"
        )
    return FeishuCredentials(app_id=app_id, app_secret=app_secret)
