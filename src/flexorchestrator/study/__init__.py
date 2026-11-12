"""Declarative studies: ``StudyConfig`` and the ``execute_study`` command.

Example:
    >>> run = orch.execute_study({  # doctest: +SKIP
    ...     "base_model_spec": "base",
    ...     "sweeps": [{"type": "surrogate", "unit": "plant.ro",
    ...                 "surrogates": ["ro_average", "ro_linear"]}],
    ...     "output_name": "{surrogate}",
    ... })
    >>> run.counts()  # doctest: +SKIP
    {'succeeded': 2}
"""

from flexorchestrator.study.config import (
    Case,
    CasesSweep,
    ConfigurationSweep,
    Execution,
    ExportConfig,
    HooksConfig,
    ModelInputsSweep,
    SpecListSweep,
    StudyConfig,
    SurrogateSweep,
    VariableSweep,
    load_config,
)
from flexorchestrator.study.runner import ExecuteParams, StudyRun, expand
from flexorchestrator.study.task import SurrogateChoice, TaskResult, TaskSpec, run_task

__all__ = [
    "Case",
    "CasesSweep",
    "ConfigurationSweep",
    "ExecuteParams",
    "Execution",
    "ExportConfig",
    "HooksConfig",
    "ModelInputsSweep",
    "SpecListSweep",
    "StudyConfig",
    "StudyRun",
    "SurrogateChoice",
    "SurrogateSweep",
    "TaskResult",
    "TaskSpec",
    "VariableSweep",
    "expand",
    "load_config",
    "run_task",
]
