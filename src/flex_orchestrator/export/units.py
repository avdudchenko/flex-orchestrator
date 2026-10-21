"""Pyomo units -> QUDT unit keys, and conversion between Pyomo units (PLAN.md D3).

QUDT keys are generated, not looked up in a table of combinations. pint splits
any Pyomo unit into parts, ``(prefix, base unit, exponent)``, and QUDT names
compound units from the same parts by rule: the prefix and the base unit's
symbol, the exponent as a digit, numerator parts joined by ``-``, then ``-PER-``
and the denominator parts::

    L/day     -> liter, day^-1             -> L-PER-DAY
    kg/m**3   -> kilo+gram, meter^-3       -> KiloGM-PER-M3
    kWh       -> kilo+watt_hour            -> KiloW-HR
    J/(kg*K)  -> joule, kilo+gram^-1, K^-1 -> J-PER-KiloGM-K

Only base units need a QUDT symbol: :data:`BASE_UNITS` holds the common ones, a
currency ``XYZ`` becomes ``CCY_XYZ``, and any other base is looked up through
the ``resolver``. With a resolver (Acquirium's ``resolve_unit``), every
generated key is **verified**: it must exist in QUDT with the same SI
multiplier as the Pyomo unit, which also guards against text matches like
Pyomo's ``"a"`` (a year) resolving to QUDT's ``ARE`` (an area). Without one,
keys are generated but unverified.

User ``overrides`` always win. A unit no key can be found for is kept with
``qudt=None`` and a warning, or raises :class:`~flex_orchestrator.errors.UnitError`
in strict mode.
"""

import itertools
import math
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pyomo.environ import units as pyunits

from flex_orchestrator.errors import UnitError

BASE_UNITS: dict[str, str] = {
    "meter": "M",
    "second": "SEC",
    "minute": "MIN",
    "hour": "HR",
    "day": "DAY",
    "week": "WK",
    "year": "YR",
    "liter": "L",
    "gram": "GM",
    "metric_ton": "TONNE",
    "gallon": "GAL_US",
    "watt": "W",
    "watt_hour": "W-HR",
    "joule": "J",
    "newton": "N",
    "pascal": "PA",
    "bar": "BAR",
    "ampere": "A",
    "volt": "V",
    "mole": "MOL",
    "kelvin": "K",
    "degree_Celsius": "DEG_C",
    "degree_Fahrenheit": "DEG_F",
    # pint turns degC inside a compound unit (degC/h) into a temperature
    # difference; QUDT spells rates with DEG_C too (DEG_C-PER-HR).
    "delta_degree_Celsius": "DEG_C",
    "delta_degree_Fahrenheit": "DEG_F",
    "percent": "PERCENT",
}
"""dict: pint base-unit name -> QUDT symbol. Prefixes and combinations are
generated, so this lists only base units, and only so keys can be generated
without a resolver; with one, bases missing here are looked up. The component
tests check every entry against Acquirium's QUDT vocabulary."""

Resolver = Callable[[str], Mapping[str, Any] | None]
"""Looks a unit up in QUDT by key, symbol or name. Returns ``{"uri",
"multiplier", "offset"}`` (Acquirium's ``resolve_unit`` shape) or ``None``, or
raises if not found."""

UnitSource = Literal["override", "verified", "generated", "resolver", "none"]

# QUDT stores many multipliers to about 7 significant figures (HP is 745.6999,
# pint says 745.69987...); distinct units are never within 1 ppm of each other.
_REL_TOL = 1e-6
_MAX_ORDERINGS = 24


@dataclass(frozen=True)
class UnitMapping:
    """How one Pyomo unit maps to QUDT.

    Attributes:
        pyomo: The Pyomo unit string, or ``None`` if the units are unknown.
        qudt: The QUDT key, or ``None`` if none was found.
        source: ``"override"`` (the user's), ``"verified"`` (generated and
            confirmed in QUDT), ``"generated"`` (generated, no resolver to
            confirm it), ``"resolver"`` (found by looking the whole unit up), or
            ``"none"``.
    """

    pyomo: str | None
    qudt: str | None
    source: UnitSource

    @property
    def verified(self) -> bool:
        """Whether the key is known to be right: confirmed in QUDT, or the user's."""
        return self.source in ("override", "verified", "resolver")


