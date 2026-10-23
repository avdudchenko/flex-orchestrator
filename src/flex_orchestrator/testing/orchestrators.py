"""Orchestrators for testing commands and plugins (PLAN.md D18).

Both helpers use an in-memory backend and, unless told otherwise, the fake
hooks, so a plugin's tests need no database and no solver.
"""

from typing import Any

from flex_orchestrator.hooks import HookSet

FAKE_HOOKS = HookSet(
    build="flex_orchestrator.testing:fake_build",
    solve="flex_orchestrator.testing:fake_solve",
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
        hooks: The hooks; :data:`FAKE_HOOKS` by default.
        **kwargs: Passed to the orchestrator.

    Example:
        >>> orch = memory_orchestrator()
        >>> orch.study
        'test'
    """
    from flex_orchestrator.backend import MemoryBackend
    from flex_orchestrator.ext import create_orchestrator

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
        cmd: A :class:`~flex_orchestrator.ext.Command` (registered first if the
            orchestrator doesn't have it) or a command name.
        params: Its parameters.
        orch: The orchestrator; a fresh :func:`memory_orchestrator` if omitted.
        progress: A :class:`~flex_orchestrator.ext.Progress` to report to.
        **fields: More parameters.

    Example:
        >>> from flex_orchestrator.ext import command
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
