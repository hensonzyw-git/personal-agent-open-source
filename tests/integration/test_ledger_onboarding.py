"""DEV-016: the onboarding command end to end, against fixture files.

Uses the committed synthetic config and snapshot, so the whole path -- load
config, parse a Feishu-shaped snapshot, validate, report -- runs without any
credential or Base.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_data_mcp.finance.onboarding import run


FIXTURES = Path(__file__).parent.parent / "fixtures" / "ledger"
CONFIG = FIXTURES / "config.synthetic.json"
SNAPSHOT = FIXTURES / "snapshot.synthetic.json"


def test_a_matching_snapshot_exits_zero(capsys) -> None:
    code = run(CONFIG, SNAPSHOT, redacted=False)
    assert code == 0
    assert "status: valid" in capsys.readouterr().out


def test_a_drifted_snapshot_exits_non_zero(tmp_path, capsys) -> None:
    snapshot = json.loads(SNAPSHOT.read_text("utf-8"))
    # Rename the amount field in the observed snapshot.
    snapshot["expense"][0]["field_name"] = "金额"
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(snapshot), "utf-8")

    code = run(CONFIG, drifted, redacted=False)
    assert code == 1
    assert "status: drifted" in capsys.readouterr().out


def test_redacted_mode_emits_committable_json(capsys) -> None:
    code = run(CONFIG, SNAPSHOT, redacted=True)
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "valid"
    # The raw ids in the fixture files must not appear in the report.
    text = json.dumps(report, ensure_ascii=False)
    assert "bascnSYNTHETIC000001" not in text
    assert "fldSYNAMOUNT" not in text
