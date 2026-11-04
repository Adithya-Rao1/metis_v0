# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pytest  # noqa: E402
from _fixtures import build_1m17_like  # noqa: E402


@pytest.fixture(scope="session")
def pdb_1m17() -> str:
    return build_1m17_like()