class UnitMapper:
    """Maps Pyomo unit strings to QUDT keys, caching each answer.

    Args:
        overrides: ``{pyomo unit: QUDT key}``, checked first, by exact string.
        resolver: Optional QUDT lookup (see :data:`Resolver`). With it, keys are
            verified and unlisted base units are found.
        strict: Raise :class:`UnitError` for a unit no key is found for, instead
            of warning and returning ``qudt=None``.
        base_units: Base-unit symbols; defaults to :data:`BASE_UNITS`.

    Example:
        >>> UnitMapper().map("L/day").qudt
        'L-PER-DAY'
    """

    def __init__(
        self,
        overrides: Mapping[str, str] | None = None,
        resolver: Resolver | None = None,
        strict: bool = False,
        base_units: Mapping[str, str] | None = None,
    ) -> None:
        """Create a mapper (see the class docstring for the arguments)."""
        self.overrides = dict(overrides or {})
        self.resolver = resolver
        self.strict = strict
        self.base_units = dict(BASE_UNITS if base_units is None else base_units)
        self._cache: dict[str | None, UnitMapping] = {}
        self._bases: dict[str, str | None] = {}

    def map(self, unit: str | None) -> UnitMapping:
        """Map one Pyomo unit string.

        Args:
            unit: A Pyomo unit string such as ``"m**3/h"`` (a value without
                units is ``"dimensionless"``). ``None`` means the units are
                unknown and maps to nothing, without a warning.

        Returns:
            The mapping.

        Raises:
            UnitError: In strict mode, if no key is found.
        """
        if unit not in self._cache:
            self._cache[unit] = self._map(unit)
        return self._cache[unit]

    def _map(self, unit: str | None) -> UnitMapping:
        if unit is None:
            return UnitMapping(None, None, "none")
        if unit in self.overrides:
            return UnitMapping(unit, self.overrides[unit], "override")
        quantity = _parse(unit)
        tried: list[str] = []
        if quantity is not None:
            candidates = self._candidates(quantity)
            if candidates is not None:
                if self.resolver is None:
                    return UnitMapping(unit, candidates[0], "generated")
                for key in candidates:
                    if self._confirms(key, quantity):
                        return UnitMapping(unit, key, "verified")
                tried = candidates[:1]
            found = self._lookup(unit, quantity)
            if found is not None:
                return UnitMapping(unit, found, "resolver")
        detail = f" ({tried[0]} isn't in QUDT)" if tried else ""
        message = (
            f"No QUDT unit for Pyomo unit {unit!r}{detail}. Add it with "
            f"unit_map={{{unit!r}: '<QUDT key>'}}."
        )
        if self.strict:
            raise UnitError(message)
        warnings.warn(f"{message} Storing it without a QUDT unit.", stacklevel=3)
        return UnitMapping(unit, None, "none")

    def _candidates(self, quantity: Any) -> list[str] | None:
        """The keys the parts generate, best first; ``None`` if a part has no key."""
        registry = pyunits.pint_registry
        parts = list(quantity.units._units.items())
        if not parts:
            return ["UNITLESS"]
        numerator, denominator = [], []
        for name, exponent in parts:
            key = self._part_key(registry, name)
            if key is None or ("-" in key and abs(exponent) != 1):
                return None
            power = abs(exponent)
            term = key if power == 1 else f"{key}{_exponent(power)}"
            (numerator if exponent > 0 else denominator).append(term)
        orderings = itertools.islice(
            itertools.product(
                itertools.permutations(numerator), itertools.permutations(denominator)
            ),
            _MAX_ORDERINGS,
        )
        keys = []
        for num, den in orderings:
            key = "-".join(num)
            if den:
                key = f"{key}-PER-{'-'.join(den)}" if key else f"PER-{'-'.join(den)}"
            keys.append(key)
        return keys

    def _part_key(self, registry: Any, name: str) -> str | None:
        """The key of one pint unit name, e.g. ``kilowatt_hour`` -> ``KiloW-HR``."""
        if name not in self._bases:
            prefix, base, _ = registry.parse_unit_name(name)[0]
            key = self._base_key(registry, base)
            if key is not None and prefix:
                key = None if key.startswith("CCY_") else prefix.capitalize() + key
            self._bases[name] = key
        return self._bases[name]

    def _base_key(self, registry: Any, base: str) -> str | None:
        if base in self.base_units:
            return self.base_units[base]
        if dict(registry.get_dimensionality(base)) == {"[currency]": 1}:
            return f"CCY_{base}"
        if self.resolver is None:
            return None
        quantity = registry.Quantity(1, base)
        for identifier in dict.fromkeys((registry.get_symbol(base), base)):
            found = self._resolve(identifier)
            if found is not None and _same_multiplier(found, quantity):
                return local_name(found)
        return None

    def _confirms(self, key: str, quantity: Any) -> bool:
        found = self._resolve(key)
        if found is None or local_name(found) != key:
            return False
        return not quantity._is_multiplicative or _same_multiplier(found, quantity)

    def _lookup(self, unit: str, quantity: Any) -> str | None:
        """Look the whole unit string up, for a unit whose parts generate nothing."""
        if self.resolver is None or not quantity._is_multiplicative:
            return None
        found = self._resolve(unit)
        if found is not None and _same_multiplier(found, quantity):
            return local_name(found)
        return None

    def _resolve(self, identifier: str) -> Mapping[str, Any] | None:
        try:
            return self.resolver(identifier) or None
        except Exception:
            return None


