"""The extension API: commands, plugins and lifecycle events.

- :func:`command` makes a function a :class:`Command`, callable from Python
  (``orch.run_command``), the CLI (``flex-orch cmd``) and, in Phase 2, HTTP.
- A :class:`Plugin` bundles commands, named hooks, listeners and an
  orchestrator subclass; :func:`create_orchestrator` builds an orchestrator with
  the enabled plugins.
- An :class:`~flexorchestrator.Orchestrator` subclass may override the stable
  methods ``build_hooks``, ``default_export_selection``, ``before_store``,
  ``after_store``, ``on_task_result`` and ``on_study_created``.
"""

import os
from typing import Any

from flexorchestrator.ext.commands import (
    COMMANDS_LEDGER,
    Command,
    CommandRegistry,
    NoParams,
    Progress,
    command,
)
from flexorchestrator.ext.events import EVENTS, EventBus
from flexorchestrator.ext.plugins import (
    ALLOWLIST_ENV,
    ENTRY_POINT_GROUP,
    PLUGIN_API_VERSION,
    Plugin,
    installed_plugins,
    load_plugins,
    orchestrator_class,
)


def create_orchestrator(*args: Any, plugins: Any = None, **kwargs: Any) -> Any:
    """Build an orchestrator with the enabled plugins, as their subclass if one has it.

    The plugins are the ones passed plus those in ``FLEXORCH_PLUGINS``. A
    plugin's ``orchestrator_cls`` is the class built; otherwise
    :class:`~flexorchestrator.Orchestrator`.

    Args:
        *args: Passed to the orchestrator (e.g. a backend).
        plugins: :class:`Plugin` objects or installed plugin names.
        **kwargs: Passed to the orchestrator; ``environ`` (``os.environ`` by
            default) is where ``FLEXORCH_PLUGINS`` and
            ``FLEXORCH_RAY_ADDRESS`` are read.

    Returns:
        The orchestrator.

    Raises:
        PluginError: If a plugin can't be loaded (see :func:`load_plugins`).
    """
    from flexorchestrator.orchestrator import BUILTIN_COMMANDS, Orchestrator

    environ = kwargs.pop("environ", None)
    loaded = load_plugins(plugins, environ=environ, builtin_commands=BUILTIN_COMMANDS)
    cls = orchestrator_class(loaded, Orchestrator)
    # The plugins are loaded: the orchestrator mustn't read the allowlist again.
    env = {**(os.environ if environ is None else environ), ALLOWLIST_ENV: ""}
    return cls(*args, plugins=loaded, environ=env, **kwargs)


__all__ = [
    "ALLOWLIST_ENV",
    "COMMANDS_LEDGER",
    "Command",
    "CommandRegistry",
    "ENTRY_POINT_GROUP",
    "EVENTS",
    "EventBus",
    "NoParams",
    "PLUGIN_API_VERSION",
    "Plugin",
    "Progress",
    "command",
    "create_orchestrator",
    "installed_plugins",
    "load_plugins",
]
