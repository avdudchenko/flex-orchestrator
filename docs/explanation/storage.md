# What is stored, and moving it

## The layout of a study

Every item is stored explicitly. Each call names the model and the tag, and
there is no "current model". A study `s` is laid out as several backend
studies:

| Backend study | Each run is |
| --- | --- |
| `s` | The study itself. |
| `s::runs` | A stored result: the model snapshot, any values stored natively, and the solve outcome. |
| `s::data` | A dataset: a table of measured data, with units. |
| `s::specs` | A model or surrogate spec, with the files it references. |
| `s::fits` | A surrogate fit, with where its training data came from. |

Each run's metadata also records the package versions, the user, the hooks
and the connection, so a result can be reproduced.

## Specs and their files

A spec is any JSON object. Storing it as data, not as a live model, means it
can be rebuilt in another process, on a Ray worker, or later on.

A flex-pse flowsheet can point at files, such as a tariff or a
demand-response events file. `store_model_spec` stores those files with the
spec. Its hash, `spec_hash`, covers their contents too, so two specs that
reference different tariffs never look the same.

- `store_model_spec(tag, spec, base_dir=...)` reads the referenced files from
  disk.
- `store_model_spec(tag, spec, files={"tariff.csv": data})` takes their bytes
  instead and never reads the disk. A host application that received the files
  over HTTP uses this. The stored spec and its `spec_hash` are the same as
  from disk.
- `model_spec(tag, with_files=True).files` gives the files back.

## Export and import

A study, or a single result, exports to a folder of JSON files that need no
database to read:

```
flex-orch --data-dir .flexorch export my_study export/my_study [--compress]
flex-orch --data-dir .flexorch import export/my_study --name copy
```

```python
orch.export_study("exports/example_a")
orch.import_study("exports/example_a", name="example_a_copy")

from flexorchestrator.archive import load_result
res = load_result("exports/example_a/results/base.json")
```

The export is written to a temporary folder and then renamed, so a failed
export never leaves a half-written folder behind. Import checks every
checksum and the schema version before it stores anything. Each file's format
has a JSON Schema in `docs/schemas/`.

## Checking a store

`flex-orch verify` reports each study's counts, tags and value hashes, along
with any problems:

- a tag stored twice;
- a run still marked `running`;
- an item whose values can't be read back.

Save its output before a restore or a move. Afterwards, compare against it:

```
flex-orch verify > before.json
# ... restore, migrate or move the store ...
flex-orch verify --against before.json
```

`--against` exits 1 on any difference in the succeeded runs, their tags or
their values. `verify` also exits 1 whenever the store has a problem.
