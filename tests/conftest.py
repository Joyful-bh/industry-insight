import uuid
from pathlib import Path

import pytest


@pytest.fixture
def workspace_tmp_path() -> Path:
    path = Path(".runtime-tests") / "cases" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path