def conversion_factor(from_unit: str, to_unit: str) -> float:
    """Return the factor that converts a value in ``from_unit`` to ``to_unit``.

    Used to keep a stored label in the unit it was first declared with, when a
    later run exports it in another unit of the same dimensions.

    Args:
        from_unit: A Pyomo unit string.
        to_unit: A Pyomo unit string.

    Returns:
        ``value_in_to_unit = factor * value_in_from_unit``.

    Raises:
        UnitError: If either unit doesn't parse, the dimensions differ, or an
            offset unit (``degC``) would need converting to a different unit.

    Example:
        >>> conversion_factor("kW", "W")
        1000.0
    """
    if from_unit == to_unit:
        return 1.0
    source, target = _parse(from_unit), _parse(to_unit)
    if source is None or target is None:
        raise UnitError(f"Can't parse unit {from_unit!r} or {to_unit!r}.")
    if source.dimensionality != target.dimensionality:
        raise UnitError(
            f"Can't convert {from_unit!r} to {to_unit!r}: the dimensions differ "
            f"({dict(source.dimensionality)} vs {dict(target.dimensionality)})."
        )
    if not (source._is_multiplicative and target._is_multiplicative):
        raise UnitError(
            f"Can't convert {from_unit!r} to {to_unit!r} by a factor: one is an "
            "offset unit."
        )
    return float(source.to(target.units).magnitude)


def _parse(unit: str) -> Any:
    """Return ``1 <unit>`` as a pint quantity, or ``None`` if it doesn't parse."""
    registry = pyunits.pint_registry
    try:
        return registry.Quantity(1, registry.parse_units(unit))
    except Exception:
        return None


def _exponent(power: float) -> str:
    return str(int(power)) if float(power).is_integer() else str(power)


def local_name(found: Mapping[str, Any]) -> str:
    """Return the QUDT key of a resolver answer: the last part of its ``uri``.

    Example:
        >>> local_name({"uri": "http://qudt.org/vocab/unit/GAL_US-PER-MIN"})
        'GAL_US-PER-MIN'
    """
    return str(found.get("uri", "")).rsplit("/", 1)[-1]


def _same_multiplier(found: Mapping[str, Any], quantity: Any) -> bool:
    if float(found.get("offset") or 0.0) != 0.0:
        return False
    multiplier = float(found.get("multiplier", math.nan))
    return math.isclose(
        multiplier, quantity.to_base_units().magnitude, rel_tol=_REL_TOL
    )
