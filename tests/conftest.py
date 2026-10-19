import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw03():
    return json.loads((FIX / "raw_03_amount_mismatch.json").read_text())


@pytest.fixture
def raw04():
    return json.loads((FIX / "raw_04_name_typo_invalid_rc.json").read_text())


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    d = tmp_path / "audit"
    monkeypatch.setenv("EXTRACTOR_AUDIT_DIR", str(d))
    return d
