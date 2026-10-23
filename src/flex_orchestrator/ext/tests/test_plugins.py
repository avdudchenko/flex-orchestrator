"""Plugins, lifecycle events and the stable subclass methods (PLAN.md D18)."""

from dataclasses import dataclass

import pytest

from flex_orchestrator.errors import HookError, PluginError
from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.ext import (
    EVENTS,
    EventBus,
    Plugin,
    command,
    create_orchestrator,
    installed_plugins,
    load_plugins,
)
from flex_orchestrator.ext import plugins as plugin_module
from flex_orchestrator.ext.plugins import (
    check_api_version,
    hook_sets,
    orchestrator_class,
    version_in_range,
)
from flex_orchestrator.orchestrator import Orchestrator, StoreItem
from flex_orchestrator.testing import FakeModel, memory_orchestrator

pytestmark = pytest.mark.unit


@command()
def hello(orch) -> str:
    return "hi"


@command(name="hello")
def hello_again(orch) -> str:
    return "hi again"


class Stamped(Orchestrator):
    seen: list

    def before_store(self, item: StoreItem) -> None:
        if item.tag == "refused":
            raise ValueError("refused")
        item.metadata["stamp"] = "yes"
        item.metadata["flexorch_tag"] = "can't replace this"

    def after_store(self, item: StoreItem) -> None:
        self.__dict__.setdefault("stored", []).append(
            (item.kind, item.tag, item.run_id)
        )

    def default_export_selection(self) -> dict:
        return {"keys": ["x"]}


@dataclass
class EP:
    """A stand-in for an importlib.metadata entry point."""

    name: str
    value: object

    def load(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@pytest.fixture
def installed(monkeypatch):
    found = {}
    monkeypatch.setattr(
        plugin_module,
        "entry_points",
        lambda group: [EP(name, value) for name, value in found.items()],
    )
    return found


def scalar(key="x", value=1.0):
    return ModelData(
        [
            ExportedSeries(
                key=key,
                kind="scalar",
                component=key,
                index=[],
                values=[value],
                timestamps=None,
                unit_pyomo=None,
                unit_qudt=None,
            )
        ]
    )


# Versions


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("1.0.0", ">=1,<2", True),
        ("2.0.0", ">=1,<2", False),
        ("1.4.2", "==1.4.2", True),
        ("1.4.2", "1.4", False),
        ("1.4.2", "!=1.4.2", False),
        ("1.4.2", ">1.4,<=1.5", True),
    ],
)
def test_version_ranges(version, spec, ok):
    assert version_in_range(version, spec) is ok


def test_a_range_that_doesnt_parse_or_excludes_this_version():
    with pytest.raises(PluginError, match="Can't read version range"):
        version_in_range("1.0.0", "~=1")
    with pytest.raises(PluginError, match="supports plugin API '>=2'"):
        check_api_version(Plugin(name="new", api_version=">=2"))


# Loading


def test_only_listed_or_passed_plugins_load(installed):
    installed["a"] = Plugin(name="a", commands=[hello])
    installed["b"] = Plugin(name="b")
    assert installed_plugins() == ["a", "b"]
    assert load_plugins(environ={}) == []
    loaded = load_plugins(environ={"FLEXORCH_PLUGINS": "a, "})
    assert [p.name for p in loaded] == ["a"]
    passed = Plugin(name="c")
    loaded = load_plugins(["b", passed, passed], environ={"FLEXORCH_PLUGINS": "a,b"})
    assert [p.name for p in loaded] == ["a", "b", "c"]


def test_entry_point_problems_are_plugin_errors(installed):
    installed["broken"] = ImportError("no module")
    installed["wrong"] = "not a plugin"
    installed["renamed"] = Plugin(name="other")
    with pytest.raises(PluginError, match="No installed plugin is named 'nope'"):
        load_plugins(["nope"], environ={})
    with pytest.raises(PluginError, match="failed to import"):
        load_plugins(["broken"], environ={})
    with pytest.raises(PluginError, match="is a str, not"):
        load_plugins(["wrong"], environ={})
    with pytest.raises(PluginError, match="names must match"):
        load_plugins(["renamed"], environ={})
    with pytest.raises(PluginError, match="Two different plugins"):
        load_plugins([Plugin(name="x"), Plugin(name="x", api_version=">=1")])


@pytest.mark.parametrize(
    ("plugins", "message"),
    [
        (
            [Plugin(name="a", commands=[command(name="fit_surrogate")(hello.fn)])],
            "flex-orchestrator already provides",
        ),
        (
            [
                Plugin(name="a", commands=[hello]),
                Plugin(name="b", commands=[hello_again]),
            ],
            "plugin 'a' already provides",
        ),
        (
            [
                Plugin(name="a", hooks={"m": "x:y"}),
                Plugin(name="b", hooks={"m": "x:z"}),
            ],
            "adds hooks 'm'",
        ),
        ([Plugin(name="a", on_events={"nonsense": print})], "unknown event"),
        (
            [
                Plugin(name="a", orchestrator_cls=Stamped),
                Plugin(name="b", orchestrator_cls=Stamped),
            ],
            "both provide",
        ),
    ],
)
def test_collisions_fail_at_load(plugins, message):
    with pytest.raises(PluginError, match=message):
        load_plugins(plugins, environ={}, builtin_commands=["fit_surrogate"])


