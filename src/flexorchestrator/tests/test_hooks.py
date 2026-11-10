"""Hooks: resolution, the registry, SolveOutcome and run_solve."""

import types

import pytest

from flexorchestrator.errors import HookError
from flexorchestrator.hooks import (
    DEFAULT_HOOKS,
    HookRegistry,
    HookSet,
    SolveOutcome,
    hook_ref,
    import_hook,
    run_solve,
)
from flexorchestrator.testing import FakeModel, fake_build, fake_solve

pytestmark = pytest.mark.unit

FAKE_BUILD = "flexorchestrator.testing:fake_build"
FAKE_SOLVE = "flexorchestrator.testing:fake_solve"


def module_level_hook(spec, **overrides):
    return spec


class CallableHook:
    def __call__(self, spec, **overrides):
        return spec


# SolveOutcome


def test_solve_outcome_ok_follows_status():
    assert SolveOutcome(status="ok").ok
    assert not SolveOutcome(status="failed").ok
    assert not SolveOutcome(status="error").ok


def test_solve_outcome_round_trips_through_json():
    outcome = SolveOutcome(status="ok", termination="optimal", wall_time=1.5)
    dumped = outcome.model_dump(mode="json")
    assert dumped["ok"] is True
    assert SolveOutcome.model_validate(dumped) == outcome


def test_solve_outcome_is_frozen():
    outcome = SolveOutcome(status="ok")
    with pytest.raises(ValueError):
        outcome.status = "failed"


def test_solve_outcome_rejects_unknown_status():
    with pytest.raises(ValueError):
        SolveOutcome(status="optimal")


# import_hook / hook_ref


def test_import_hook_resolves_function():
    assert import_hook(FAKE_BUILD) is fake_build


def test_import_hook_resolves_dotted_attribute():
    assert import_hook("flexorchestrator.hooks:SolveOutcome.model_validate")


@pytest.mark.parametrize("ref", ["no_colon", ":fn", "module:", ""])
def test_import_hook_rejects_malformed(ref):
    with pytest.raises(HookError, match="not an import string"):
        import_hook(ref)


def test_import_hook_missing_module():
    with pytest.raises(HookError, match="Could not import 'no_such_pkg.mod'"):
        import_hook("no_such_pkg.mod:fn")


def test_import_hook_hints_flexpse_for_default_hooks():
    with pytest.raises(HookError, match="default hooks need flex-pse"):
        import_hook("flexorchestrator.flexpse.missing:build")


def test_import_hook_missing_attribute():
    with pytest.raises(HookError, match="has no attribute 'nope'"):
        import_hook("flexorchestrator.testing:nope")


def test_import_hook_rejects_non_callable():
    with pytest.raises(HookError, match="not a callable"):
        import_hook("flexorchestrator.hooks:HOOK_KINDS")


def test_hook_ref_round_trips_module_level_function():
    ref = hook_ref(fake_build)
    assert ref == "flexorchestrator.testing.hooks:fake_build"
    assert import_hook(ref) is fake_build


def test_hook_ref_none_for_unimportable_callables():
    def closure(spec, **overrides):
        return spec

    main_fn = types.FunctionType(module_level_hook.__code__, {})
    main_fn.__module__ = "__main__"
    main_fn.__qualname__ = "module_level_hook"
    gone_fn = types.FunctionType(module_level_hook.__code__, {})
    gone_fn.__module__ = "no_such_module"
    assert hook_ref(gone_fn) is None
    assert hook_ref(lambda spec: spec) is None
    assert hook_ref(closure) is None
    assert hook_ref(main_fn) is None
    assert hook_ref(CallableHook()) is None


# HookSet


def test_merged_later_set_slots_win():
    merged = DEFAULT_HOOKS.merged(HookSet(build=FAKE_BUILD)).merged(HookSet())
    assert merged.build == FAKE_BUILD
    assert merged.solve == DEFAULT_HOOKS.solve


def test_merged_keeps_callable_identity():
    hook = CallableHook()
    assert HookSet().merged(HookSet(build=hook)).build is hook


def test_to_refs_converts_callables():
    refs = HookSet(build=fake_build, solve=FAKE_SOLVE).to_refs()
    assert refs == {
        "build": "flexorchestrator.testing.hooks:fake_build",
        "solve": FAKE_SOLVE,
    }


def test_to_refs_rejects_lambda():
    with pytest.raises(HookError, match="can't be imported by name"):
        HookSet(build=lambda spec: spec).to_refs()


