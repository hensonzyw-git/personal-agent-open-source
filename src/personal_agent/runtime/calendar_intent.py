"""Trusted routing guard for explicit calendar-create requests.

An EventKit write exists only after the iPhone reports its result.  A model
sentence such as "已创建日程" has no such evidence, so an explicit request to
create a calendar entry must not complete as a direct answer.
"""

from __future__ import annotations

import re


_CREATE_VERB = r"(?:创建|新建|添加|加入|设定)"
_CALENDAR_NOUN = r"(?:日程|日历|行程)"
_EXPLICIT_CREATE = re.compile(
    rf"(?:{_CREATE_VERB}.{{0,32}}{_CALENDAR_NOUN}"
    rf"|{_CALENDAR_NOUN}.{{0,32}}{_CREATE_VERB})"
)


def is_calendar_create_request(text: str) -> bool:
    """Whether ``text`` explicitly asks the iPhone to create a calendar item.

    This is deliberately narrow. Queries such as "查看今天日程" remain ordinary
    read requests; only a create verb paired with a calendar noun gets the
    no-prose-success guard and an essential create declaration.
    """

    return bool(text and _EXPLICIT_CREATE.search(text))
