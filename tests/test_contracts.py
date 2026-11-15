"""S1: контракты валидны, fixtures соответствуют схемам, ядро не зависит от HTTP/CSV."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from core.contracts import ActionType, ConsultationResult, Evidence
from core.engine import ConsultationEngine, load_engine
from core.phone import extract_phone, mask_phone, normalize_phone

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "contracts" / "fixtures").rglob("*.json"))
SCHEMA_FOR = {
    "core/request": "ConsultationRequest", "core/result": "ConsultationResult",
    "http/turn_request": "TurnRequest", "http/turn_response": "TurnResponse",
    "http/contacts_update": "ContactsUpdate", "http/lead_view": "LeadView",
    "http/trace_view": "TraceView", "http/error": "ErrorResponse",
}


def _schema_name(path: Path) -> str:
    rel = path.relative_to(ROOT / "contracts" / "fixtures").as_posix()
    for prefix, name in SCHEMA_FOR.items():
        if rel.startswith(prefix):
            return name
    raise AssertionError(f"no schema mapping for {rel}")


def test_contracts_dir_is_up_to_date():
    r = subprocess.run([sys.executable, "scripts/gen_contracts.py", "--check"], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, "contracts/ устарели — запустите scripts/gen_contracts.py\n" + r.stdout


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_matches_json_schema(path):
    schema = json.loads((ROOT / "contracts" / "schemas" / f"{_schema_name(path)}.schema.json").read_text("utf-8"))
    jsonschema.validate(json.loads(path.read_text("utf-8")), schema)


def test_required_fixture_coverage():
    names = {p.stem for p in FIXTURES}
    # только телефон, отказ от контактов, обычный ответ без сырых файлов, отдельный debug
    assert {"result_lead_phone_only", "result_contacts_refused", "result_answer_plain",
            "result_answer_debug", "trace_view", "lead_view_saved_phone_only"} <= names


def test_plain_answer_fixture_has_no_raw_sources():
    d = json.loads((ROOT / "contracts/fixtures/core/result_answer_plain.json").read_text("utf-8"))
    assert d["retrieved"] == []
    assert "synthetic/" not in d["answer"] and ".md" not in d["answer"]


def test_phone_only_fixture_does_not_invent_name_or_company():
    d = json.loads((ROOT / "contracts/fixtures/http/lead_view_saved_phone_only.json").read_text("utf-8"))
    assert d["contact"]["phone"] and d["contact"]["full_name"] is None and d["contact"]["company"] is None
    assert d["delivery"] == "stored_only"
    assert "уведомл" not in d["status_text"].lower()


def test_used_must_be_subset_of_retrieved():
    ev = Evidence(evidence_id="a", doc_id="d", text="t")
    with pytest.raises(ValidationError):
        ConsultationResult(session_id="s", request_id="r", answer="x", action=ActionType.answer,
                           retrieved=[ev], used_evidence_ids=["b"])


def test_clarify_requires_question():
    with pytest.raises(ValidationError):
        ConsultationResult(session_id="s", request_id="r", answer="x", action=ActionType.clarify)


def test_core_has_no_http_csv_or_db_dependency():
    banned = {"fastapi", "starlette", "httpx", "requests", "csv", "sqlite3", "backend"}
    for py in (ROOT / "core").glob("*.py"):
        tree = ast.parse(py.read_text("utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for m in mods:
                assert m.split(".")[0] not in banned, f"{py.name} imports {m}"


def test_default_engine_satisfies_protocol():
    assert isinstance(load_engine(), ConsultationEngine)


@pytest.mark.parametrize("raw,expected", [
    ("+7 (916) 123-45-67", "+79161234567"),
    ("89161234567", "+79161234567"),
    ("9161234567", "+79161234567"),
    ("+44 20 7946 0958", "+442079460958"),
    ("12345", None),
    ("+7 916 123", None),
    ("71234567890", None),
    ("", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_extract_and_mask_phone():
    assert extract_phone("звоните 8 (999) 000-00-01 после обеда") == "+79990000001"
    assert extract_phone("заказ 12345") is None
    assert mask_phone("+79990000001") == "+7999***0001"
