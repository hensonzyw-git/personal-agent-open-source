"""The fixed set of Feishu endpoints the connector may call.

Arbitrary URLs are forbidden (technical design 2.1, 3.1). The connector talks to
one host and a closed list of path templates; anything else is refused before a
request is built, so a bug or a crafted argument cannot turn the adapter into a
general HTTP client. The host is pinned too: a base url is not accepted from
configuration or arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


#: The only host. Henson's ledger is on the mainland Feishu tenant.
FEISHU_BASE_URL: Final[str] = "https://open.feishu.cn"


class OperationClass(StrEnum):
    """Timeout and retry budgets differ by what the call does (design 6.3)."""

    TOKEN = "token"
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    #: A path template with `{placeholder}` segments, e.g. `.../tables/{table_id}`.
    path_template: str
    operation: OperationClass


# The closed list. Each is a Bitable operation the Finance connector needs, plus
# the tenant-token mint. No delete, no field-create, no app-management endpoint
# is present, so the connector cannot perform one.
TENANT_TOKEN = Endpoint(
    "tenant_token",
    "POST",
    "/open-apis/auth/v3/tenant_access_token/internal",
    OperationClass.TOKEN,
)
LIST_FIELDS = Endpoint(
    "list_fields",
    "GET",
    "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
    OperationClass.READ,
)
GET_RECORD = Endpoint(
    "get_record",
    "GET",
    "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
    OperationClass.READ,
)
SEARCH_RECORDS = Endpoint(
    "search_records",
    "POST",
    "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
    OperationClass.READ,
)
CREATE_RECORD = Endpoint(
    "create_record",
    "POST",
    "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
    OperationClass.WRITE,
)
#: The one *mutating* endpoint, added for `finance.update_expense_category`.
#:
#: Bitable's update is a partial `PUT`: only the fields named in the body are
#: touched and the rest of the row is left alone. That is what makes a one-field
#: correction possible without re-sending -- and therefore without being able to
#: silently rewrite -- 名称, 金额, 日期 or 是否家庭支出. The single-field payload
#: is enforced by the update path, not by hope; this note records why the shape
#: of the endpoint is load-bearing rather than incidental.
#:
#: Still no delete endpoint. A category correction is the only mutation this
#: connector may perform, and removing a ledger row remains something no code
#: here can do at all.
UPDATE_RECORD = Endpoint(
    "update_record",
    "PUT",
    "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
    OperationClass.WRITE,
)

ALLOWLIST: Final[tuple[Endpoint, ...]] = (
    TENANT_TOKEN,
    LIST_FIELDS,
    GET_RECORD,
    SEARCH_RECORDS,
    CREATE_RECORD,
    UPDATE_RECORD,
)


class EndpointNotAllowed(ValueError):
    """A request did not match any allowlisted endpoint."""


# A path segment substituted for a placeholder must look like a resource id:
# no slashes, no query, no traversal. This is a structural guard, not id
# validation -- the schema validator owns real id checks.
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def build_path(endpoint: Endpoint, params: dict[str, str]) -> str:
    """Fill a template's placeholders, refusing anything unsafe.

    Every placeholder must be supplied, every supplied value must be a clean
    single segment, and no extra keys are tolerated -- a stray key is a sign the
    caller is targeting a different endpoint than it thinks.
    """
    placeholders = set(re.findall(r"\{(\w+)\}", endpoint.path_template))
    if set(params) != placeholders:
        raise EndpointNotAllowed(
            f"{endpoint.name} expects params {sorted(placeholders)}, "
            f"got {sorted(params)}"
        )
    for key, value in params.items():
        if not isinstance(value, str) or not _SEGMENT.match(value):
            raise EndpointNotAllowed(f"unsafe value for {key} in {endpoint.name}")
    path = endpoint.path_template
    for key, value in params.items():
        path = path.replace(f"{{{key}}}", value)
    return path


def full_url(endpoint: Endpoint, params: dict[str, str]) -> str:
    return FEISHU_BASE_URL + build_path(endpoint, params)
