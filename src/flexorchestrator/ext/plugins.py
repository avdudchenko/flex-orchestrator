"""Plugins: bundles of commands, hooks and an orchestrator subclass.

A plugin package exposes a :class:`Plugin` through the
``flexorchestrator.plugins`` entry-point group::

    [project.entry-points."flexorchestrator.plugins"]
    digester_tools = "digester_tools:plugin"

Being installed isn't enough to load: a plugin loads only when its name is in
``FLEXORCH_PLUGINS`` (comma-separated) or it is passed explicitly
(``Orchestrator(plugins=[...])``), so each deployment enables its own set.
Loading checks that the plugin's ``api_version`` range includes
:data:`PLUGIN_API_VERSION`, and that no two plugins (or a plugin and a
built-in) claim the same command or hook name, or both provide an
orchestrator subclass.
"""

import operator
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from flexorchestrator.errors import PluginError
from flexorchestrator.ext.commands import Command
from flexorchestrator.ext.events import EVENTS
from flexorchestrator.hooks import HOOK_KINDS

PLUGIN_API_VERSION = "1.0.0"
"""str: The plugin API's version (semver, separate from the package version)."""
ENTRY_POINT_GROUP = "flexorchestrator.plugins"
"""str: The entry-point group installed plugins register under."""
ALLOWLIST_ENV = "FLEXORCH_PLUGINS"
"""str: The environment variable listing the installed plugins to enable,
comma-separated (installed plugins are off unless listed)."""


@dataclass(frozen=True)
class Plugin:
    """What a plugin provides.

    Attributes:
        name: The plugin's name, as listed in ``FLEXORCH_PLUGINS``.
        api_version: The :data:`PLUGIN_API_VERSION` range it supports, e.g.
            ``">=1,<2"``.
        commands: Commands it adds.
        hooks: Named hooks: ``{name: import string}`` registers that
            hook for every slot, ``{name: {"build": ..., "solve": ...}}``
            registers a set.
        orchestrator_cls: An :class:`~flexorchestrator.Orchestrator` subclass;
            :func:`create_orchestrator` builds it.
        routers: FastAPI routers, mounted by the Phase 2 server at
            ``/ext/<name>``; the library keeps them and doesn't import FastAPI.
        on_startup: Called with the orchestrator once it is set up.
        on_shutdown: Called with the orchestrator when it closes.
        on_events: ``{event: listener or [listeners]}`` (see
            :mod:`flexorchestrator.ext.events`).
    """

    name: str
    api_version: str = ">=1,<2"
    commands: Sequence[Command] = ()
    hooks: Mapping[str, str | Mapping[str, str]] = field(default_factory=dict)
    orchestrator_cls: type | None = None
    routers: Sequence[Any] = ()
    on_startup: Callable[[Any], None] | None = None
    on_shutdown: Callable[[Any], None] | None = None
    on_events: Mapping[str, Callable[..., Any] | Sequence[Callable[..., Any]]] = field(
        default_factory=dict
    )


def load_plugins(
    plugins: Sequence[Plugin | str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    builtin_commands: Sequence[str] = (),
) -> list[Plugin]:
    """Return the plugins to load: those passed, plus those in the allowlist.

    Args:
        plugins: :class:`Plugin` objects, or names of installed plugins.
        environ: Where ``FLEXORCH_PLUGINS`` is read; ``os.environ`` by default.
        builtin_commands: Names plugins may not reuse.

    Returns:
        The plugins, allowlisted ones first, each once.

    Raises:
        PluginError: If a name isn't installed, an entry point isn't a
            :class:`Plugin`, an ``api_version`` excludes this version, or two
            plugins collide.
    """
    environ = os.environ if environ is None else environ
    allowed = [n.strip() for n in environ.get(ALLOWLIST_ENV, "").split(",")]
    wanted: list[Plugin | str] = [n for n in allowed if n] + list(plugins or [])
    loaded: dict[str, Plugin] = {}
    for item in wanted:
        plugin = item if isinstance(item, Plugin) else _from_entry_point(item)
        if plugin.name in loaded:
            if loaded[plugin.name] is not plugin:
                raise PluginError(
                    f"Two different plugins are named {plugin.name!r}; plugin "
                    "names are unique."
                )
            continue
        check_api_version(plugin)
        loaded[plugin.name] = plugin
    check_collisions(list(loaded.values()), builtin_commands)
    return list(loaded.values())


def _from_entry_point(name: str) -> Plugin:
    found = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
    if name not in found:
        raise PluginError(
            f"No installed plugin is named {name!r} (entry-point group "
            f"{ENTRY_POINT_GROUP!r}). Installed: {sorted(found) or 'none'}."
        )
    try:
        plugin = found[name].load()
    except Exception as exc:
        raise PluginError(f"Plugin {name!r} failed to import: {exc}") from exc
    if not isinstance(plugin, Plugin):
        raise PluginError(
            f"Entry point {name!r} is a {type(plugin).__name__}, not a "
            "flexorchestrator.ext.Plugin."
        )
    if plugin.name != name:
        raise PluginError(
            f"Entry point {name!r} loads a plugin named {plugin.name!r}; the two "
            "names must match."
        )
    return plugin


def installed_plugins() -> list[str]:
    """Return the names of the plugins installed, loaded or not, sorted."""
    return sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))


