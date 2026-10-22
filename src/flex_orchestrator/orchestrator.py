"""The orchestrator: studies, datasets and model results on a storage backend.

Model results are stored **explicitly**: every call names the model and the
tag, and there is no "current model" (PLAN.md D4). A study ``s`` is laid out as
backend studies (D10): ``s`` itself; ``s::runs``, where each stored result is
one run carrying ``{flexorch_kind: "result", flexorch_tag: tag}``, the model
snapshot (:mod:`flex_orchestrator.results`), any values stored natively, and
the ``solve_outcome`` JSON; and ``s::data``, where each dataset is one run (D6,
:mod:`flex_orchestrator.data`).
"""

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Any, Literal

from flex_orchestrator import data as datasets
from flex_orchestrator import frames, results
from flex_orchestrator.backend.base import StorageBackend, to_utc
from flex_orchestrator.data import DATA_KIND, StoredData
from flex_orchestrator.errors import (
    AmbiguousColumnError,
    HookError,
    StorageError,
    StudyExistsError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
)
from flex_orchestrator.export import ModelData, UnitMapper, export_model_data
from flex_orchestrator.hooks import (
    DEFAULT_HOOKS,
    HookRegistry,
    HookSet,
    SolveOutcome,
    run_solve,
)
from flex_orchestrator.results import MODEL_MANIFEST, MODEL_VALUES
from flex_orchestrator.specs import check_tag

RESULT_KIND = "result"
LEDGERS = {RESULT_KIND: "runs", DATA_KIND: "data"}
"""dict: Item kind -> the ledger study (``<s>::<ledger>``) that holds it (D10)."""
SOLVE_OUTCOME = "solve_outcome"
"""str: The label of the JSON output that holds a result's solve outcome."""


class SolveNotOptimal(Exception):  # noqa: N818 (recorded as the run's error type)
    """Recorded as the error of a result run whose solve didn't succeed."""


@dataclass(frozen=True)
class StoredResult:
    """What :meth:`Orchestrator.store_model_results` stored.

    Attributes:
        tag: The result's tag.
        run_id: The backend run.
        status: ``"succeeded"``, or ``"failed"`` when the solve wasn't ``ok``
            (then only the solve outcome is stored, and the tag can be reused).
        outcome: The solve outcome, if one was given.
        keys: The exported keys stored in the snapshot.
        streams: The keys also stored as native variables.
    """

    tag: str
    run_id: str
    status: Literal["succeeded", "failed"]
    outcome: SolveOutcome | None = None
    keys: list[str] = field(default_factory=list)
    streams: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the result was stored with its model values."""
        return self.status == "succeeded"


@dataclass(eq=False)
class ResultHandle:
    """A stored result, read lazily from its snapshot (PLAN.md D7).

    The snapshot is read the first time values are asked for, then kept.

    Attributes:
        tag: The result's tag.
        run_id: The backend run.
        metadata: The run's metadata (``flexorch_tag``, versions, ...).
        started_at: When the run started (UTC).
    """

    tag: str
    run_id: str
    metadata: dict[str, Any]
    started_at: Any
    _orch: "Orchestrator" = field(repr=False)
    _loaded: tuple[ModelData, Any] | None = field(default=None, repr=False)

    @classmethod
    def _from_run(cls, orch: "Orchestrator", run: Any) -> "ResultHandle":
        return cls(
            run.metadata.get("flexorch_tag"),
            run.run_id,
            dict(run.metadata),
            run.started_at,
            orch,
        )

    def _snapshot(self) -> tuple[ModelData, Any]:
        if self._loaded is None:
            self._loaded = self._orch._read_snapshot(self.run_id)
        return self._loaded

    def data(self, units: Mapping[str, str] | None = None) -> ModelData:
        """Return the values, converted to ``units`` if given.

        Args:
            units: ``{key or component path: Pyomo unit}``, as in
                :meth:`Orchestrator.model_results`.
        """
        data = self._snapshot()[0]
        return results.convert(data, units) if units else data

    def to_frame(
        self,
        frame: frames.FrameKind | None = None,
        units: Mapping[str, str] | None = None,
    ) -> Any:
        """Return the timeseries as one table, a column per key.

        polars gets a ``time`` column first, pandas a UTC index. Scalars are in
        :meth:`scalars`.

        Args:
            frame: ``"polars"`` or ``"pandas"``; the orchestrator's default if
                omitted.
            units: Units to convert to, as in :meth:`data`.
        """
        return frames.model_frame(self.data(units), frame or self._orch.frame)

    def to_polars(self, units: Mapping[str, str] | None = None) -> Any:
        """Return :meth:`to_frame` as a polars DataFrame."""
        return self.to_frame("polars", units)

    def to_pandas(self, units: Mapping[str, str] | None = None) -> Any:
        """Return :meth:`to_frame` as a pandas DataFrame."""
        return self.to_frame("pandas", units)

    def scalars(self, units: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Return ``{key: value}`` for every scalar."""
        return {s.key: s.value for s in self.data(units).scalars()}

    @property
    def units(self) -> dict[str, str | None]:
        """``{key: Pyomo unit}`` for every stored key."""
        return {s.key: s.unit_pyomo for s in self.data()}

    @property
    def outcome(self) -> SolveOutcome | None:
        """The solve outcome stored with the result, if any."""
        stored = self._snapshot()[1]
        if stored is None:
            return None
        return SolveOutcome.model_validate(
            {k: v for k, v in stored.items() if k != "ok"}
        )


