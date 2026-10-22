"""Every example script runs from start to finish (PLAN.md §7, integration tier).

Each runs in its own process with its own Acquirium, so it doesn't share the
session's server.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1800)]

EXAMPLES = Path(__file__).parents[3] / "examples"


def run(script: Path, *args: str) -> str:
    done = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    return done.stdout


@pytest.mark.needs_highs
def test_ro_surrogates():
    pytest.importorskip("acquirium")
    pytest.importorskip("flexparameterize")
    out = run(EXAMPLES / "ro_surrogates" / "example.py")
    for tag in ("fixed_intensity", "linear_surrogate"):
        assert f"\n{tag}: $" in out
