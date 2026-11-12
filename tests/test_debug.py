"""S4: трассы, retrieved vs used, маскирование, провал этапа."""
from __future__ import annotations

import json
import logging
import sys

from uvicorn.logging import AccessFormatter, DefaultFormatter

from backend.redaction import RedactingFilter, install_log_redaction, redact, redact_text
from core.contracts import ActionType, ConsultationResult, Evidence, StageTiming
from core.stub_engine import StubEngine

DBG = "/api/v1/sessions/{}/turns/{}/debug"


def test_trace_belongs_to_selected_answer(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про показания счётчиков", "q-1")
    api.say(sid, tok, "Теперь про удалённый мониторинг оборудования", "q-2")
    t1 = api.get(DBG.format(sid, "q-1"), tok).json()
    t2 = api.get(DBG.format(sid, "q-2"), tok).json()
    assert t1["request_id"] == "q-1" and t2["request_id"] == "q-2"
    assert t1["used"][0]["evidence_id"] != t2["used"][0]["evidence_id"]


def test_retrieved_and_used_are_separate(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про отчёты и показания счётчиков", "q")
    t = api.get(DBG.format(sid, "q"), tok).json()
    rid = {e["evidence_id"] for e in t["retrieved"]}
    uid = {e["evidence_id"] for e in t["used"]}
    assert uid and uid <= rid and t["used_missing_ids"] == []
    names = [s["name"] for s in t["timings"]]
    assert {"engine.retrieval", "engine.generation", "engine", "total"} <= set(names)
    assert all(s["ms"] >= 0 for s in t["timings"])


def test_user_answer_has_no_raw_sources(api):
    sid, tok = api.new_session()
    r = api.say(sid, tok, "Вопрос про отчёты и показания счётчиков")
    assert "synthetic/" not in r["answer"] and "evidence" not in json.dumps(r)


def test_failed_stage_visible_and_secret_masked(make_api):
    class LeakyFail(StubEngine):
        def consult(self, request):
            raise ConnectionError("POST https://llm/v1 failed: Authorization: Bearer abcdefghijklmnop123\n"
                                  "Cookie: session=super-secret-session; another=also-secret\n"
                                  "api_key=sk-SECRETSECRETSECRET client phone +7 999 000-00-10")

    api, _ = make_api(engine=LeakyFail())
    sid, tok = api.new_session()
    api.say(sid, tok, "Мой телефон +7 999 000-00-11, вопрос", "f", expect=502)
    t = api.get(DBG.format(sid, "f"), tok).json()
    dumped = json.dumps(t, ensure_ascii=False)
    assert t["status"] == "failed" and t["error"]["stage"] == "engine"
    assert any(s["name"] == "engine" and s["status"] == "error" for s in t["timings"])
    for secret in ("abcdefghijklmnop123", "super-secret-session", "also-secret", "sk-SECRET", "9990000010"):
        assert secret not in dumped


def test_used_id_missing_is_reported(make_api):
    class BadUsed(StubEngine):
        def consult(self, request):
            # retrieved пуст (debug не дал), used ссылается в никуда — дефект engine
            return ConsultationResult(session_id=request.session_id, request_id=request.request_id,
                                      answer="ответ", action=ActionType.answer, used_evidence_ids=["ghost#1"],
                                      timings=[StageTiming(name="generation", ms=1.0)])

    api, _ = make_api(engine=BadUsed())
    sid, tok = api.new_session()
    api.say(sid, tok, "вопрос про что-нибудь", "b")
    t = api.get(DBG.format(sid, "b"), tok).json()
    assert t["used"] == [] and t["used_missing_ids"] == ["ghost#1"]


def test_debug_disabled(make_api):
    api, _ = make_api(debug_api=False)
    sid, tok = api.new_session()
    r = api.say(sid, tok, "вопрос про отчёты", "d")
    assert r["debug_available"] is False
    assert api.get(DBG.format(sid, "d"), tok).status_code == 403


def test_debug_requires_session_owner(api):
    s1, t1 = api.new_session()
    s2, t2 = api.new_session()
    api.say(s1, t1, "вопрос про отчёты", "o")
    assert api.get(DBG.format(s1, "o"), t2).status_code == 404


def test_redaction_unit():
    s = redact_text("Authorization: Bearer abc.def.ghi123 token=xyz12345 mail a.b@example.com tel 89161234567")
    assert "abc.def" not in s and "xyz12345" not in s and "example.com" not in s and "1234567" not in s
    d = redact({"headers": {"Authorization": "Bearer x", "X-Client-Token": "t"}, "phone": "+79161234567",
                "nested": ["api_key: sk-abcdefghijklmnopq"]})
    assert d["headers"]["Authorization"] == "[REDACTED]" and d["headers"]["X-Client-Token"] == "[REDACTED]"
    assert d["phone"] == "+7916***4567" and "abcdefghijkl" not in d["nested"][0]
    # обычный текст и номера заказов не портятся
    assert redact_text("Продукт А для 3 объектов, заказ 12345") == "Продукт А для 3 объектов, заказ 12345"


def test_log_filter_masks():
    rec = logging.LogRecord("casemtc", logging.INFO, __file__, 1, "call %s with %s", ("x", "Bearer qwertyuiop12"), None)
    RedactingFilter().filter(rec)
    assert "qwertyuiop12" not in rec.getMessage()


def test_log_filter_redacts_formatted_headers_and_nested_args(caplog):
    install_log_redaction()
    logger = logging.getLogger("casemtc.client")
    logger.warning("request\nCookie: %s\nstatus: %d", "session=raw-cookie; second=raw-second", 429)
    logger.warning("payload=%(payload)r", {"payload": {"Proxy-Authorization": "raw-proxy",
                   "headers": [(b"Set-Cookie", b"session=raw-set-cookie; HttpOnly"),
                               ("X-CSRF-Token", "raw-csrf"), ("Accept", "application/json")]}})
    logger.warning({"Password": "raw-password", "status": "unchanged"})
    for secret in ("raw-cookie", "raw-second", "raw-proxy", "raw-set-cookie", "raw-csrf", "raw-password"):
        assert secret not in caplog.text
    assert "Cookie: [REDACTED]\nstatus: 429" in caplog.text
    assert "application/json" in caplog.text and "unchanged" in caplog.text


def test_log_filter_masks_full_cookie_lines_and_sensitive_query_params():
    original = ("Set-Cookie: session=raw-cookie; Path=/; SameSite=Lax\n"
                "Proxy-Authorization=Basic raw-proxy\n"
                "response: {'X-Archive-Token': 'raw-archive', 'status': 'ok'}\n"
                "headers: [(b'Cookie', b'session=raw-bytes-cookie')]\n"
                "GET /?x-client-token=raw-client&name=public")
    masked = redact_text(original)
    assert "raw-" not in masked
    assert "'status': 'ok'" in masked and "&name=public" in masked


def test_evidence_text_survives_redaction_in_trace(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про отчёты и показания счётчиков", "e")
    t = api.get(DBG.format(sid, "e"), tok).json()
    assert t["used"][0]["text"].startswith("Продукт А")


def test_log_filter_keeps_uvicorn_args_shape_and_masks_traceback():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:1", "POST", "/x?token=abcdefgh123", "1.1", 200), None)
    RedactingFilter().filter(rec)
    assert len(rec.args) == 5 and "abcdefgh123" not in rec.getMessage()
    access = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    formatted = access.format(rec)
    assert "POST /x?token=[REDACTED] HTTP/1.1" in formatted and "200 OK" in formatted
    try:
        raise RuntimeError("Bearer zzzzzzzzzzzz9999 failed\nCookie: session=raw-error; another=raw-other")
    except RuntimeError:
        rec2 = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "boom %s", ("for user",),
                                 sys.exc_info(), sinfo="GET /?x-archive-token=raw-stack&trace=1")
    rec2.color_message = "boom %s\nCookie: color=raw-color"
    RedactingFilter().filter(rec2)
    assert rec2.exc_info is None and "zzzzzzzz" not in rec2.exc_text and "RuntimeError" in rec2.exc_text
    formatter = DefaultFormatter("%(levelprefix)s %(message)s", use_colors=True)
    formatted = formatter.format(rec2)
    assert "boom for user" in formatted and "RuntimeError" in formatted and "&trace=1" in formatted
    assert "raw-" not in formatted