def check_api_version(plugin: Plugin) -> None:
    """Check that ``plugin.api_version`` includes :data:`PLUGIN_API_VERSION`.

    Raises:
        PluginError: If it doesn't, or the range can't be read.
    """
    if not version_in_range(PLUGIN_API_VERSION, plugin.api_version):
        raise PluginError(
            f"Plugin {plugin.name!r} supports plugin API {plugin.api_version!r}, "
            f"but this flex-orchestrator provides {PLUGIN_API_VERSION}. Install "
            "a version of the plugin built for it."
        )


_CLAUSE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(\d+(?:\.\d+){0,2})$")


def version_in_range(version: str, spec: str) -> bool:
    """Return whether ``version`` satisfies every clause of ``spec``.

    Args:
        version: ``"major.minor.patch"``.
        spec: Comma-separated clauses such as ``">=1,<2"`` or ``"==1.0"``;
            missing parts count as 0.

    Raises:
        PluginError: If a clause can't be read.

    Example:
        >>> version_in_range("1.4.0", ">=1,<2")
        True
    """
    have = _parts(version)
    for clause in (c.strip() for c in spec.split(",")):
        match = _CLAUSE.match(clause)
        if match is None:
            raise PluginError(f"Can't read version range {spec!r} (at {clause!r}).")
        op, want = match.group(1) or "==", _parts(match.group(2))
        if not _OPS[op](have, want):
            return False
    return True


_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


def _parts(version: str) -> tuple[int, int, int]:
    numbers = [int(p) for p in version.split(".")]
    return tuple((numbers + [0, 0, 0])[:3])  # type: ignore[return-value]


def check_collisions(
    plugins: Sequence[Plugin], builtin_commands: Sequence[str]
) -> None:
    """Check that the plugins don't claim the same names, or two subclasses.

    Raises:
        PluginError: On the first collision.
    """
    commands = {name: "flex-orchestrator" for name in builtin_commands}
    hooks: dict[str, str] = {}
    subclass: Plugin | None = None
    for plugin in plugins:
        for cmd in plugin.commands:
            if cmd.name in commands:
                raise PluginError(
                    f"Plugin {plugin.name!r} adds command {cmd.name!r}, which "
                    f"{commands[cmd.name]} already provides."
                )
            commands[cmd.name] = f"plugin {plugin.name!r}"
        for name in plugin.hooks:
            if name in hooks:
                raise PluginError(
                    f"Plugin {plugin.name!r} adds hooks {name!r}, which "
                    f"{hooks[name]} already provides."
                )
            hooks[name] = f"plugin {plugin.name!r}"
        for event in plugin.on_events:
            if event not in EVENTS:
                raise PluginError(
                    f"Plugin {plugin.name!r} listens to unknown event {event!r}; "
                    f"events are {list(EVENTS)}."
                )
        if plugin.orchestrator_cls is not None:
            if subclass is not None:
                raise PluginError(
                    f"Plugins {subclass.name!r} and {plugin.name!r} both provide "
                    "an orchestrator subclass; enable only one of them."
                )
            subclass = plugin


def orchestrator_class(plugins: Sequence[Plugin], base: type) -> type:
    """Return the orchestrator class to build: a plugin's subclass, or ``base``.

    Raises:
        PluginError: If the subclass doesn't subclass ``base``.
    """
    for plugin in plugins:
        cls = plugin.orchestrator_cls
        if cls is None:
            continue
        if not (isinstance(cls, type) and issubclass(cls, base)):
            raise PluginError(
                f"Plugin {plugin.name!r}'s orchestrator_cls must subclass "
                f"{base.__name__}."
            )
        return cls
    return base


def listeners(plugin: Plugin) -> list[tuple[str, Callable[..., Any]]]:
    """Return ``(event, listener)`` for each listener the plugin registers."""
    pairs = []
    for event, found in plugin.on_events.items():
        for listener in found if isinstance(found, Sequence) else [found]:
            pairs.append((event, listener))
    return pairs


def hook_sets(plugin: Plugin) -> dict[str, dict[str, str]]:
    """Return the plugin's named hooks as ``{name: {kind: import string}}``.

    A bare import string is registered for every hook kind.
    """
    sets = {}
    for name, refs in plugin.hooks.items():
        if isinstance(refs, str):
            sets[name] = {kind: refs for kind in HOOK_KINDS}
        else:
            sets[name] = dict(refs)
    return sets
