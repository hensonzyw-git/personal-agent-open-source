"""Explicit unbounded grant values; transport fields remain required.

The database expiry is an index projection, never the signed grant scope.
"""
from datetime import datetime, timezone


def expiry_projection(value):
    if value is None:return datetime.max.replace(tzinfo=timezone.utc)
    if isinstance(value,str):return datetime.fromisoformat(value.replace('Z','+00:00'))
    return value


def within_limit(value, maximum):
    return maximum is None or (value is not None and value<=maximum)


def extends_limit(value, previous):
    return value is None or (previous is not None and value>=previous)


def valid_expiry(value, maximum_seconds, now):
    return (value is None and maximum_seconds is None) or (value is not None and value>now
        and (maximum_seconds is None or (value-now).total_seconds()<=maximum_seconds))


def active_window(issued, expires, now, *, allow_unbounded=False):
    return (type(issued) is int and issued<=now and
        ((expires is None and allow_unbounded) or (type(expires) is int and now<expires)))
