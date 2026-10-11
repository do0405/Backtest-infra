"""Versioned Decimal semantics independent of the process/thread context.

Addition, subtraction and evidence aggregation preserve aligned coefficients
exactly. Division uses 50 significant digits and ROUND_HALF_EVEN with fixed
exponent limits and explicit Decimal traps. Normalization multiplication and
power-of-ten scale changes preserve reported coefficient digits exactly.
"""

from decimal import Context, Decimal, DivisionByZero, InvalidOperation, MAX_EMAX, MIN_EMIN, Overflow, ROUND_HALF_EVEN, localcontext
from functools import wraps

NUMERIC_POLICY = 'financial_exact_add_decimal50_half_even_v2'
CALCULATION_PRECISION = 50
MAX_EXACT_ALIGNED_DIGITS = 131072


def calculation_context(*, precision: int = CALCULATION_PRECISION) -> Context:
    return Context(prec=precision,rounding=ROUND_HALF_EVEN,Emin=-999999,Emax=999999,capitals=1,clamp=0,
                   flags=[],traps=[InvalidOperation,DivisionByZero,Overflow])


def deterministic_decimal(function):
    @wraps(function)
    def calculate(*args,**kwargs):
        with localcontext(calculation_context()):
            return function(*args,**kwargs)
    return calculate


def exact_multiply(first: Decimal, second: Decimal) -> Decimal:
    if not first.is_finite() or not second.is_finite():
        raise ValueError('finite_decimal_multiplier_required')
    precision=max(1,len(first.as_tuple().digits)+len(second.as_tuple().digits))
    context=calculation_context(precision=precision)
    context.Emax=MAX_EMAX
    context.Emin=MIN_EMIN
    with localcontext(context):
        return first*second


def exact_scale(value: Decimal, scale: int) -> Decimal:
    coefficient=value.as_tuple()
    return Decimal((coefficient.sign,coefficient.digits,coefficient.exponent+scale))


def exact_sum(values) -> Decimal:
    """Aligned coefficient sum retains small terms instead of rounding inputs.

    Reject an unsupported alignment span before creating an oversized Decimal
    context. The declared span cap also bounds cancellation work on manually
    supplied inputs that did not pass the XML numeric envelope.
    """
    values=tuple(values)
    if not values:
        return Decimal(0)
    if any(not value.is_finite() for value in values):
        raise ValueError('unsupported_numeric_envelope')
    nonzero=tuple(value for value in values if value)
    if not nonzero:
        return Decimal(0)
    lowest=min(value.as_tuple().exponent for value in nonzero)
    highest=max(value.adjusted() for value in nonzero)
    precision=highest-lowest+1+len(str(len(nonzero)))
    if precision>MAX_EXACT_ALIGNED_DIGITS or abs(lowest)>MAX_EXACT_ALIGNED_DIGITS or abs(highest)>MAX_EXACT_ALIGNED_DIGITS:
        raise ValueError('unsupported_numeric_envelope')
    with localcontext(calculation_context(precision=max(1,precision))):
        return sum(nonzero,Decimal(0))


def exact_difference(first: Decimal, second: Decimal) -> Decimal:
    return exact_sum((first,second.copy_negate()))
