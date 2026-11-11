"""Running a study's tasks, and a fit's variants, on Ray.

``max_workers > 1`` sends work here. The main process keeps the store: it
prepares each task (``study.task.prepare``), Ray
workers build, solve and export (``study.task.execute``),
and a ``ray.wait`` loop in the main process stores each result as it arrives
(``study.task.finish``). Workers get plain data and
import strings, never a model, a connection or a credential.

The loop keeps at most ``max_workers`` tasks running, kills a task that runs
past ``task_timeout_s`` (``ray.cancel(force=True)``), retries a task whose
worker died or whose build or solve raised (never one whose spec or result
couldn't be stored), and cancels everything still running when the command is
cancelled or ``on_error: stop`` trips.

Ray is local unless an address is given: ``ray.init(address="local",
namespace="flexorch", include_dashboard=False)``, started once per process
and reused. A Ray the caller already connected to is used as it is.
"""

import dataclasses
import time
from collections import deque
from typing import Any

from flexorchestrator.errors import StudyConfigError, TagExistsError
from flexorchestrator.hooks import SolveOutcome

NAMESPACE = "flexorch"
"""str: The Ray namespace the orchestrator's tasks run in."""

RUNTIME_ENV = {
    "env_vars": {
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
}
"""dict: Every worker's ``runtime_env``: one thread per solver library, so
``max_workers`` solves don't oversubscribe the CPUs."""

POLL_S = 0.25
"""float: How often the loop checks timeouts and cancellation, in seconds."""


def connect(address: str | None = None) -> Any:
    """Return the ``ray`` module, connected: to ``address``, or local Ray.

    A Ray this process is already connected to is used as it is.

    Args:
        address: A Ray cluster, e.g. ``"ray://tenant-ray-head:10001"``; ``None``
            starts local Ray (once per process).

    Returns:
        The ``ray`` module.

    Raises:
        StudyConfigError: If Ray isn't installed.
    """
    try:
        import ray
    except ImportError as exc:  # pragma: no cover - Ray comes with Acquirium
        raise StudyConfigError(
            "max_workers > 1 runs on Ray, which isn't installed: "
            "pip install 'ray>=2.59'."
        ) from exc
    if not ray.is_initialized():
        if address is None:
            ray.init(address="local", namespace=NAMESPACE, include_dashboard=False)
        else:
            ray.init(address=address, namespace=NAMESPACE)
    return ray


def remote(
    ray: Any,
    fn: Any,
    *,
    num_cpus: float = 1.0,
    memory: float | None = None,
    max_calls: int | None = None,
) -> Any:
    """Return ``fn`` as a Ray remote function with the orchestrator's options.

    Ray's own retries are off (``max_retries=0``): the loops here retry, so
    one budget (``retries``) covers a raising task and a dead worker alike.

    Args:
        ray: The connected ``ray`` module (:func:`connect`).
        fn: A module-level function, importable on the workers.
        num_cpus: CPUs each call reserves.
        memory: Bytes of memory each call reserves, if set.
        max_calls: Restart a worker process after this many calls, if set.

    Returns:
        The remote function; call it with ``.remote(...)``.
    """
    options: dict[str, Any] = {
        "num_cpus": num_cpus,
        "max_retries": 0,
        "runtime_env": RUNTIME_ENV,
    }
    if memory is not None:
        options["memory"] = int(memory)
    if max_calls is not None:
        options["max_calls"] = max_calls  # only ray.remote() takes it
    return ray.remote(**options)(fn)


def run_tasks(
    orch: Any,
    tasks: list[Any],
    hooks: Any,
    execution: Any,
    workers: int,
    progress: Any,
) -> list[Any]:
    """Run a study's tasks on Ray; return their results in task order.

    Args:
        orch: The orchestrator (main process).
        tasks: The :class:`~flexorchestrator.study.task.TaskSpec` s.
        hooks: The portable hooks (``study.task.study_hooks``).
        execution: The config's :class:`~flexorchestrator.study.config.Execution`.
        workers: Tasks running at once.
        progress: Advanced after each task; cancellation is checked often.

    Raises:
        TagExistsError: As ``study.task.prepare``.
        CommandCancelled: If the command is cancelled; running tasks are
            cancelled first.
    """
    from flexorchestrator.study import task as steps  # it imports the orchestrator

    ray = connect(execution.ray_address)
    fn = remote(
        ray,
        steps.execute,
        num_cpus=execution.resources.num_cpus,
        memory=execution.resources.memory,
        max_calls=execution.max_calls,
    )
    timeout = execution.task_timeout_s
    pending, running, results = deque(tasks), {}, []
    # The main process prepares and stores while the workers solve: up to
    # ``workers`` tasks are prepared ahead, so a freed slot is filled at once.
    prepared: deque = deque()
    bundles: dict[str, Any] = {}
    stopped = False

    def submit(work: Any, attempts: int) -> None:
        bundle = work.bundle
        if bundle.spec_hash not in bundles:
            bundles[bundle.spec_hash] = ray.put(bundle)
        ref = fn.remote(
            dataclasses.replace(work, bundle=None), bundles[bundle.spec_hash]
        )
        running[ref] = (work, attempts, time.monotonic())

    def record(result: Any) -> None:
        nonlocal stopped
        if steps.report(orch, progress, execution, results, result):
            stopped = True
            _cancel(ray, running)
            running.clear()

    def finish(work: Any, done: Any, attempts: int) -> Any:
        try:
            return steps.finish(orch, work, done, attempts)
        except Exception as exc:
            return steps.errored(work.task, steps.error_text(exc), attempts)

    def prepare_next() -> None:
        task = pending.popleft()
        if task.on_existing == "skip" and steps.stored(orch, task.tag):
            record(steps.skipped(task))
            return
        try:
            prepared.append(steps.prepare(orch, task, hooks))
        except TagExistsError:
            raise
        except Exception as exc:
            record(steps.errored(task, steps.error_text(exc), 1))

    try:
        while running or prepared or (pending and not stopped):
            progress.check()
            if stopped:
                prepared.clear()
            while prepared and len(running) < workers:
                submit(prepared.popleft(), 1)
            ahead = bool(pending) and not stopped and len(prepared) < workers
            if ahead and len(running) < workers:
                prepare_next()  # a free slot and nothing prepared
                continue
            if not running:
                continue  # nothing is left: the loop condition ends it
            if ahead:  # collect what's done, else prepare the next task
                ready, _ = ray.wait(list(running), num_returns=len(running), timeout=0)
                if not ready:
                    prepare_next()
            else:
                ready, _ = ray.wait(list(running), num_returns=1, timeout=POLL_S)
            for ref in ready:
                if ref not in running:
                    continue  # cancelled by on_error: stop
                work, attempts, _ = running.pop(ref)
                try:
                    done = ray.get(ref)
                except Exception as exc:  # the worker died, or Ray failed it
                    done = steps.Done(error=f"{type(exc).__name__}: {exc}")
                if steps.retryable(done) and attempts <= execution.retries:
                    submit(work, attempts + 1)
                    continue
                record(finish(work, done, attempts))
            if timeout is not None:
                now = time.monotonic()
                for ref, (work, attempts, started) in list(running.items()):
                    if ref not in running or now - started <= timeout:
                        continue  # cancelled by on_error: stop, or in time
                    ray.cancel(ref, force=True)
                    del running[ref]
                    outcome = SolveOutcome(
                        status="error",
                        termination="timeout",
                        message=f"killed after {timeout:g} s (task_timeout_s)",
                        wall_time=now - started,
                    )
                    record(finish(work, steps.Done(outcome=outcome), attempts))
    except BaseException:
        _cancel(ray, running)
        raise
    return sorted(results, key=lambda r: r.index)


def map_ordered(
    fn: Any,
    jobs: list[tuple[Any, ...]],
    *,
    workers: int,
    ray_address: str | None = None,
    progress: Any = None,
    **options: Any,
) -> list[Any]:
    """Run ``fn(*job)`` for every job on Ray, at most ``workers`` at once.

    Args:
        fn: A module-level function, importable on the workers.
        jobs: The positional arguments of each call.
        workers: Calls running at once.
        ray_address: The Ray to connect to (:func:`connect`).
        progress: Checked for cancellation while waiting, if given.
        **options: Passed to :func:`remote` (``num_cpus``, ``memory``,
            ``max_calls``).

    Returns:
        What each call returned, in job order.

    Raises:
        Exception: The first exception a job raised, after the jobs still
            running are cancelled; ``CommandCancelled`` likewise.
    """
    ray = connect(ray_address)
    remote_fn = remote(ray, fn, **options)
    pending = deque(enumerate(jobs))
    running: dict[Any, int] = {}
    results: dict[int, Any] = {}
    try:
        while pending or running:
            if progress is not None:
                progress.check()
            while pending and len(running) < workers:
                index, job = pending.popleft()
                running[remote_fn.remote(*job)] = index
            ready, _ = ray.wait(list(running), num_returns=1, timeout=POLL_S)
            for ref in ready:
                results[running.pop(ref)] = ray.get(ref)
    except BaseException:
        _cancel(ray, running)
        raise
    return [results[i] for i in range(len(jobs))]


def _cancel(ray: Any, refs: Any) -> None:
    """Kill every task in ``refs`` (``ray.cancel(force=True)``)."""
    for ref in list(refs):
        ray.cancel(ref, force=True)
