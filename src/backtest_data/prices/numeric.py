"""Versioned price arithmetic independent of caller Decimal context/traps.

Finite sums/products are exact within an explicit supported envelope: every
operand/result has at most 10,000 coefficient digits, stored exponent within
[-10,000, 10,000], and nonzero adjusted exponent within the same bounds. Inputs
or results outside that envelope raise ``numeric_envelope`` rather than silently
round. Precision for exact operations is sized from the actual operands.

Division and benchmark point rounding use 50 significant digits, HALF_EVEN,
and a fresh fixed context. A repeating split price/factor is consequently
rounded; split quantities and matched-statistic N/D sums remain exact.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow, ROUND_HALF_EVEN, Rounded, Underflow, localcontext
from hashlib import sha256


NUMERIC_POLICY = "price_decimal50_half_even_v1"


@dataclass(frozen=True, slots=True)
class SourceNumericPolicy:
    id: str = NUMERIC_POLICY
    division_precision: int = 50
    rounding: str = ROUND_HALF_EVEN
    max_coefficient_digits: int = 10000
    max_absolute_exponent: int = 10000
    context_emin: int = -999999
    context_emax: int = 999999


SOURCE_NUMERIC_POLICY = SourceNumericPolicy()


def _supported(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("finite_decimal_required")
    parts = value.as_tuple()
    policy = SOURCE_NUMERIC_POLICY
    if (len(parts.digits) > policy.max_coefficient_digits
            or abs(parts.exponent) > policy.max_absolute_exponent
            or (value != 0 and abs(value.adjusted()) > policy.max_absolute_exponent)):
        raise ValueError("numeric_envelope: operand/result exceeds price policy bounds")


def _context(precision: int, *, exact: bool = False) -> Context:
    policy = SOURCE_NUMERIC_POLICY
    traps = [InvalidOperation, DivisionByZero, Overflow, Underflow]
    if exact:
        traps += [Inexact, Rounded]
    return Context(prec=max(1, precision), rounding=policy.rounding, Emin=policy.context_emin,
                   Emax=policy.context_emax, capitals=1, clamp=0, flags=[], traps=traps)


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum finite decimals without losing coefficient digits, including 1e28+1."""
    operands = tuple(values)
    if not operands:
        return Decimal(0)
    for value in operands:
        _supported(value)
    least_exponent = min(value.as_tuple().exponent for value in operands)
    greatest_adjusted = max(value.adjusted() for value in operands)
    # Align all coefficients to the smallest exponent and reserve a carry bound
    # for the operand count. This bounds exact signed addition as well.
    precision = max(1, greatest_adjusted - least_exponent + 1) + len(str(len(operands))) + 1
    with localcontext(_context(precision, exact=True)):
        result = sum(operands[1:], operands[0])
    _supported(result)
    return result


def exact_multiply(first: Decimal, second: Decimal) -> Decimal:
    """Multiply full finite coefficients in an operand-sized isolated context."""
    _supported(first)
    _supported(second)
    precision = len(first.as_tuple().digits) + len(second.as_tuple().digits)
    with localcontext(_context(precision, exact=True)):
        result = first * second
    _supported(result)
    return result


def exact_difference(first: Decimal, second: Decimal) -> Decimal:
    """Subtract exactly; copy_negate avoids ambient-context unary rounding."""
    return exact_sum((first, second.copy_negate()))


def fixed_divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Versioned 50-significant-digit HALF_EVEN division with fixed traps."""
    _supported(numerator)
    _supported(denominator)
    with localcontext(_context(SOURCE_NUMERIC_POLICY.division_precision)):
        result = numerator / denominator
    _supported(result)
    return result


def fixed_round(value: Decimal) -> Decimal:
    """Round a benchmark point once using the versioned fixed 50-digit policy."""
    _supported(value)
    with localcontext(_context(SOURCE_NUMERIC_POLICY.division_precision)):
        result = +value
    _supported(result)
    return result


def numeric_signature(*parts: object) -> str:
    """Bind numeric-policy identity and exact immutable inputs to a fingerprint.

    Decimal repr retains source coefficient/scale. Length-delimited components
    avoid concatenation ambiguity. Callers supply immutable deterministic parts
    in semantic order; signatures never depend on ambient arithmetic context.
    """
    encoded = [NUMERIC_POLICY] + [repr(part) for part in parts]
    payload = "".join(f"{len(part)}:{part}" for part in encoded).encode("utf-8")
    return f"{NUMERIC_POLICY}:{sha256(payload).hexdigest()}"
