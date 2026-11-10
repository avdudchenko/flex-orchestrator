"""Model inputs from stored data.

A ``model_inputs`` entry (:class:`ModelInput`) is a fit selector plus how its
data lines up with the model's time steps; :func:`resolve` turns entries into
build overrides and a provenance record.
"""

from flexorchestrator.inputs.models import InputResample, InputSet, ModelInput

__all__ = ["InputResample", "InputSet", "ModelInput"]
