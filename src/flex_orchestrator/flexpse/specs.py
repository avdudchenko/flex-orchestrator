"""Validators for flex-pse specs (see :func:`flex_orchestrator.specs.validate_spec`)."""

from flexcore.config.io import load_spec
from flexcore.config.schema import SurrogateSpec
from flexcore.exceptions import FlexConfigError
from pydantic import ValidationError

from flex_orchestrator.errors import SpecError


def validate_model_spec(spec: dict) -> dict:
    """Check a flowsheet spec with flex-pse's own loader, and normalise it.

    Args:
        spec: A ``FlowsheetSpec`` as JSON.

    Returns:
        The validated spec as JSON, with defaults written out and any older
        schema migrated by flex-pse. Fields are written under their aliases
        (``{"$source": ...}``), the form flex-pse itself writes.

    Raises:
        SpecError: If flex-pse rejects it; the message names the bad field.
    """
    try:
        return load_spec(spec).model_dump(mode="json", by_alias=True)
    except FlexConfigError as exc:
        raise SpecError(f"Invalid flex-pse flowsheet spec: {exc}") from exc


def validate_surrogate_spec(spec: dict) -> dict:
    """Check a surrogate spec against flex-pse's ``SurrogateSpec`` and normalise it.

    Args:
        spec: A ``SurrogateSpec`` as JSON.

    Returns:
        The validated spec as JSON, with defaults written out.

    Raises:
        SpecError: If it doesn't validate.
    """
    try:
        return SurrogateSpec.model_validate(spec).model_dump(mode="json", by_alias=True)
    except ValidationError as exc:
        raise SpecError(f"Invalid flex-pse surrogate spec: {exc}") from exc
