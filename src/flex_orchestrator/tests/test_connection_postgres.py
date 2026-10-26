"""Mode 2's checks against a real Postgres (PLAN.md D11, integration tier).

Opt in with ``FLEXORCH_TEST_PG`` (a URL without the password, for a role that
can create tables) and ``FLEXORCH_TEST_PG_PASSWORD``. With TimescaleDB
installed the full preflight passes and binds the database; without it the
preflight stops at the extension check. The binding table is dropped after
each test.
"""

import os

import pytest
from pydantic import SecretStr

from flex_orchestrator.connection import (
    BINDING_TABLE,
    postgres_dsn,
    preflight,
    redact,
)
from flex_orchestrator.errors import DBAuthError, StorageError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get("FLEXORCH_TEST_PG")
            and os.environ.get("FLEXORCH_TEST_PG_PASSWORD")
        ),
        reason="set FLEXORCH_TEST_PG and FLEXORCH_TEST_PG_PASSWORD to a Postgres",
    ),
]


@pytest.fixture
def dsn():
    dsn = postgres_dsn(os.environ["FLEXORCH_TEST_PG"])
    yield dsn
    import psycopg

    with psycopg.connect(dsn, password=password().get_secret_value()) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {BINDING_TABLE}")


def password() -> SecretStr:
    return SecretStr(os.environ["FLEXORCH_TEST_PG_PASSWORD"])


def has_timescaledb(dsn) -> bool:
    import psycopg

    with psycopg.connect(dsn, password=password().get_secret_value()) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"
            ).fetchone()
            is not None
        )


def test_a_wrong_password_is_a_db_auth_error(dsn, tmp_path):
    wrong = "not-the-password-" + password().get_secret_value()[:3]
    with pytest.raises(DBAuthError) as info:
        preflight(dsn, SecretStr(wrong), tmp_path)
    assert wrong not in str(info.value)
    assert redact(dsn) in str(info.value)


def test_the_preflight_on_a_real_server(dsn, tmp_path):
    if not has_timescaledb(dsn):
        with pytest.raises(StorageError, match="timescaledb"):
            preflight(dsn, password(), tmp_path)
        return
    preflight(dsn, password(), tmp_path / "a")
    preflight(dsn, password(), tmp_path / "a")  # the same data_dir again
    with pytest.raises(StorageError, match="belongs to the managed Acquirium"):
        preflight(dsn, password(), tmp_path / "b")
