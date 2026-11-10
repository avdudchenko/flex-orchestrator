"""Exceptions raised by flex-orchestrator.

Every error subclasses :class:`FlexOrchestratorError`, so callers can catch the
base class for any orchestrator failure or a concrete subclass for one category.
Messages say what went wrong and how to fix it.
"""


class FlexOrchestratorError(Exception):
    """Base class for all flex-orchestrator errors."""


class HookError(FlexOrchestratorError):
    """A build/solve hook can't be resolved or broke its contract.

    Raised for a malformed or unimportable import string, an unknown registered
    hook name, a hook slot that isn't set, or a solve hook that returns
    something other than a :class:`~flexorchestrator.hooks.SolveOutcome`.
    """


class SpecError(FlexOrchestratorError):
    """A spec, its tag or assets, or a path into it is invalid.

    Raised for a spec that isn't a JSON object or fails its format's
    validation, a spec stored as the wrong kind, an invalid tag, a stored spec
    whose hash doesn't match, a configuration-option path or asset pointer
    that doesn't resolve, and a surrogate aimed at a unit the spec doesn't
    have. (A bad override path in the default build raises flex-pse's own
    error.)
    """


class ExportError(FlexOrchestratorError):
    """Model values can't be exported.

    Raised for a key that names nothing, ambiguous or inconsistent time sets,
    two values that would be stored under the same key, or a named component
    with non-numeric values.
    """


class UnitError(FlexOrchestratorError):
    """A unit can't be mapped to QUDT (in strict mode) or converted."""


class StorageError(FlexOrchestratorError):
    """The storage backend refused a write or read.

    Raised for a label already declared with another kind, a write to a run
    that isn't running, a timeseries without datetimes, and errors reported by
    Acquirium.
    """


class DBAuthError(StorageError):
    """Postgres refused the database login of a managed Acquirium.

    The message names the redacted ``user@host:port/db`` and never the password.
    """


class StudyExistsError(FlexOrchestratorError):
    """``create_study`` was given the name of a study that exists."""


class StudyNotFoundError(FlexOrchestratorError, LookupError):
    """The study doesn't exist, or no study is selected."""


class TagExistsError(FlexOrchestratorError):
    """An item with this kind and tag is already stored in the study.

    A run that failed doesn't count, so a failed tag can be stored again.
    """


class TagNotFoundError(FlexOrchestratorError, LookupError):
    """Nothing is stored in the study under this kind and tag."""


class AmbiguousColumnError(FlexOrchestratorError, LookupError):
    """A column name looked up across datasets is in more than one."""


class StudyConfigError(FlexOrchestratorError):
    """A study config (``StudyConfig``) is invalid or can't be expanded.

    Raised for a config that fails validation, an ``output_name`` that renders
    an invalid or repeated tag, sweeps of different lengths with
    ``combine: zip``, and execution options this version can't run.
    """


class FitError(FlexOrchestratorError):
    """A surrogate can't be fitted from the data selected.

    Raised for a selector that matches nothing (or, for ``resolve``, more than
    one stream), data that can't be aligned or has gaps, units that don't
    convert, and a regressor that can't be resolved or fails.
    """


class ModelInputError(FlexOrchestratorError):
    """Stored data can't be set on a model variable.

    Raised for a ``model_inputs`` entry whose selector matches nothing, whose
    data has a gap its ``gaps`` rule can't fill, or that a model without a time
    axis can't take, and for two entries (or an entry and a ``variable`` sweep)
    setting one path.
    """


class CommandError(FlexOrchestratorError):
    """A command can't be found, registered or run.

    Raised for an unknown command, a name registered twice, and parameters that
    don't validate against the command's params model.
    """


class CommandCancelled(CommandError):
    """A command stopped because its ``Progress`` was cancelled."""


class PluginError(FlexOrchestratorError):
    """A plugin can't be loaded.

    Raised for a plugin whose ``api_version`` doesn't include this version's
    ``PLUGIN_API_VERSION``, a name that isn't installed, and collisions between
    plugins (command or hook names, or two orchestrator subclasses).
    """


class ArchiveError(FlexOrchestratorError):
    """A study export can't be written or read back.

    Raised for an export into a folder that isn't empty, a file whose schema
    version this version doesn't read, and a checksum that doesn't match.
    """
