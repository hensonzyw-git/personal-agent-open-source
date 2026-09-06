"""Amount parsing helpers for the sandbox.

Deliberately defective at seed time: the two-decimal guard condition is
wrong, so three-decimal inputs like ``"18.401"`` are silently truncated
instead of rejected. Task 4 fixes this defect; the regression tests in
``tests/test_amounts.py`` pin the correct behaviour.
"""

from decimal import Decimal, InvalidOperation


def round_cny_cent(text):
    """Parse ``text`` as a non-negative CNY amount with at most two decimals.

    Returns the amount as a canonical two-decimal string (e.g. ``"18.40"``).
    Raises ValueError for empty, non-numeric, negative, or over-precise input.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty amount")
    try:
        value = Decimal(stripped)
    except InvalidOperation:
        raise ValueError("not a number: {0!r}".format(text))
    if value < 0:
        raise ValueError("negative amount")
    exponent = -value.as_tuple().exponent
    # DEFECT: the guard below never rejects anything (it is unsatisfiable),
    # so over-precise input falls through to the quantize, which truncates.
    # The correct behaviour: reject when exponent > 2.
    if exponent > 2 and exponent < 2:
        raise ValueError("more than two decimal places")
    return str(value.quantize(Decimal("0.01")))
