"""Exceptions raised by flex-orchestrator.

Every error subclasses :class:`FlexOrchestratorError`, so callers can catch the
base class for any orchestrator failure or a concrete subclass for one category.
Messages say what went wrong and how to fix it.
"""


class FlexOrchestratorError(Exception):
    """Base class for all flex-orchestrator errors."""


class HookError(FlexOrchestratorError):
    """A build/solve hook can't be resolved or broke its contract (PLAN.md D1).

    Raised for a malformed or unimportable import string, an unknown registered
    hook name, a hook slot that isn't set, or a solve hook that returns
    something other than a :class:`~flex_orchestrator.hooks.SolveOutcome`.
    """


class SpecError(FlexOrchestratorError):
    """A model spec, override or surrogate target doesn't match the model.

    Raised for an override path that names no component (or one that can't be
    set), and for a surrogate swap aimed at a unit the spec or model doesn't
    have.
    """


class ExportError(FlexOrchestratorError):
    """Model values can't be exported (PLAN.md D3).

    Raised for a key that names nothing, ambiguous or inconsistent time sets,
    two values that would be stored under the same key, or a named component
    with non-numeric values.
    """


class UnitError(FlexOrchestratorError):
    """A unit can't be mapped to QUDT (in strict mode) or converted."""


class StorageError(FlexOrchestratorError):
    """The storage backend refused a write or read (PLAN.md D10).

    Raised for a label already declared with another kind, a write to a run
    that isn't running, a timeseries without datetimes, and errors reported by
    Acquirium.
    """


class DBAuthError(StorageError):
    """Postgres refused the database login of a managed Acquirium (PLAN.md D11).

    The message names the redacted ``user@host:port/db`` and never the password.
    """


class StudyExistsError(FlexOrchestratorError):
    """``create_study`` was given the name of a study that exists (PLAN.md D9)."""


class StudyNotFoundError(FlexOrchestratorError, LookupError):
    """The study doesn't exist, or no study is selected (PLAN.md D9)."""


class TagExistsError(FlexOrchestratorError):
    """An item with this kind and tag is already stored in the study (PLAN.md D9).

    A run that failed doesn't count, so a failed tag can be stored again.
    """


class TagNotFoundError(FlexOrchestratorError, LookupError):
    """Nothing is stored in the study under this kind and tag."""


class AmbiguousColumnError(FlexOrchestratorError, LookupError):
    """A column name looked up across datasets is in more than one (PLAN.md D6)."""


class StudyConfigError(FlexOrchestratorError):
    """A study config (``StudyConfig``) is invalid or can't be expanded (PLAN.md M4).

    Raised for a config that fails validation, an ``output_name`` that renders
    an invalid or repeated tag, sweeps of different lengths with
    ``combine: zip``, and execution options this version can't run.
    """


class FitError(FlexOrchestratorError):
    """A surrogate can't be fitted from the data selected (PLAN.md D19).

    Raised for a selector that matches nothing (or, for ``resolve``, more than
    one stream), data that can't be aligned or has gaps, units that don't
    convert, and a regressor that can't be resolved or fails.
    """


class CommandError(FlexOrchestratorError):
    """A command can't be found, registered or run (PLAN.md D18).

    Raised for an unknown command, a name registered twice, and parameters that
    don't validate against the command's params model.
    """


class CommandCancelled(CommandError):
    """A command stopped because its ``Progress`` was cancelled (PLAN.md D18)."""


class PluginError(FlexOrchestratorError):
    """A plugin can't be loaded (PLAN.md D18).

    Raised for a plugin whose ``api_version`` doesn't include this version's
    ``PLUGIN_API_VERSION``, a name that isn't installed, and collisions between
    plugins (command or hook names, or two orchestrator subclasses).
    """


class ArchiveError(FlexOrchestratorError):
    """A study export can't be written or read back (PLAN.md D17).

    Raised for an export into a folder that isn't empty, a file whose schema
    version this version doesn't read, and a checksum that doesn't match.
    """
