"""Embedding the library in a host application.

Study handles (what selecting a study returns), several threads working through
them at once, and the user each handle records. On memory and files; the
threaded test also on the shared Acquirium.
"""

import threading

import pytest

from flexorchestrator.errors import StudyNotFoundError
from flexorchestrator.orchestrator import Orchestrator
from flexorchestrator.tests.test_verify import OK, cost
from flexorchestrator.verify import verify

COST = "costing.cost"


def test_selecting_a_study_returns_a_handle_fixed_to_it(orch_backend, study_name):
    orch = Orchestrator(orch_backend)
    a = orch.create_study(f"{study_name}-a")
    b = orch.create_study(f"{study_name}-b")
    # The orchestrator follows the last selection; each handle keeps its own.
    assert (a.study, b.study, orch.study) == (
        f"{study_name}-a",
        f"{study_name}-b",
        b.study,
    )
    again = orch.select_study(a.study)
    assert orch.study == again.study == a.study and b.study == f"{study_name}-b"
    assert orch.get_or_create_study(b.study).study == b.study
    assert repr(a) == f"<Orchestrator study='{study_name}-a'>"
    # A handle shares the connection and everything set up on the orchestrator.
    assert a.backend is orch.backend and a.commands is orch.commands
    a.store_model_data("r", cost(1.0), OK)
    assert [r.tag for r in a.stored_model_results()] == ["r"]
    assert b.stored_model_results() == []
    with pytest.raises(StudyNotFoundError, match="create_study"):
        orch.select_study(f"{study_name}-nope")


def test_closing_a_handle_leaves_the_connection_open(orch_backend, study_name):
    closed = []

    class Connection:
        def metadata(self):
            return {"mode": "test"}

        def close(self):
            closed.append(True)

    orch = Orchestrator(orch_backend)
    orch.connection = Connection()
    with orch.create_study(study_name) as handle:
        handle.store_model_data("r", cost(1.0), OK)
    assert closed == []  # the with-block closed only the handle
    orch.close()
    assert closed == [True]


def test_each_handle_records_its_own_user(orch_backend, study_name, monkeypatch):
    monkeypatch.setenv("FLEXORCH_USER", "service")
    orch = Orchestrator(orch_backend)
    alice = orch.create_study(study_name)
    alice.user = "alice"
    bob = orch.select_study(study_name)
    bob.user = "bob"
    alice.store_model_data("by_alice", cost(1.0), OK)
    bob.store_model_data("by_bob", cost(2.0), OK)
    orch.store_model_data("by_service", cost(3.0), OK)
    users = {r.tag: r.metadata["flexorch_user"] for r in orch.stored_model_results()}
    assert users == {"by_alice": "alice", "by_bob": "bob", "by_service": "service"}


@pytest.mark.on_acquirium
def test_threads_working_through_handles_never_share_a_study(orch_backend, study_name):
    """Each thread's results land in its own study, as serially."""
    orch = Orchestrator(orch_backend)
    names = [f"{study_name}-t{i}" for i in range(4)]
    serial = [f"{study_name}-s{i}" for i in range(4)]
    for name in names + serial:
        orch.create_study(name)
    per_thread = 3

    def store(handle, i):
        for k in range(per_thread):
            handle.store_model_data(f"r{k}", cost(10.0 * i + k), OK)

    start = threading.Barrier(len(names))
    errors = []

    def work(i, name):
        try:
            mine = orch.select_study(name)
            mine.user = f"user{i}"
            start.wait()
            for k in range(per_thread):
                # Another thread's selection on the shared orchestrator...
                orch.select_study(names[(i + k + 1) % len(names)])
                # ...never moves this handle.
                mine.store_model_data(f"r{k}", cost(10.0 * i + k), OK)
            assert mine.study == name
        except Exception as exc:  # reported below, with the thread's index
            errors.append((i, exc))

    threads = [threading.Thread(target=work, args=(i, n)) for i, n in enumerate(names)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=600)
    assert not errors, errors
    for i, name in enumerate(serial):
        store(orch.select_study(name), i)

    for i, (threaded, alone) in enumerate(zip(names, serial, strict=True)):
        results = orch.select_study(threaded).stored_model_results()
        assert sorted(r.tag for r in results) == [f"r{k}" for k in range(per_thread)]
        assert {r.metadata["flexorch_user"] for r in results} == {f"user{i}"}
        assert sorted(r.scalars()[COST] for r in results) == [
            10.0 * i + k for k in range(per_thread)
        ]
        # The same values, byte for byte, as the same results stored serially.
        report = verify(orch, [threaded, alone], samples=per_thread)
        assert not report["problems"]
        got, want = report["studies"][threaded], report["studies"][alone]
        assert {t: h["hash"] for t, h in got["result"]["hashes"].items()} == {
            t: h["hash"] for t, h in want["result"]["hashes"].items()
        }
