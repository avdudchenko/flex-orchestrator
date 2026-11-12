"""Commands: named, validated operations on an orchestrator.

A command is a function ``fn(orch, params, progress, **runtime) -> result`` whose
``params`` argument is a pydantic model (a command that needs neither may take only
``orch``, or ``orch`` and ``params``). :func:`~flexorchestrator.ext.command` turns it
into a :class:`Command`, and one definition is then callable from Python
(``orch.run_command("price_sweep", prices=[0.1])``), from the CLI
(``flex-orch cmd price_sweep --prices 0.1``) and from a host app's HTTP API, with the
same JSON parameters.

``runtime`` carries what JSON can't: callables such as a ``build`` hook passed
from Python. It is never recorded, apart from the hooks' import strings.

Every call is recorded as a run of the ``flexorch::commands`` ledger: its
parameters as the ``params`` input, what it returned as the ``result`` output
(JSON), and who ran it on which study in the metadata. A call that raises ends
``failed`` with the error.
"""

import inspect
import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel, ValidationError

from flexorchestrator.backend.base import recording
from flexorchestrator.errors import CommandCancelled, CommandError

COMMANDS_LEDGER = "flexorch::commands"
"""str: The backend study every command call is recorded in."""
COMMAND_KIND = "command"
"""str: The ``flexorch_kind`` metadata of a command run."""
PARAMS = "params"
"""str: The label of a command run's JSON input holding its parameters."""
RESULT = "result"
"""str: The label of a command run's JSON output holding what it returned."""

Role = Literal["viewer", "editor", "owner"]
"""The least role that may run a command over HTTP (the host app enforces it)."""


class NoParams(BaseModel):
    """The params model of a command that takes no parameters."""


@dataclass(frozen=True)
class Command:
    """A registered command.

    Attributes:
        name: The command's unique name.
        fn: ``fn(orch, params, progress, **runtime) -> result``.
        params_model: The pydantic model its parameters are checked against.
        role: The least role that may run it over HTTP.
        long_running: Whether HTTP sends it through a job queue.
        description: One line for ``--help`` and the OpenAPI schema; the first
            line of the function's docstring by default.
    """

    name: str
    fn: Callable[..., Any]
    params_model: type[BaseModel]
    role: Role = "editor"
    long_running: bool = False
    description: str = ""

    def call(
        self, orch: Any, params: BaseModel, progress: "Progress", **runtime: Any
    ) -> Any:
        """Call the function with the arguments it takes.

        ``fn(orch)``, ``fn(orch, params)`` and ``fn(orch, params, progress)``
        are all commands; ``runtime`` values go in as keywords.
        """
        positional = [
            p
            for p in inspect.signature(self.fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        args = (orch, params, progress)[: max(1, min(3, len(positional)))]
        return self.fn(*args, **runtime)

    def validate(self, params: Any = None, **fields: Any) -> BaseModel:
        """Return ``params`` (a mapping or model) and ``fields`` as the params model.

        Raises:
            CommandError: If they don't validate; the message lists each field.
        """
        if isinstance(params, self.params_model) and not fields:
            return params
        if isinstance(params, BaseModel):
            params = params.model_dump()
        merged = {**dict(params or {}), **fields}
        try:
            return self.params_model.model_validate(merged)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc']) or '(params)'}: "
                f"{error['msg']}"
                for error in exc.errors()
            )
            raise CommandError(
                f"Invalid parameters for command {self.name!r}: {problems}."
            ) from exc


def command(
    name: str | None = None,
    *,
    role: Role = "editor",
    long_running: bool = False,
    params: type[BaseModel] | None = None,
    description: str | None = None,
) -> Callable[[Callable[..., Any]], Command]:
    """Make a function a :class:`Command`.

    Args:
        name: The command's name; the function's name by default.
        role: The least role that may run it over HTTP.
        long_running: Whether HTTP sends it through a job queue.
        params: The params model; by default the annotation of the function's
            second parameter, or :class:`NoParams` if it has none.
        description: One line of help; the docstring's first line by default.

    Returns:
        A decorator that returns the :class:`Command`.

    Example:
        >>> from pydantic import BaseModel
        >>> class Echo(BaseModel):
        ...     text: str
        >>> @command()
        ... def echo(orch, params: Echo, progress) -> dict:
        ...     return {"text": params.text}
        >>> echo.name, echo.params_model.__name__
        ('echo', 'Echo')
    """

    def wrap(fn: Callable[..., Any]) -> Command:
        model = params or _params_model(fn)
        doc = (inspect.getdoc(fn) or "").strip().splitlines()
        return Command(
            name=name or fn.__name__,
            fn=fn,
            params_model=model,
            role=role,
            long_running=long_running,
            description=description if description is not None else (doc or [""])[0],
        )

    return wrap


def _params_model(fn: Callable[..., Any]) -> type[BaseModel]:
    parameters = list(inspect.signature(fn).parameters.values())
    if len(parameters) < 2:
        return NoParams
    try:
        hints = get_type_hints(fn)
    except Exception:  # pragma: no cover - an unresolvable annotation
        hints = {}
    model = hints.get(parameters[1].name)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise CommandError(
            f"Command {fn.__name__!r}: annotate its second parameter with a "
            "pydantic model, or pass params= to @command."
        )
    return model


class Progress:
    """Progress reports and cancellation for one command call.

    A command calls :meth:`update` as it goes and :meth:`check` between steps;
    whoever runs it (a caller, the CLI, a host app's job queue) reads
    :meth:`snapshot` and may :meth:`cancel` it.

    Args:
        callback: Called with :meth:`snapshot` after every update.

    Example:
        >>> progress = Progress()
        >>> progress.update(total=2, message="fitting")
        >>> progress.advance()
        >>> progress.snapshot()["done"]
        1
    """

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None):
        """Start with nothing done; see the class docstring."""
        self.done = 0
        self.total: int | None = None
        self.message: str | None = None
        self._callback = callback
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    def update(
        self,
        done: int | None = None,
        total: int | None = None,
        message: str | None = None,
    ) -> None:
        """Set how much is done, of how much, and what is happening."""
        with self._lock:
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if message is not None:
                self.message = message
        self._notify()

    def advance(self, steps: int = 1, message: str | None = None) -> None:
        """Count ``steps`` more as done."""
        with self._lock:
            self.done += steps
            if message is not None:
                self.message = message
        self._notify()

    def cancel(self) -> None:
        """Ask the command to stop at its next :meth:`check`."""
        self._cancelled.set()

    def child(self) -> "Progress":
        """Return a :class:`Progress` with its own counts that shares this one's cancel.

        For a step that reports its own progress, such as the fits a study
        runs first: cancelling either cancels both.

        Example:
            >>> parent = Progress()
            >>> step = parent.child()
            >>> parent.cancel()
            >>> step.cancelled
            True
        """
        child = Progress()
        child._cancelled = self._cancelled
        return child

    @property
    def cancelled(self) -> bool:
        """Whether :meth:`cancel` was called."""
        return self._cancelled.is_set()

    def check(self) -> None:
        """Raise :class:`~flexorchestrator.errors.CommandCancelled` if cancelled."""
        if self.cancelled:
            raise CommandCancelled("The command was cancelled.")

    def snapshot(self) -> dict[str, Any]:
        """Return ``{done, total, message, cancelled}``."""
        with self._lock:
            return {
                "done": self.done,
                "total": self.total,
                "message": self.message,
                "cancelled": self.cancelled,
            }

    def _notify(self) -> None:
        if self._callback is not None:
            self._callback(self.snapshot())


