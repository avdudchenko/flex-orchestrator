"""Lifecycle events: a lighter alternative to subclassing.

``@orch.on("result_stored")`` registers a listener; the orchestrator calls it
with keyword arguments after the event. A listener that raises propagates: the
item is already stored by then, so nothing is undone.

==================== ==========================================================
Event                Keyword arguments
==================== ==========================================================
``study_created``    ``study``
``spec_stored``      ``item`` (a ``StoreItem``)
``result_stored``    ``item``
``data_stored``      ``item``
``fit_stored``       ``item``
``job_finished``     ``job`` (an ``execute_study`` summary)
``command_finished`` ``name``, ``status`` (``succeeded``/``failed``), ``error``
==================== ==========================================================
"""

from collections.abc import Callable
from typing import Any

from flexorchestrator.errors import PluginError

EVENTS = (
    "study_created",
    "spec_stored",
    "result_stored",
    "data_stored",
    "fit_stored",
    "job_finished",
    "command_finished",
)
"""tuple[str, ...]: Every lifecycle event."""


class EventBus:
    """Listeners per event.

    Example:
        >>> bus = EventBus()
        >>> seen = []
        >>> @bus.on("study_created")
        ... def remember(study):
        ...     seen.append(study)
        >>> bus.emit("study_created", study="a")
        >>> seen
        ['a']
    """

    def __init__(self) -> None:
        """Create a bus with no listeners."""
        self._listeners: dict[str, list[Callable[..., Any]]] = {e: [] for e in EVENTS}

    def on(
        self, event: str, fn: Callable[..., Any] | None = None
    ) -> Callable[..., Any]:
        """Register ``fn`` for ``event``; without ``fn``, return a decorator.

        Raises:
            PluginError: If ``event`` isn't one of
                :data:`~flexorchestrator.ext.events.EVENTS`.
        """
        if event not in self._listeners:
            raise PluginError(f"Unknown event {event!r}; events are {list(EVENTS)}.")
        if fn is None:
            return lambda listener: self.on(event, listener)
        self._listeners[event].append(fn)
        return fn

    def emit(self, event: str, **payload: Any) -> None:
        """Call every listener of ``event`` with ``payload``, in order."""
        for listener in list(self._listeners[event]):
            listener(**payload)