def test_to_refs_rejects_unresolved_name():
    with pytest.raises(HookError, match="resolve it with HookRegistry"):
        HookSet(build="my_model").to_refs()


def test_default_hooks_are_flexpse_import_strings():
    refs = DEFAULT_HOOKS.to_refs()
    assert set(refs) == {
        "build",
        "solve",
        "apply_surrogate",
        "swap_surrogate",
        "time_sets",
        "configure",
        "time_grid",
        "apply_values",
    }
    assert all(ref.startswith("flexorchestrator.flexpse:") for ref in refs.values())


# HookRegistry


@pytest.fixture
def registry():
    registry = HookRegistry()
    registry.register("fake", build=FAKE_BUILD, solve=fake_solve)
    return registry


def test_resolve_callable_import_string_and_name(registry):
    resolved = registry.resolve(
        HookSet(build="fake", solve=FAKE_SOLVE, apply_surrogate=module_level_hook)
    )
    assert resolved.build is fake_build
    assert resolved.solve is fake_solve
    assert resolved.apply_surrogate is module_level_hook
    assert resolved.swap_surrogate is None


def test_resolve_name_picks_slot_of_same_kind(registry):
    assert registry.resolve_ref("fake", "solve") is fake_solve


def test_resolve_name_missing_slot(registry):
    with pytest.raises(HookError, match="have no apply_surrogate hook"):
        registry.resolve_ref("fake", "apply_surrogate")


def test_resolve_unknown_name_lists_registered(registry):
    with pytest.raises(HookError, match=r"Registered: \['fake'\]"):
        registry.resolve_ref("other", "build")


def test_resolve_unknown_kind(registry):
    with pytest.raises(HookError, match="Unknown hook kind"):
        registry.resolve_ref(fake_build, "export")


def test_resolve_rejects_non_hook(registry):
    with pytest.raises(HookError, match="must be a callable"):
        registry.resolve_ref(42, "build")


def test_resolve_reports_bad_import_string(registry):
    with pytest.raises(HookError, match="no attribute"):
        registry.resolve(HookSet(build="flexorchestrator.testing:nope"))


def test_register_duplicate_needs_replace(registry):
    with pytest.raises(HookError, match="already registered"):
        registry.register("fake", build=fake_build)
    registry.register("fake", build=module_level_hook, replace=True)
    assert registry.resolve_ref("fake", "build") is module_level_hook


@pytest.mark.parametrize("name", ["", "pkg:fn"])
def test_register_rejects_bad_name(name):
    with pytest.raises(HookError, match="can't contain ':'"):
        HookRegistry().register(name, build=fake_build)


@pytest.mark.parametrize("hook", ["other_name", 42])
def test_register_rejects_names_and_non_callables(hook):
    with pytest.raises(HookError, match="callable or an import string"):
        HookRegistry().register("x", build=hook)


def test_names_sorted(registry):
    registry.register("a", build=fake_build)
    assert registry.names() == ["a", "fake"]


# run_solve


def test_run_solve_passes_outcome_through_and_fills_wall_time():
    model = fake_build({})
    outcome = run_solve(fake_solve, model)
    assert outcome.ok
    assert outcome.wall_time is not None and outcome.wall_time >= 0
    assert isinstance(model, FakeModel) and model.solved


def test_run_solve_keeps_hook_wall_time():
    outcome = run_solve(lambda model: SolveOutcome(status="ok", wall_time=7.0), None)
    assert outcome.wall_time == 7.0


def test_run_solve_passes_options():
    outcome = run_solve(fake_solve, None, status="failed")
    assert outcome.status == "failed"
    assert outcome.termination == "infeasible"


def test_run_solve_turns_exception_into_error_outcome():
    outcome = run_solve(fake_solve, None, status="error")
    assert outcome.status == "error"
    assert outcome.message == "RuntimeError: fake solver crashed"
    assert "Traceback" in outcome.log
    assert outcome.wall_time is not None


def test_run_solve_rejects_wrong_return_type():
    with pytest.raises(HookError, match="returned dict"):
        run_solve(lambda model: {"status": "ok"}, None)


def test_run_solve_does_not_swallow_keyboard_interrupt():
    def interrupted(model):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_solve(interrupted, None)


def test_fake_build_copies_spec():
    spec = {"a": {"b": 1}}
    model = fake_build(spec, x=1)
    spec["a"]["b"] = 2
    assert model.spec == {"a": {"b": 1}}
    assert model.overrides == {"x": 1}