class CommandRegistry:
    """Commands by name, with where each came from.

    Example:
        >>> registry = CommandRegistry()
        >>> @command()
        ... def ping(orch) -> str:
        ...     return "pong"
        >>> registry.register(ping).name
        'ping'
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._commands: dict[str, tuple[Command, str]] = {}

    def register(self, cmd: Command, *, source: str = "user") -> Command:
        """Register a command.

        Args:
            cmd: The command.
            source: Where it comes from (``"builtin"``, ``"plugin:<name>"``,
                ``"user"``), named in collision errors.

        Raises:
            CommandError: If the name is taken.
        """
        if not isinstance(cmd, Command):
            raise CommandError(
                f"{cmd!r} is not a Command; decorate the function with @command()."
            )
        if cmd.name in self._commands:
            other = self._commands[cmd.name][1]
            raise CommandError(
                f"Command {cmd.name!r} from {source} collides with the one from "
                f"{other}; command names are unique."
            )
        self._commands[cmd.name] = (cmd, source)
        return cmd

    def get(self, name: str) -> Command:
        """Return a command by name.

        Raises:
            CommandError: If there is none.
        """
        try:
            return self._commands[name][0]
        except KeyError:
            raise CommandError(
                f"There is no command {name!r}. Commands: {self.names()}."
            ) from None

    def source(self, name: str) -> str:
        """Return where a command came from."""
        self.get(name)
        return self._commands[name][1]

    def names(self) -> list[str]:
        """Return the registered names, sorted."""
        return sorted(self._commands)

    def __contains__(self, name: object) -> bool:
        """Return whether a command is registered under ``name``."""
        return name in self._commands

    def __iter__(self) -> Iterable[Command]:
        """Iterate over the commands, sorted by name."""
        return iter(self._commands[name][0] for name in self.names())


def to_json(value: Any) -> Any:
    """Return ``value`` as plain JSON: models dumped, other objects as strings."""
    return json.loads(json.dumps(value, default=_default))


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def record_call(
    backend: Any,
    metadata: Mapping[str, Any],
    params: BaseModel,
    run: Callable[[], Any],
) -> Any:
    """Run a command call as a run of :data:`COMMANDS_LEDGER` and return its result.

    Args:
        backend: The store.
        metadata: The run's metadata.
        params: The validated parameters, recorded as the ``params`` input.
        run: Calls the command.

    Returns:
        What the command returned.

    Raises:
        Exception: Whatever the command raised; the run is then ``failed``.
    """
    backend.define_study(COMMANDS_LEDGER)
    backend.declare(COMMANDS_LEDGER, PARAMS, role="input", kind="json")
    backend.declare(COMMANDS_LEDGER, RESULT, role="output", kind="json")
    with recording(backend, COMMANDS_LEDGER, metadata) as run_id:
        backend.record(run_id, PARAMS, params.model_dump(mode="json"))
        result = run()
        backend.record(run_id, RESULT, to_json(result))
    return result
