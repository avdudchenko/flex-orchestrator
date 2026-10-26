# A tenant's Ray cluster

Studies with `max_workers > 1` run on Ray (PLAN.md D13). A small tenant uses a
local Ray inside its orchestrator container, which is the default and needs
nothing from this folder. A larger tenant gets **its own** Ray cluster, which
this folder templates. A Ray cluster is never shared between tenants, because
its workers run the tenant's hook and plugin code (D14).

| File | For |
| --- | --- |
| [`docker-compose.yml`](docker-compose.yml) | a head and N workers on the tenant's Docker network |
| [`raycluster.yaml`](raycluster.yaml) | a KubeRay `RayCluster` and its `NetworkPolicy` in the tenant's namespace |

Point the orchestrator at the cluster:

```python
orch.execute_study(cfg, max_workers=16, ray_address="ray://ray-head:10001")
```

You can also set `execution.ray_address` in the config, pass `flex-orch run
--ray-address`, or set `FLEXORCH_RAY_ADDRESS` (D16, the tenant stack's
environment).

## What the cluster needs

- **The same software as the orchestrator.** Use the tenant's orchestrator
  image (`deploy/docker`, M6), so that flex-orchestrator, flex-pse, the
  solvers and every hook or plugin package match. Workers import hooks by
  name (`"mypkg.models:build"`). A hook defined in a script you run can't be
  sent, and `execute_study` refuses it before anything runs.
- **No credentials.** Workers receive the spec, its files and the hooks'
  names, and return the exported values. The orchestrator stores every
  result, so the cluster never sees Acquirium or the database.
- **Isolation.**
  - Containers run as non-root, with a read-only root filesystem (Ray writes
    to `/tmp` and `/dev/shm`) and no Docker socket.
  - The cluster is reachable only on the tenant's internal network, with no
    published ports.
  - The Kubernetes `NetworkPolicy` lets Ray pods talk only to each other and
    to the tenant's pods.
- **One thread per solver library.** `OMP/MKL/OPENBLAS_NUM_THREADS=1` is set
  on the nodes. The orchestrator also sets it in each task's `runtime_env`.

## Sizing

Each task reserves `execution.resources.num_cpus` (1 by default) and,
optionally, `memory` (bytes). Ray runs as many tasks at once as fit, up to
`max_workers`. Set `execution.max_calls` to restart a worker process after
that many tasks, if a solver leaks memory. The M5 benchmark
(`docs/decisions/0001-ray-execution.md`) has measured memory per worker and
time per task.

## Status

These templates aren't tested yet. The M5 benchmark ran on local Ray on one
Windows machine, and the 2-node run waits for a Linux host with Docker. The
tenant network and image they reference arrive with M6 (`deploy/tenant`,
`deploy/docker`).
