from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The moment the fixture responses were captured (2026-11-07). Buckets that had
# not been used yet reported a reset exactly one window after this.
CAPTURED_AT = datetime(2026, 11, 7, 9, 41, 57, tzinfo=timezone.utc)


def load_fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def summary_after_use():
    return load_fixture("summary_after_use.json")


@pytest.fixture
def load_code_assist_pro():
    return load_fixture("load_code_assist_pro.json")


@pytest.fixture
def captured_at():
    return CAPTURED_AT
