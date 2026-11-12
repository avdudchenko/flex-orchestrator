# Installation

flex-pse isn't on PyPI yet, so install from the conda environment files in the
repository. They install flex-pse, Acquirium and the solvers, then this
package:

```
git clone https://github.com/flex-pse/flex-orchestrator.git
cd flex-orchestrator
conda env create -f environment.yml        # deployment: pinned upstream commits
conda env create -f environment-dev.yml    # development: tests, linters and docs
idaes get-extensions                       # IPOPT, if you need it
```

`environment.yml` pins flex-pse and Acquirium to fixed git commits, so two
installs made from the same file get the same versions. `environment-dev.yml`
pins the same versions and adds the test and documentation tools.

## Optional parts

The package's extras pull in what each optional part needs:

| Extra | For |
| --- | --- |
| `acquirium` | The Acquirium backend (installed by both environment files). |
| `flexpse` | The default build and solve hooks, on flex-pse. |
| `ray` | Running studies and fits in parallel (`max_workers > 1`). |
| `pyomo` | The model data exporter, without the rest of flex-pse. |
| `docs` | Building this documentation. |
| `dev` | Tests and linters. |

For example, `pip install -e ".[ray]"` adds Ray to an existing environment.

## Building these docs

```
pip install -e ".[docs]"
sphinx-build -W --keep-going -b html docs docs/_build
```