class Orchestrator:
    """Studies, model results and hooks on one storage backend.

    Args:
        backend: The store, e.g. :class:`~flex_orchestrator.backend.MemoryBackend`
            or :class:`~flex_orchestrator.backend.AcquiriumBackend`.
        study: A study to select; it must exist.
        hooks: Hooks that replace the flex-pse defaults, slot by slot.
        registry: Where registered hook names resolve.
        unit_mapper: Maps Pyomo units to QUDT. By default it verifies keys
            with the backend's ``resolve_unit``.
        strict_units: With the default mapper, raise on a unit with no QUDT
            key instead of storing it without one.
        frame: What reads return by default, ``"polars"`` or ``"pandas"`` (D7).
        allow_delete: Whether :meth:`delete`, :meth:`delete_study` and
            ``replace=True`` may delete anything (D9). A deployment that must
            never lose a stored item turns this off.

    Example:
        >>> from flex_orchestrator.backend import MemoryBackend
        >>> orch = Orchestrator(MemoryBackend())
        >>> orch.create_study("example_a")
        'example_a'
    """

    def __init__(
        self,
        backend: StorageBackend,
        *,
        study: str | None = None,
        hooks: HookSet | None = None,
        registry: HookRegistry | None = None,
        unit_mapper: UnitMapper | None = None,
        strict_units: bool = False,
        frame: frames.FrameKind = "polars",
        allow_delete: bool = True,
    ) -> None:
        """Set up the orchestrator; see the class docstring for the arguments."""
        self.backend = backend
        self.hooks = DEFAULT_HOOKS.merged(hooks) if hooks else DEFAULT_HOOKS
        self.registry = registry or HookRegistry()
        self.unit_mapper = unit_mapper or UnitMapper(
            resolver=backend.resolve_unit, strict=strict_units
        )
        if frame not in frames.FRAME_KINDS:
            raise ValueError(
                f"frame must be one of {frames.FRAME_KINDS}, not {frame!r}."
            )
        self.frame = frame
        self.allow_delete = allow_delete
        self._study: str | None = None
        self._locks: dict[str, threading.RLock] = {}
        self._locks_lock = threading.Lock()
        if study is not None:
            self.select_study(study)

    @property
    def acquirium(self) -> Any:
        """The backend's Acquirium client, for Acquirium's own API (PLAN.md D8).

        The orchestrator doesn't wrap queries: use the client directly, e.g.
        ``orch.acquirium.study.get("example_a::runs").experiments.where(...)``.

        Raises:
            StorageError: If the backend isn't backed by Acquirium.
        """
        client = getattr(self.backend, "acquirium", None)
        if client is None:
            raise StorageError(
                f"{type(self.backend).__name__} has no Acquirium client; "
                "orch.acquirium needs the Acquirium backend."
            )
        return client

    # Studies (D9)

    @property
    def study(self) -> str:
        """The selected study.

        Raises:
            StudyNotFoundError: If no study is selected.
        """
        if self._study is None:
            raise StudyNotFoundError(
                "No study is selected; call create_study() or select_study() first."
            )
        return self._study

    def create_study(self, name: str) -> str:
        """Create a study and select it.

        Raises:
            StudyExistsError: If the study exists. Use
                :meth:`get_or_create_study` when either is fine.
        """
        check_tag(name)
        if self.backend.has_study(name):
            raise StudyExistsError(
                f"Study {name!r} exists; use select_study({name!r}) to work in it."
            )
        self.backend.define_study(name)
        self._define_ledgers(name)
        self._study = name
        return name

    def select_study(self, name: str) -> str:
        """Select an existing study.

        Raises:
            StudyNotFoundError: If it doesn't exist.
        """
        if not self.backend.has_study(name):
            raise StudyNotFoundError(
                f"There is no study {name!r}; use create_study({name!r}) to make it."
            )
        self._define_ledgers(name)
        self._study = name
        return name

    def get_or_create_study(self, name: str) -> str:
        """Select the study, creating it if it doesn't exist."""
        if self.backend.has_study(name):
            return self.select_study(name)
        return self.create_study(name)

    def delete_study(self, name: str) -> None:
        """Delete a study and everything stored in it, for good.

        Deletes the study's datasets, results and their declarations in
        Acquirium (PLAN.md D9). If it is the selected study, none is selected
        afterwards.

        Raises:
            StorageError: If deletion is turned off, or a run in the study is
                still running (then nothing is deleted).
            StudyNotFoundError: If the study doesn't exist.
        """
        self._check_delete("delete a study")
        if not self.backend.has_study(name):
            raise StudyNotFoundError(f"There is no study {name!r}.")
        with self._lock(name):
            ledgers = [self._ledger(name, ledger) for ledger in LEDGERS.values()]
            present = [ledger for ledger in ledgers if self.backend.has_study(ledger)]
            for ledger in present:
                if self.backend.runs(ledger, status="running"):
                    raise StorageError(
                        f"Study {name!r} has a running run in {ledger!r}; "
                        "finish it before deleting the study."
                    )
            for ledger in present:
                self.backend.delete_study(ledger)
            self.backend.delete_study(name)
        if self._study == name:
            self._study = None

    def delete(self, kind: str, tag: str) -> int:
        """Delete a stored item, for good: every run with this kind and tag.

        Args:
            kind: ``"result"`` or ``"data"``.
            tag: The item's tag (a result tag or a dataset name).

        Returns:
            How many runs were deleted (failed attempts included).

        Raises:
            StorageError: If deletion is turned off, or the item's run is
                still running.
            TagNotFoundError: If nothing has this kind and tag.
        """
        self._check_delete(f"delete {kind} {tag!r}")
        ledger = self._ledger(self.study, _ledger_of(kind))
        with self._lock(self.study):
            found = self.backend.runs(ledger, metadata=_tag_metadata(tag, kind))
            if not found:
                raise TagNotFoundError(
                    f"Study {self.study!r} has no stored {kind} {tag!r}."
                )
            self.backend.delete_runs(ledger, [run.run_id for run in found])
        return len(found)

    # Datasets (D6)

    def store_data(
        self,
        name: str,
        df: Any,
        units: dict[str, str | None] | None = None,
        *,
        time_column: str | None = None,
        replace: bool = False,
    ) -> StoredData:
        """Store a table of measured data as the dataset ``name``.

        Each column becomes a timeseries with its unit, so it can be queried,
        converted and used to fit surrogates (D19). Timestamps are stored in
        UTC; naive ones are taken as UTC.

        Args:
            name: The dataset's name, unique among the study's datasets.
            df: A polars or pandas DataFrame of numeric columns.
            units: ``{column: unit}``, as QUDT keys (``"M3-PER-DAY"``) or as
                written (``"m3/day"``, ``"gal/min"``). Columns left out have no
                unit.
            time_column: The datetime column. For pandas the default is the
                index; for polars, the only datetime column.
            replace: Replace a stored dataset of this name: the new one is
                written first, then the old one is deleted (D9).

        Returns:
            What was stored.

        Raises:
            TagExistsError: If the dataset exists and ``replace`` is false.
            StorageError: If the table can't be stored (no datetimes, repeated
                timestamps, a non-numeric column, an empty table).

        Example:
            >>> units = {"biogas_volume": "m3/day"}
            >>> orch.store_data("biogas_data", df, units=units)  # doctest: +SKIP
        """

        def write() -> StoredData:
            timestamps, columns = frames.to_columns(df, time_column)
            return datasets.store(
                self.backend,
                self._ledger(self.study, LEDGERS[DATA_KIND]),
                name,
                timestamps,
                columns,
                dict(units or {}),
                self.unit_mapper,
                self._metadata(name, DATA_KIND),
            )

        return self._guarded(DATA_KIND, name, replace, write)

    def get_data(
        self,
        name: str | None = None,
        columns: list[str] | None = None,
        start: Any = None,
        end: Any = None,
        *,
        column: str | None = None,
        frame: frames.FrameKind | None = None,
    ) -> Any:
        """Read a dataset, or one column found across the study's datasets.

        Args:
            name: The dataset.
            columns: Only these columns, in this order.
            start: Only rows at or after this datetime (naive means UTC).
            end: Only rows at or before this datetime.
            column: Instead of ``name``: a column name, looked up across the
                study's datasets.
            frame: ``"polars"`` or ``"pandas"``; the orchestrator's default if
                omitted.

        Returns:
            A polars DataFrame with a ``time`` column first, or a pandas
            DataFrame with a UTC ``DatetimeIndex``.

        Raises:
            TagNotFoundError: If the dataset, or a dataset with ``column``,
                doesn't exist.
            AmbiguousColumnError: If ``column`` is in several datasets.
        """
        if (name is None) == (column is None):
            raise TypeError("Give the dataset name, or column= to look one up.")
        if column is not None:
            name, columns = self._dataset_with(column), [column]
        ledger = self._ledger(self.study, LEDGERS[DATA_KIND])
        run = self._latest(name, DATA_KIND)
        timestamps, values = datasets.read(
            self.backend, ledger, run.run_id, name, columns
        )
        keep = [
            i
            for i, when in enumerate(timestamps)
            if (start is None or when >= to_utc(start))
            and (end is None or when <= to_utc(end))
        ]
        return frames.from_columns(
            [timestamps[i] for i in keep],
            {c: [v[i] for i in keep] for c, v in values.items()},
            frame or self.frame,
        )

    # Model results (D4)

    def store_model_results(
        self,
        tag: str,
        model: Any,
        outcome: SolveOutcome | None = None,
        keys: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        *,
        streams: Iterable[str] | None = None,
        replace: bool = False,
        time_sets: Sequence[Any] | None = None,
        datetime_index: Sequence[Any] | None = None,
    ) -> StoredResult:
        """Export a model's values and store them under ``tag``.

        Values are exported with
        :func:`~flex_orchestrator.export.export_model_data` and stored as one
        snapshot (a ``table`` of timeseries plus a JSON manifest: PLAN.md D5).
        Keys named in ``streams`` are also stored as native Acquirium variables,
        for queries and fitting. Everything is checked before the run starts,
        so a value that can't be stored leaves no run behind. If ``outcome``
        isn't ``ok``, only the outcome is stored, in a ``failed`` run, so the
        tag can be used again.

        Args:
            tag: The result's tag, unique among the study's results.
            model: The model, passed explicitly (there is no current model).
            outcome: How the solve ended; stored as ``solve_outcome``.
            keys: Exact paths to export (see ``export_model_data``).
            include: Glob patterns to export.
            exclude: Glob patterns to leave out.
            streams: Keys, component paths or globs of exported values to also
                store natively (``["costing.aggregate_electrical_power"]``).
            replace: Replace a stored result with this tag: the new one is
                written first, then the old one is deleted (D9). A failed
                solve leaves the old result in place.
            time_sets: The model's time Sets. By default the ``time_sets`` hook
                gives them (for flex-pse models, including the partial-horizon
                Sets of startup/shutdown).
            datetime_index: One datetime per time point, for models whose time
                block doesn't have them.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded
                (and ``replace`` is false).
            ExportError, StorageError, UnitError: If the values can't be
                exported or stored.
        """
        _check_outcome(outcome)

        def write() -> StoredResult:
            data = None
            if outcome is None or outcome.ok:
                data = export_model_data(
                    model,
                    keys=keys,
                    include=include,
                    exclude=exclude,
                    time_sets=(
                        time_sets if time_sets is not None else self._time_sets(model)
                    ),
                    datetime_index=datetime_index,
                    unit_mapper=self.unit_mapper,
                )
            return self._store(tag, data, outcome, streams)

        return self._guarded(RESULT_KIND, tag, replace, write)

    def store_model_data(
        self,
        tag: str,
        data: ModelData,
        outcome: SolveOutcome | None = None,
        *,
        streams: Iterable[str] | None = None,
        replace: bool = False,
    ) -> StoredResult:
        """Store values that are already exported under ``tag``.

        :meth:`store_model_results` is this after
        :func:`~flex_orchestrator.export.export_model_data`. Use it directly for
        values exported another way, e.g. from a model that isn't Pyomo.

        Args:
            tag: The result's tag, unique among the study's results.
            data: The values.
            outcome: How the solve ended. If it isn't ``ok``, ``data`` is
                ignored and only the outcome is stored, in a ``failed`` run.
            streams: Values to also store natively; see
                :meth:`store_model_results`.
            replace: Replace a stored result; see :meth:`store_model_results`.

        Returns:
            What was stored.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded
                (and ``replace`` is false).
            ExportError, StorageError, UnitError: If the values can't be
                stored.
        """
        _check_outcome(outcome)
        return self._guarded(
            RESULT_KIND, tag, replace, lambda: self._store(tag, data, outcome, streams)
        )

    def solve_and_store(
        self,
        tag: str,
        model: Any,
        keys: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        *,
        streams: Iterable[str] | None = None,
        replace: bool = False,
        solve: Any = None,
        time_sets: Sequence[Any] | None = None,
        datetime_index: Sequence[Any] | None = None,
        **options: Any,
    ) -> StoredResult:
        """Solve a model and store its results under ``tag``.

        The tag is checked before solving, so a solve is never wasted on a tag
        that can't be stored. A solve that raises or isn't optimal is stored as
        a ``failed`` result holding only its outcome.

        Args:
            tag: The result's tag.
            model: The built model.
            keys: Exact paths to export.
            include: Glob patterns to export.
            exclude: Glob patterns to leave out.
            streams: Values to also store natively; see
                :meth:`store_model_results`.
            replace: Replace a stored result; see :meth:`store_model_results`.
            solve: The solve hook (a callable, import string or registered
                name); the orchestrator's ``solve`` hook by default.
            time_sets: See :meth:`store_model_results`.
            datetime_index: See :meth:`store_model_results`.
            **options: Passed to the solve hook.

        Returns:
            What was stored, with the outcome.

        Raises:
            TagExistsError: If a result with this tag is running or succeeded
                (and ``replace`` is false).
        """
        self._check_new(tag, RESULT_KIND, replace)
        hook = self.registry.resolve_ref(solve or self.hooks.solve, "solve")
        outcome = run_solve(hook, model, **options)
        return self.store_model_results(
            tag,
            model,
            outcome,
            keys,
            include,
            exclude,
            streams=streams,
            replace=replace,
            time_sets=time_sets,
            datetime_index=datetime_index,
        )

    def model_results(
        self, tag: str, units: Mapping[str, str] | None = None
    ) -> ModelData:
        """Read a stored result's model values back from its snapshot.

        Values come back as they were exported, with UTC timestamps, unless
        ``units`` asks for others.

        Args:
            tag: The result's tag.
            units: ``{key or component path: Pyomo unit}`` to convert to, with
                pint (``{"costing.aggregate_electrical_power": "W"}``).

        Raises:
            TagNotFoundError: If no succeeded result has this tag.
            ExportError, UnitError: If ``units`` names a key that wasn't stored,
                or a unit that doesn't convert.
        """
        return self.result(tag).data(units)

    def result(self, tag: str) -> "ResultHandle":
        """Return a handle on a stored result; nothing is read until asked for.

        Raises:
            TagNotFoundError: If no succeeded result has this tag.

        Example:
            >>> orch.result("base").to_pandas()  # doctest: +SKIP
        """
        return ResultHandle._from_run(self, self._latest(tag, RESULT_KIND))

    def stored_model_results(
        self, tags: Iterable[str] | None = None
    ) -> list["ResultHandle"]:
        """Return handles on the study's stored results, oldest first.

        Args:
            tags: Only these tags.

        Example:
            >>> for r in orch.stored_model_results():  # doctest: +SKIP
            ...     df = r.to_frame("polars")
        """
        wanted = None if tags is None else set(tags)
        return [
            ResultHandle._from_run(self, run)
            for run in self.backend.runs(
                self._runs, status="succeeded", metadata={"flexorch_kind": RESULT_KIND}
            )
            if wanted is None or run.metadata.get("flexorch_tag") in wanted
        ]

    def _read_snapshot(self, run_id: str) -> tuple[ModelData, Any]:
        found = {
            r.label: r.value
            for r in self.backend.records(
                self._runs, run_id, labels=[MODEL_MANIFEST, MODEL_VALUES, SOLVE_OUTCOME]
            )
        }
        data = results.read_snapshot(found.get(MODEL_VALUES), found[MODEL_MANIFEST])
        return data, found.get(SOLVE_OUTCOME)

    # Internals

    def _store(
        self,
        tag: str,
        data: ModelData | None,
        outcome: SolveOutcome | None,
        streams: Iterable[str] | None,
    ) -> StoredResult:
        table, manifest, writes = None, None, []
        if data is not None and (outcome is None or outcome.ok):
            table, manifest = results.snapshot(data)
            if streams:
                native = results.select(data, streams)
                writes = results.plan_writes(self.backend, self._runs, native)
            results.declare(self.backend, self._runs, writes)
            self.backend.declare(self._runs, MODEL_MANIFEST, role="output", kind="json")
            if table is not None:
                self.backend.declare(
                    self._runs, MODEL_VALUES, role="output", kind="table"
                )
        if outcome is not None:
            self.backend.declare(self._runs, SOLVE_OUTCOME, role="output", kind="json")
        run_id = self.backend.start_run(self._runs, self._metadata(tag))
        try:
            if manifest is not None:
                self.backend.record(run_id, MODEL_MANIFEST, manifest)
            if table is not None:
                self.backend.record(run_id, MODEL_VALUES, table)
            results.record(self.backend, run_id, writes)
            if outcome is not None:
                self.backend.record(
                    run_id, SOLVE_OUTCOME, outcome.model_dump(mode="json")
                )
        except BaseException as exc:
            self.backend.fail_run(run_id, exc)
            raise
        if outcome is not None and not outcome.ok:
            self.backend.fail_run(
                run_id,
                SolveNotOptimal(
                    f"solve {outcome.status} ({outcome.termination}): {outcome.message}"
                ),
            )
            return StoredResult(tag, run_id, "failed", outcome)
        self.backend.finish_run(run_id)
        return StoredResult(
            tag,
            run_id,
            "succeeded",
            outcome,
            data.keys() if data is not None else [],
            [w.series.key for w in writes],
        )

    @property
    def _runs(self) -> str:
        return self._ledger(self.study, LEDGERS[RESULT_KIND])

    @staticmethod
    def _ledger(study: str, name: str) -> str:
        return f"{study}::{name}"

    def _define_ledgers(self, study: str) -> None:
        for ledger in LEDGERS.values():
            self.backend.define_study(self._ledger(study, ledger))

    def _check_new(self, tag: str, kind: str, replace: bool = False) -> None:
        check_tag(tag)
        if replace:
            self._check_delete(f"replace {kind} {tag!r}")
        ledger = self._ledger(self.study, LEDGERS[kind])
        for run in self.backend.runs(ledger, metadata=_tag_metadata(tag, kind)):
            if run.status == "running" or (run.status == "succeeded" and not replace):
                raise TagExistsError(
                    f"Study {self.study!r} already has a {run.status} {kind} "
                    f"{tag!r} (run {run.run_id})."
                )

    def _guarded(
        self, kind: str, tag: str, replace: bool, write: Callable[[], Any]
    ) -> Any:
        """Check the tag, write, then settle the tag, holding the study's lock."""
        with self._lock(self.study):
            self._check_new(tag, kind, replace)
            stored = write()
            if stored.run_id is not None and getattr(stored, "ok", True):
                self._settle(kind, tag, stored.run_id, replace)
            return stored

    def _settle(self, kind: str, tag: str, run_id: str, replace: bool) -> None:
        """Leave one live run with this tag, after a write (D9).

        The check and the write aren't atomic in Acquirium, so another writer
        may have stored the same tag in between. Without ``replace`` the
        earliest run wins: if another live run started before this one, this
        one is deleted and the call fails. With ``replace`` the latest wins: the
        runs before this one are deleted, and if one started after it, this
        one is deleted and the call fails.
        """
        ledger = self._ledger(self.study, LEDGERS[kind])
        live = [
            run
            for run in self.backend.runs(ledger, metadata=_tag_metadata(tag, kind))
            if run.status in ("running", "succeeded")
        ]
        ids = [run.run_id for run in live]
        here = ids.index(run_id)
        before, after = live[:here], live[here + 1 :]
        lost = after if replace else before
        if lost:
            self.backend.delete_runs(ledger, [run_id])
            raise TagExistsError(
                f"Another write stored {kind} {tag!r} at the same time (run "
                f"{lost[0].run_id}); this one was removed."
            )
        if replace:
            self.backend.delete_runs(
                ledger, [run.run_id for run in before if run.status == "succeeded"]
            )

    def _check_delete(self, what: str) -> None:
        if not self.allow_delete:
            raise StorageError(
                f"Can't {what}: this orchestrator was created with allow_delete=False."
            )

    @contextmanager
    def _lock(self, study: str) -> Iterator[None]:
        with self._locks_lock:
            lock = self._locks.setdefault(study, threading.RLock())
        with lock:
            yield

    def _latest(self, tag: str, kind: str) -> Any:
        ledger = self._ledger(self.study, LEDGERS[kind])
        found = self.backend.runs(
            ledger, status="succeeded", metadata=_tag_metadata(tag, kind)
        )
        if not found:
            raise TagNotFoundError(
                f"Study {self.study!r} has no stored {kind} {tag!r}."
            )
        return found[-1]

    def _dataset_with(self, column: str) -> str:
        ledger = self._ledger(self.study, LEDGERS[DATA_KIND])
        stored = {
            run.metadata["flexorch_tag"]
            for run in self.backend.runs(
                ledger, status="succeeded", metadata={"flexorch_kind": DATA_KIND}
            )
        }
        found = sorted(
            {
                v.metadata.get("dataset")
                for v in self.backend.variables(ledger)
                if v.metadata.get("column") == column
            }
            & stored
        )
        if not found:
            raise TagNotFoundError(
                f"No dataset in study {self.study!r} has a column {column!r}."
            )
        if len(found) > 1:
            raise AmbiguousColumnError(
                f"Column {column!r} is in datasets {found}; name the dataset, e.g. "
                f"get_data({found[0]!r}, columns=[{column!r}])."
            )
        return found[0]

    def _metadata(self, tag: str, kind: str = RESULT_KIND) -> dict[str, str]:
        return {
            **_tag_metadata(tag, kind),
            "flexorch_version": version("flex-orchestrator"),
        }

    def _time_sets(self, model: Any) -> list[Any] | None:
        ref = self.hooks.time_sets
        if ref is None:
            return None
        try:
            hook = self.registry.resolve_ref(ref, "time_sets")
        except HookError:
            if ref == DEFAULT_HOOKS.time_sets:
                # flex-pse isn't installed. The exporter still finds a
                # TimeBlock by duck typing; only flex-pse's partial-horizon
                # Sets need the hook.
                return None
            raise
        return hook(model) or None


def _check_outcome(outcome: Any) -> None:
    if outcome is not None and not isinstance(outcome, SolveOutcome):
        raise TypeError(
            f"outcome must be a SolveOutcome, not {type(outcome).__name__}."
        )


def _ledger_of(kind: str) -> str:
    if kind not in LEDGERS:
        raise ValueError(f"kind must be one of {sorted(LEDGERS)}, not {kind!r}.")
    return LEDGERS[kind]


def _tag_metadata(tag: str, kind: str = RESULT_KIND) -> dict[str, str]:
    return {"flexorch_kind": kind, "flexorch_tag": tag}
