"""Read export files back without a database (PLAN.md D17)."""

import gzip
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from flex_orchestrator import frames, results
from flex_orchestrator.archive.models import SCHEMA, ResultFile
from flex_orchestrator.errors import ArchiveError
from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.hooks import SolveOutcome


def read_json(path: str | os.PathLike[str]) -> Any:
    """Read an export file, gzipped (``.gz``) or not.

    Raises:
        ArchiveError: If it can't be read, isn't JSON, or has another schema.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        payload = json.loads(data)
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"Can't read export file {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        found = payload.get("schema") if isinstance(payload, dict) else None
        raise ArchiveError(
            f"{path} has schema {found!r}; this version reads {SCHEMA!r}."
        )
    return payload


def decode_values(file: ResultFile) -> ModelData:
    """Rebuild a result file's :class:`~flex_orchestrator.export.ModelData`."""
    times = [datetime.fromisoformat(t) for t in file.time_index.timestamps]
    series = []
    for key, entry in file.timeseries.items():
        rows = entry.rows if entry.rows is not None else range(len(times))
        series.append(
            ExportedSeries(
                key=key,
                kind="timeseries",
                component=entry.component,
                index=list(entry.index),
                values=[entry.values[row] for row in rows],
                timestamps=[times[row] for row in rows],
                unit_pyomo=entry.pyomo_unit,
                unit_qudt=entry.unit,
                unit_verified=entry.unit_verified,
                raw_index=tuple(entry.index),
            )
        )
    for key, entry in file.scalars.items():
        series.append(
            ExportedSeries(
                key=key,
                kind="scalar",
                component=entry.component,
                index=list(entry.index),
                values=[entry.value],
                timestamps=None,
                unit_pyomo=entry.pyomo_unit,
                unit_qudt=entry.unit,
                unit_verified=entry.unit_verified,
                raw_index=tuple(entry.index),
            )
        )
    order = {key: i for i, key in enumerate(file.keys)}
    series.sort(key=lambda s: order.get(s.key, len(order)))
    return ModelData(series)


@dataclass(eq=False)
class LoadedResult(results.ResultReads):
    """A result read from an export file; works like a ``ResultHandle``, offline.

    Attributes:
        file: The parsed file.
    """

    file: ResultFile

    @property
    def tag(self) -> str:
        """The result's tag."""
        return self.file.tag

    @property
    def run_id(self) -> str:
        """The run it was exported from."""
        return self.file.run_id

    @property
    def metadata(self) -> dict[str, Any]:
        """The run's metadata."""
        return self.file.metadata

    @property
    def spec_tag(self) -> str | None:
        """The model spec it was built from."""
        return (self.file.model_spec_ref or {}).get("tag")

    @property
    def overrides(self) -> dict[str, Any]:
        """The build overrides."""
        return dict(self.file.overrides)

    @property
    def model_inputs(self) -> list[dict[str, Any]]:
        """Where the model inputs among :attr:`overrides` came from (D21)."""
        return [dict(m) for m in self.file.model_inputs]

    @property
    def outcome(self) -> SolveOutcome | None:
        """How the solve ended."""
        return SolveOutcome.from_stored(self.file.solve_outcome)

    def data(self, units: Mapping[str, str] | None = None) -> ModelData:
        """Return the values, converted to ``units`` if given (see ``ResultHandle``)."""
        data = decode_values(self.file)
        return results.convert(data, units) if units else data

    def to_frame(
        self, frame: frames.FrameKind = "polars", units: Mapping[str, str] | None = None
    ) -> Any:
        """Return the timeseries as one table, a column per key."""
        return frames.model_frame(self.data(units), frame)


def load_result(path: str | os.PathLike[str]) -> LoadedResult:
    """Read a result file (``results/<tag>.json``, or one from ``export_result``).

    No database is needed.

    Raises:
        ArchiveError: If the file can't be read or isn't a result file.

    Example:
        >>> res = load_result("exports/example_a/results/base.json")  # doctest: +SKIP
        >>> res.to_frame("pandas"), res.units  # doctest: +SKIP
    """
    payload = read_json(path)
    try:
        return LoadedResult(ResultFile.model_validate(payload))
    except ValidationError as exc:
        raise ArchiveError(f"{path} isn't a valid result file: {exc}") from exc
