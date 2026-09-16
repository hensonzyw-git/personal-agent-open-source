"""Read-only trip identity; never changes write/resolver/refund semantics."""
import re
import unicodedata

PARSER_VERSION = 'trip-query-v1'
_SUFFIX = re.compile(r'（(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})*)(?:\.[0-9]+)? [A-Z]{3}）$')


def valid_tag(tag):
    return (isinstance(tag, str) and 0 < len(tag) <= 256 and tag == tag.strip()
            and '#' not in tag and not any(unicodedata.category(c).startswith('C') or c in '\r\n\t' for c in tag))


def query_tag(name):
    if not isinstance(name, str) or name.count('#') != 1:
        return None
    tag = name.split('#', 1)[1].strip()
    tag = _SUFFIX.sub('', tag).strip()
    # Unrecognised trailing annotations are ambiguous, not part of a guessed trip.
    if any(c in tag for c in '()（）'):
        return None
    return tag if valid_tag(tag) else None
