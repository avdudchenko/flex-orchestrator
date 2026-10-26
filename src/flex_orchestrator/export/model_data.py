"""Containers for exported model values (PLAN.md D3)."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal


@dataclass(frozen=True)
class ExportedSeries:
    """One exported value: a timeseries or a scalar.

    Attributes:
        key: The stored label: the component path, plus the custom index for
            an element of an indexed component (``"costing.capital_cost[pump1]"``).
        kind: ``"timeseries"`` or ``"scalar"``.
        component: The Pyomo component path the value came from.
        index: The custom (non-time) index, as JSON-friendly values; ``[]`` for
            a component with no custom index.
        values: The values; one per time point for a timeseries, one for a
            scalar. ``None`` where the model has no value.
        timestamps: The time point of each value (timeseries only): the model's
            datetimes, or the raw time-set values if it has none.
        unit_pyomo: The Pyomo unit string, or ``None``.
        unit_qudt: The QUDT key, or ``None`` if nothing mapped the unit.
        unit_verified: Whether ``unit_qudt`` is confirmed in QUDT (or was given
            by the user), rather than only generated from the unit's parts.
        raw_index: The custom index as Pyomo has it (any hashable values).
    """

    key: str
    kind: Literal["timeseries", "scalar"]
    component: str
    index: list[Any]
    values: list[float | None]
    timestamps: list[Any] | None
    unit_pyomo: str | None
    unit_qudt: str | None
    unit_verified: bool = False
    raw_index: tuple[Any, ...] = ()

    @property
    def stale(self) -> bool:
        """Whether any value is missing (the model had no value for it)."""
        return any(value is None for value in self.values)

    @property
    def value(self) -> float | None:
        """The value of a scalar.

        Raises:
            TypeError: If this is a timeseries.
        """
        if self.kind != "scalar":
            raise TypeError(f"{self.key!r} is a timeseries; use .values.")
        return self.values[0]


@dataclass
class ModelData:
    """Everything :func:`~flex_orchestrator.export.export_model_data` exported.

    Attributes:
        series: The exported values, in walk order.
    """

    series: list[ExportedSeries] = field(default_factory=list)

    def __iter__(self) -> Iterator[ExportedSeries]:
        """Iterate over the exported values."""
        return iter(self.series)

    def __len__(self) -> int:
        """Return the number of exported values."""
        return len(self.series)

    def __getitem__(self, key: str) -> ExportedSeries:
        """Return the value stored under ``key``.

        Raises:
            KeyError: If nothing was exported under ``key``.
        """
        return self._by_key[key]

    @cached_property
    def _by_key(self) -> dict[str, ExportedSeries]:
        return {item.key: item for item in self.series}

    def keys(self) -> list[str]:
        """Return every exported key, in walk order."""
        return [item.key for item in self.series]

    def timeseries(self) -> list[ExportedSeries]:
        """Return the timeseries."""
        return [item for item in self.series if item.kind == "timeseries"]

    def scalars(self) -> list[ExportedSeries]:
        """Return the scalars."""
        return [item for item in self.series if item.kind == "scalar"]

    def stale(self) -> list[str]:
        """Return the keys with at least one missing value."""
        return [item.key for item in self.series if item.stale]
