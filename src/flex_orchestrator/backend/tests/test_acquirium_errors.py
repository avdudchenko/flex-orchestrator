"""Acquirium backend error handling that a healthy local server can't trigger."""

from types import SimpleNamespace

import pytest
import requests

from flex_orchestrator.backend.acquirium import AcquiriumBackend
from flex_orchestrator.errors import StorageError

pytestmark = pytest.mark.unit


def http_error(status):
    response = requests.Response()
    response.status_code = status
    response._content = b"server says no"
    return requests.HTTPError(response=response)


def fake_client(error):
    def get(name):
        raise error

    return SimpleNamespace(
        client=SimpleNamespace(resolve_unit=lambda identifier: None),
        study=SimpleNamespace(get=get, define=get),
    )


def test_a_missing_study_is_not_an_error():
    assert not AcquiriumBackend(fake_client(http_error(404))).has_study("s")


def test_other_http_errors_become_storage_errors():
    backend = AcquiriumBackend(fake_client(http_error(500)))
    with pytest.raises(StorageError, match="Acquirium"):
        backend.has_study("s")
    with pytest.raises(StorageError, match="server says no"):
        backend.define_study("s")


def test_value_errors_become_storage_errors():
    backend = AcquiriumBackend(fake_client(ValueError("blank name")))
    with pytest.raises(StorageError, match="blank name"):
        backend.define_study("")


def test_a_study_made_elsewhere_is_found():
    found = SimpleNamespace(name="s")
    client = SimpleNamespace(
        client=SimpleNamespace(resolve_unit=lambda identifier: None),
        study=SimpleNamespace(get=lambda name: found),
    )
    backend = AcquiriumBackend(client)
    assert backend.has_study("s") and backend._study("s") is found


def test_connection_errors_while_looking_a_study_up_become_storage_errors():
    backend = AcquiriumBackend(fake_client(requests.ConnectionError("refused")))
    with pytest.raises(StorageError, match="refused"):
        backend.has_study("s")
