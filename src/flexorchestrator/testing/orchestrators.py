"""Orchestrators for testing commands and plugins.

Both helpers use an in-memory backend and, unless told otherwise, the fake
hooks, so a plugin's tests need no database and no solver.
"""

from typing import Any

from flexorchestrator.hooks import HookSet

FAKE_HOOKS = HookSet(
    build="flexorchestrator.testing:fake_build",
    solve="flexorchestrator.testing:fake_solve",
)
"""HookSet: Build and solve without a model library or a solver."""


def memory_orchestrator(
    *,
    study: str | None = "test",
    plugins: Any = None,
    hooks: HookSet | None = None,
    **kwargs: Any,
) -> Any:
    """Return an orchestrator on a fresh in-memory backend.

    Args:
        study: A study to create and select; ``None`` for none.
        plugins: Plugins to load; their orchestrator subclass is built.
            ``FLEXORCH_PLUGINS`` is ignored, so tests don't depend on it.
        hooks: The hooks; :data:`~flexorchestrator.testing.orchestrators.FAKE_HOOKS` by
            default.
        **kwargs: Passed to the orchestrator.

    Example:
        >>> orch = memory_orchestrator()
        >>> orch.study
        'test'
    """
    from flexorchestrator.backend import MemoryBackend
    from flexorchestrator.ext import create_orchestrator

    orch = create_orchestrator(
        MemoryBackend(),
        plugins=plugins,
        hooks=FAKE_HOOKS if hooks is None else hooks,
        environ={},
        **kwargs,
    )
    if study is not None:
        orch.create_study(study)
    return orch


def run_command_in_test(
    cmd: Any,
    params: Any = None,
    *,
    orch: Any = None,
    progress: Any = None,
    **fields: Any,
) -> Any:
    """Run a command on a memory orchestrator and return what it returned.

    Args:
        cmd: A :class:`~flexorchestrator.ext.Command` (registered first if the
            orchestrator doesn't have it) or a command name.
        params: Its parameters.
        orch: The orchestrator; a fresh :func:`memory_orchestrator` if omitted.
        progress: A :class:`~flexorchestrator.ext.Progress` to report to.
        **fields: More parameters.

    Example:
        >>> from flexorchestrator.ext import command
        >>> @command()
        ... def ping(orch) -> str:
        ...     return "pong"
        >>> run_command_in_test(ping)
        'pong'
    """
    orch = orch or memory_orchestrator()
    name = cmd if isinstance(cmd, str) else cmd.name
    if not isinstance(cmd, str) and name not in orch.commands:
        orch.register_command(cmd)
    return orch.run_command(name, params, progress=progress, **fields)