def test_the_orchestrator_class_comes_from_a_plugin():
    assert orchestrator_class([Plugin(name="a")], Orchestrator) is Orchestrator
    assert (
        orchestrator_class([Plugin(name="a", orchestrator_cls=Stamped)], Orchestrator)
        is Stamped
    )
    with pytest.raises(PluginError, match="must subclass"):
        orchestrator_class([Plugin(name="a", orchestrator_cls=dict)], Orchestrator)


def test_hook_sets_take_a_ref_or_a_set():
    plugin = Plugin(name="a", hooks={"one": "m:f", "two": {"build": "m:b"}})
    sets = hook_sets(plugin)
    assert sets["one"]["solve"] == "m:f" and sets["one"]["build"] == "m:f"
    assert sets["two"] == {"build": "m:b"}


def test_a_plugin_adds_commands_hooks_listeners_and_lifecycle(memory_backend):
    calls = []
    plugin = Plugin(
        name="demo",
        commands=[hello],
        hooks={
            "fake": {
                "build": "flex_orchestrator.testing:fake_build",
                "solve": "flex_orchestrator.testing:fake_solve",
            }
        },
        on_startup=lambda orch: calls.append(("start", orch._study)),
        on_shutdown=lambda orch: calls.append(("stop", orch._study)),
        on_events={"study_created": [lambda study: calls.append(("created", study))]},
    )
    orch = Orchestrator(memory_backend, plugins=[plugin], environ={}, study=None)
    assert orch.run_command("hello") == "hi"
    assert orch.commands.source("hello") == "plugin 'demo'"
    assert isinstance(orch.build_hooks(), type(orch.hooks))
    orch.create_study("s")
    model = orch.registry.resolve_ref("fake", "build")({"a": 1})
    assert isinstance(model, FakeModel)
    orch.close()
    orch.close()
    assert calls == [("start", None), ("created", "s"), ("stop", "s")]
    with pytest.raises(HookError, match="already registered"):
        orch.registry.register("fake", build="m:f")


def test_plugins_in_the_allowlist_load_into_any_orchestrator(installed, memory_backend):
    installed["demo"] = Plugin(name="demo", commands=[hello])
    orch = Orchestrator(memory_backend, environ={"FLEXORCH_PLUGINS": "demo"})
    assert "hello" in orch.commands


def test_a_plugin_subclass_needs_create_orchestrator(memory_backend):
    plugin = Plugin(name="demo", orchestrator_cls=Stamped)
    with pytest.warns(UserWarning, match="create_orchestrator"):
        Orchestrator(memory_backend, plugins=[plugin], environ={})
    orch = create_orchestrator(memory_backend, plugins=[plugin], environ={})
    assert type(orch) is Stamped


# Events


def test_the_event_bus():
    bus = EventBus()
    seen = []
    bus.on("job_finished", lambda **kw: seen.append(kw))
    bus.on("job_finished")(lambda job: seen.append(job))
    bus.emit("job_finished", job=1)
    assert seen == [{"job": 1}, 1]
    with pytest.raises(PluginError, match="Unknown event"):
        bus.on("nope")
    assert "result_stored" in EVENTS


def test_store_events_and_the_stable_methods():
    plugin = Plugin(name="stamp", orchestrator_cls=Stamped)
    orch = memory_orchestrator(plugins=[plugin], study=None)
    events = []
    for event in ("study_created", "spec_stored", "result_stored", "data_stored"):
        orch.on(event, lambda event=event, **kw: events.append(event))
    orch.create_study("s")
    orch.store_model_spec("base", {"a": 1})
    stored = orch.store_model_data("r", scalar())
    import pandas as pd

    orch.store_data("d", pd.DataFrame({"v": [1.0]}, pd.DatetimeIndex(["2025-01-01"])))
    assert events == ["study_created", "spec_stored", "result_stored", "data_stored"]
    run = orch.backend.runs(orch._runs)[0]
    assert run.metadata["stamp"] == "yes"
    assert run.metadata["flexorch_tag"] == "r"
    assert ("result", "r", stored.run_id) in orch.stored
    with pytest.raises(ValueError, match="refused"):
        orch.store_model_data("refused", scalar())
    assert len(orch.backend.runs(orch._runs)) == 1


def test_the_default_export_selection_applies_when_none_is_given():
    orch = memory_orchestrator(plugins=[Plugin(name="s", orchestrator_cls=Stamped)])
    calls = []

    def export(model, keys=None, include=None, exclude=None, **kw):
        calls.append((keys, include, exclude))
        return scalar("x")

    import flex_orchestrator.orchestrator as module

    original = module.export_model_data
    module.export_model_data = export
    try:
        orch.store_model_results("a", object(), time_sets=[])
        orch.store_model_results("b", object(), include=["y*"], time_sets=[])
    finally:
        module.export_model_data = original
    assert calls == [(["x"], None, None), (None, ["y*"], None)]
