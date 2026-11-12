Storage
=======

See :doc:`../explanation/storage` for an overview.

Backends
--------

.. automodule:: flexorchestrator.backend

.. autoclass:: flexorchestrator.backend.AcquiriumBackend

.. autodata:: flexorchestrator.backend.base.Row

.. autoclass:: flexorchestrator.backend.base.StreamStore

What is stored
--------------

.. automodule:: flexorchestrator.data

.. automodule:: flexorchestrator.results

.. automodule:: flexorchestrator.spec_store

.. automodule:: flexorchestrator.assets

Export and import
-----------------

.. automodule:: flexorchestrator.archive

.. autodata:: flexorchestrator.archive.models.SCHEMA
.. autodata:: flexorchestrator.archive.export.PARTS
.. autodata:: flexorchestrator.archive.imports.IMPORTED_FROM

.. automodule:: flexorchestrator.archive.models
   :no-members:

The parts of the export file formats:

.. autoclass:: flexorchestrator.archive.models.DatasetColumn
.. autoclass:: flexorchestrator.archive.models.TimeIndex
.. autoclass:: flexorchestrator.archive.models.ResultSeries
.. autoclass:: flexorchestrator.archive.models.ResultScalar
.. autoclass:: flexorchestrator.archive.models.FitSeries

Verify
------

.. automodule:: flexorchestrator.verify
