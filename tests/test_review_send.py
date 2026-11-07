"""`daily_review.py send` must not report success when `hermes send` delivered nothing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "initiatives" / "scripts"))
daily_review = pytest.importorskip("daily_review")


def _proc(code=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


def test_skipped_duplicate_target_is_an_error():
    skipped = {"success": True, "skipped": True, "reason": "cron_auto_delivery_duplicate_target"}
    assert daily_review._send_error(_proc(out=json.dumps(skipped))) == "cron_auto_delivery_duplicate_target"


def test_failed_result_and_nonzero_exit_are_errors():
    assert daily_review._send_error(_proc(out=json.dumps({"success": False, "error": "chat not found"}))) == "chat not found"
    assert daily_review._send_error(_proc(code=1, err="boom")) == "boom"


def test_delivered_result_is_not_an_error():
    assert daily_review._send_error(_proc(out=json.dumps({"success": True, "message_id": "2776"}))) is None
