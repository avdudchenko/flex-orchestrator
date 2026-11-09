"""Design studies as plain data: composing specs and reading their results.

Nothing here imports flex-pse; :mod:`flex_orchestrator.flexpse.design` builds them.
"""

from typing import Any

from pydantic import BaseModel

from flex_orchestrator.errors import ExportError
from flex_orchestrator.export import ModelData


class DesignCapacity(BaseModel):
    """One sized capacity.

    Attributes:
        size: The size, in ``units``.
        units: The Pyomo unit of the size.
        at_upper_bound: Whether the bound, not the economics, set the size.
        capital_cost: The overnight cost.
        lifetime_years: The asset life used to annualize it.
        annualized_capital_cost: The cost per year.
    """

    size: float
    units: str | None
    at_upper_bound: bool
    capital_cost: float
    lifetime_years: float
    annualized_capital_cost: float


class DesignSummary(BaseModel):
    """A stored design study's sizes and annualized costs.

    Attributes:
        study_years: The calendar years the study covers.
        currency: The Pyomo unit of the costs.
        capacities: ``{name: capacity}``.
        annual_operating_cost_by_year: ``{modeled year: cost}``.
        operating_cost_by_horizon: ``{(year, month): the horizon's own bill}``.
        annualized_operating_cost: Discounted operating cost per year.
        annualized_capital_cost: Capital recovery per year.
        annualized_cost: The objective: the sum of the two.
        total_capital_cost: Overnight capital, undiscounted.
    """

    study_years: int
    currency: str | None
    capacities: dict[str, DesignCapacity]
    annual_operating_cost_by_year: dict[int, float]
    operating_cost_by_horizon: dict[tuple[int, int], float]
    annualized_operating_cost: float
    annualized_capital_cost: float
    annualized_cost: float
    total_capital_cost: float


def compose_design(base: dict[str, Any], design: dict[str, Any], name: str) -> dict:
    """Return the design spec that runs flowsheet ``base`` over ``design``.

    Args:
        base: A flowsheet spec as JSON. Not modified.
        design: A dumped :class:`~flex_orchestrator.study.config.DesignStudy`.
        name: The merged model's name.
    """
    return {**design, "name": name, "base": base}


def summary_selection(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Return the export selection that stores a design study's summary.

    Args:
        spec: A design spec as JSON.
    """
    return {
        "include": [
            "capacity*",
            "annual*",
            f"horizon*.{_costing(spec)}.aggregate_operating_cost",
        ]
    }


def read_summary(data: ModelData, spec: dict[str, Any]) -> DesignSummary:
    """Return the summary of a design study's stored values.

    Args:
        data: The stored result.
        spec: The design spec it was built from.

    Raises:
        ExportError: If the result's export left out a value the summary needs;
            export ``capacity*``, ``annual*`` and each horizon's
            ``aggregate_operating_cost``.
    """

    def value(key: str) -> float:
        try:
            return data[key].value
        except KeyError:
            raise ExportError(
                f"This result didn't store {key!r}, so it has no design summary."
            ) from None

    costing = _costing(spec)
    capacities = {}
    for capacity in spec["capacities"]:
        block = f"capacity[{capacity['name']}]"
        size = value(f"{block}.size")
        capacities[capacity["name"]] = DesignCapacity(
            size=size,
            units=data[f"{block}.size"].unit_pyomo,
            at_upper_bound=size >= capacity["upper"] - 1e-6,
            capital_cost=value(f"{block}.capital_cost"),
            lifetime_years=capacity["lifetime_years"],
            annualized_capital_cost=value(f"{block}.annualized_capital_cost"),
        )
    years = [y["year"] for y in spec["years"]]
    months = [m["month"] for m in spec["months"]]
    operating = value("annualized_operating_cost")
    capital = value("annualized_capital_cost")
    return DesignSummary(
        study_years=sum(y["represents"] for y in spec["years"]),
        currency=data["annualized_operating_cost"].unit_pyomo,
        capacities=capacities,
        annual_operating_cost_by_year={
            y: value(f"annual_operating_cost[{y}]") for y in years
        },
        operating_cost_by_horizon={
            (y, m): value(f"horizon[{y},{m}].{costing}.aggregate_operating_cost")
            for y in years
            for m in months
        },
        annualized_operating_cost=operating,
        annualized_capital_cost=capital,
        annualized_cost=operating + capital,
        total_capital_cost=sum(c.capital_cost for c in capacities.values()),
    )


def _costing(spec: dict[str, Any]) -> str:
    """Return the name of the base flowsheet's one costing element."""
    return next(
        e["name"] for e in spec["base"]["elements"] if e.get("kind") == "costing"
    )
