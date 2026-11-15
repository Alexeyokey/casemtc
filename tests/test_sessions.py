"""S2: сессии, архив, изоляция контекста, идемпотентность, restart."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import backend.app as backend_app
from backend.archive import export_all
from backend.db import Database
from tests.conftest import GateEngine, RecordingEngine


def test_two_sessions_are_isolated(make_api):
    eng = RecordingEngine()
    api, _ = make_api(engine=eng)
    s1, t1 = api.new_session()
    s2, t2 = api.new_session()
    api.say(s1, t1, "Нужно считать электроэнергию на 5 объектах, меня зовут Анна Тестова")
    api.say(s2, t2, "Интересует телеметрия оборудования на заводе")
    req_s2 = eng.requests[-1]
    assert req_s2.session_id == s2
    assert req_s2.history == []
    assert req_s2.client_context.facts == {}
    assert req_s2.client_context.contact.full_name is None


def test_topic_change_keeps_history_within_session(make_api):
    eng = RecordingEngine()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    api.say(sid, tok, "Нужно считать электроэнергию на 2 объектах")
    api.say(sid, tok, "А ещё вопрос про телеметрию оборудования")
    api.say(sid, tok, "Вернёмся к учёту: сколько стоит?")
    last = eng.requests[-1]
    assert [m.role.value for m in last.history] == ["user", "assistant", "user", "assistant"]
    assert last.client_context.facts["масштаб"] == "2 объектах"


def test_new_dialog_closes_previous_and_starts_clean(make_api):
    eng = RecordingEngine()
    api, _ = make_api(engine=eng)
    s1, t1 = api.new_session()
    api.say(s1, t1, "Нужно считать электроэнергию на 7 объектах")
    s2, t2 = api.new_session(close=(s1, t1))
    # старая сессия закрыта, но в архиве
    assert api.get(f"/api/v1/sessions/{s1}", t1).json()["status"] == "closed"
    assert api.say(s1, t1, "ещё вопрос", expect=409)["error"]["code"] == "session_closed"
    api.say(s2, t2, "Здравствуйте, расскажите о продуктах")
    assert eng.requests[-1].history == [] and eng.requests[-1].client_context.facts == {}


def test_close_endpoint(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про учёт электроэнергии на складе")
    assert api.post(f"/api/v1/sessions/{sid}/close", tok).json()["status"] == "closed"
    assert len(api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 2


def test_repeat_request_id_does_not_duplicate(api):
    sid, tok = api.new_session()
    a = api.say(sid, tok, "Вопрос про учёт электроэнергии", request_id="rid-1")
    b = api.say(sid, tok, "Вопрос про учёт электроэнергии", request_id="rid-1")
    assert b["replayed"] is True and a["assistant_message_id"] == b["assistant_message_id"]
    assert len(api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 2


def test_request_id_reused_for_other_text_conflicts(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Первый вопрос про учёт", request_id="rid-1")
    assert api.say(sid, tok, "Другой текст", request_id="rid-1", expect=409)["error"]["code"] == "request_id_conflict"


def test_concurrent_turns_rejected(make_api):
    eng = GateEngine()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", api.say(sid, tok, "долгий __gate__ вопрос", "rid-a")))
    th.start()
    assert eng.entered.wait(5)
    err = api.say(sid, tok, "второй вопрос параллельно", "rid-b", expect=409)
    assert err["error"]["code"] == "turn_in_progress" and err["error"]["retryable"] is True
    # повтор той же отправки тоже не дублирует
    assert api.say(sid, tok, "долгий __gate__ вопрос", "rid-a", expect=409)["error"]["code"] == "turn_in_progress"
    eng.release.set()
    th.join(5)
    assert out["r"]["answer"]
    assert len(api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 2


def test_same_request_is_replayed_after_delayed_admission(make_api, monkeypatch):
    eng = RecordingEngine()
    api, app = make_api(engine=eng)
    sid, tok = api.new_session()
    chat = app.state.chat
    ready = threading.Event()
    resume = threading.Event()
    first_thread = None
    original_one = chat.db.one

    def pause_stale_lookup(sql, params=()):
        row = original_one(sql, params)
        if threading.current_thread() is first_thread and "SELECT * FROM turns WHERE" in sql:
            # The old admission read could return before a second caller
            # committed the same ID. The new read is inside the write transaction.
            ready.set()
            assert resume.wait(5)
        return row

    monkeypatch.setattr(chat.db, "one", pause_stale_lookup)

    def first_request():
        nonlocal first_thread
        first_thread = threading.current_thread()
        try:
            return chat.handle_turn(sid, tok, "same-id", "Вопрос про учёт")
        finally:
            ready.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_request)
        try:
            assert ready.wait(5)
            second = chat.handle_turn(sid, tok, "same-id", "Вопрос про учёт")
        finally:
            resume.set()
        assert first.result(timeout=5).assistant_message_id == second.assistant_message_id
    assert second.replayed
    assert len(eng.requests) == 1


def test_engine_failure_then_retry_same_request_id(make_api):
    class FlakyEngine(RecordingEngine):
        fail = True

        def consult(self, request):
            if self.fail and request.request_id == "rid-x":
                self.fail = False
                raise TimeoutError("upstream timeout api_key=sk-THISSHOULDNOTLEAK123456")
            return super().consult(request)

    eng = FlakyEngine()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    api.say(sid, tok, "Первый вопрос про учёт электроэнергии", "ok-1")
    err = api.say(sid, tok, "Второй вопрос про телеметрию", "rid-x", expect=502)
    assert err["error"]["code"] == "engine_error" and err["error"]["retryable"]
    ok = api.say(sid, tok, "Второй вопрос про телеметрию", "rid-x")
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert ok["assistant_message_id"] == msgs[-1]["id"]
    # в повторном вызове engine видит историю без дублей
    assert len(eng.requests[-1].history) == 2


def test_failed_turn_not_in_context_of_next_turn(make_api):
    class FailOnMarker(RecordingEngine):
        pass

    eng = FailOnMarker()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    api.say(sid, tok, "сломай __fail__ это", "f-1", expect=502)
    api.say(sid, tok, "Нормальный вопрос про учёт электроэнергии", "f-2")
    assert eng.requests[-1].history == []
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert msgs[0]["in_context"] is False  # в архиве, но не в контексте


def test_failed_request_cannot_be_retried_after_newer_turn(make_api):
    class FailOnce(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.failed = False

        def consult(self, request):
            if request.request_id == "old" and not self.failed:
                self.failed = True
                raise TimeoutError
            return super().consult(request)

    eng = FailOnce()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    api.say(sid, tok, "Старый вопрос", "old", expect=502)
    newer = api.say(sid, tok, "Новый вопрос", "new")
    error = api.say(sid, tok, "Старый вопрос", "old", expect=409)["error"]
    assert error["code"] == "turn_superseded" and not error["retryable"]
    assert "новое сообщение" in error["message"]

    api.say(sid, tok, "Старый вопрос", "resubmitted")
    assert [(msg.role.value, msg.content) for msg in eng.requests[-1].history] == [
        ("user", "Новый вопрос"), ("assistant", newer["answer"]),
    ]
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert [(msg["request_id"], msg["in_context"]) for msg in msgs] == [
        ("old", False), ("new", True), ("new", True), ("resubmitted", True), ("resubmitted", True),
    ]


def test_legacy_out_of_order_replies_are_grouped_in_engine_history(make_api):
    eng = RecordingEngine()
    api, app = make_api(engine=eng)
    sid, tok = api.new_session()
    older = api.say(sid, tok, "Старый вопрос", "old")
    newer = api.say(sid, tok, "Новый вопрос", "new")
    with app.state.db.tx() as conn:
        # Старый код мог дописать ответ на старую реплику после нового ответа.
        conn.execute("UPDATE messages SET seq=seq+4 WHERE session_id=?", (sid,))
        conn.executemany("UPDATE messages SET seq=? WHERE id=?", [
            (1, older["user_message_id"]), (2, newer["user_message_id"]),
            (3, newer["assistant_message_id"]), (4, older["assistant_message_id"]),
        ])
        conn.executemany("UPDATE turns SET finished_at=? WHERE session_id=? AND request_id=?", [
            ("2026-01-01T00:00:00.000+00:00", sid, "new"),
            ("2026-01-01T00:00:01.000+00:00", sid, "old"),
        ])
    api.say(sid, tok, "Ещё один вопрос", "last")
    assert [(msg.role.value, msg.content) for msg in eng.requests[-1].history] == [
        ("user", "Новый вопрос"), ("assistant", newer["answer"]),
        ("user", "Старый вопрос"), ("assistant", older["answer"]),
    ]


def test_foreign_session_is_not_accessible(api):
    s1, t1 = api.new_session()
    s2, t2 = api.new_session()
    api.say(s1, t1, "Вопрос про учёт электроэнергии")
    assert api.get(f"/api/v1/sessions/{s1}", t2).status_code == 404
    assert api.get(f"/api/v1/sessions/{s1}").status_code == 404
    assert api.say(s1, t2, "чужой", expect=404)["error"]["code"] == "session_not_found"
    assert api.get(f"/api/v1/sessions/{s1}/lead", t2).status_code == 404


def test_archive_survives_restart_without_lead(make_api, settings):
    api, app = make_api()
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про учёт электроэнергии без заявки")
    app.state.db.close()
    api2, _ = make_api()
    view = api2.get(f"/api/v1/sessions/{sid}", tok).json()
    assert len(view["messages"]) == 2 and view["lead"] is None
    rec = api2.c.get(f"/api/v1/archive/sessions/{sid}", headers={"X-Archive-Token": "test-archive-token"}).json()
    assert rec["lead"] is None and len(rec["messages"]) == 2


def test_pending_turn_recovered_after_restart(make_api, settings):
    api, app = make_api()
    sid, tok = api.new_session()
    db = app.state.db
    with db.tx() as c:  # имитируем падение процесса посреди хода
        c.execute("INSERT INTO turns(session_id, request_id, status, user_text, created_at) "
                  "VALUES (?,?,?,?,datetime('now'))", (sid, "crash-1", "pending", "вопрос до падения"))
        c.execute("INSERT INTO messages(session_id, seq, role, content, request_id, in_context, created_at) "
                  "VALUES (?,1,'user','вопрос до падения','crash-1',0,datetime('now'))", (sid,))
    db.close()
    api2, _ = make_api()
    r = api2.say(sid, tok, "вопрос до падения", "crash-1")
    assert r["answer"]
    msgs = api2.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_archive_api_requires_token(api):
    assert api.c.get("/api/v1/archive/sessions").status_code == 403
    assert api.c.get("/api/v1/archive/sessions", headers={"X-Archive-Token": "wrong"}).status_code == 403


def test_archive_disabled_without_token(make_api):
    api, _ = make_api(archive_token=None)
    r = api.c.get("/api/v1/archive/sessions", headers={"X-Archive-Token": "anything"})
    assert r.status_code == 403 and "backend.archive" in r.json()["error"]["message"]


def test_cli_export(make_api, settings, tmp_path):
    api, app = make_api()
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про учёт электроэнергии для экспорта")
    res = export_all(Database(settings.db_path), tmp_path / "exp")
    assert res["sessions"] == 1
    rec = json.loads((tmp_path / "exp" / "sessions.jsonl").read_text("utf-8").splitlines()[0])
    assert rec["session_id"] == sid and len(rec["messages"]) == 2
    assert "Консультант" in (tmp_path / "exp" / "sessions" / f"{sid}.md").read_text("utf-8")


def test_jsonl_export_endpoint(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Вопрос про учёт электроэнергии")
    r = api.c.get("/api/v1/archive/export.jsonl", headers={"X-Archive-Token": "test-archive-token"})
    lines = [json.loads(x) for x in r.text.strip().splitlines()]
    assert lines[0]["session_id"] == sid


def test_cancel_turn_keeps_archive_but_not_context(make_api):
    eng = GateEngine()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    out = {}
    th = threading.Thread(target=lambda: out.setdefault(
        "r", api.c.post(f"/api/v1/sessions/{sid}/messages", headers={"X-Client-Token": tok},
                        json={"request_id": "c-1", "text": "отменяемый __gate__ вопрос"})))
    th.start()
    assert eng.entered.wait(5)
    assert api.post(f"/api/v1/sessions/{sid}/turns/c-1/cancel", tok).json()["status"] == "cancelled"
    eng.release.set()
    th.join(5)
    assert out["r"].status_code == 409 and out["r"].json()["error"]["code"] == "turn_cancelled"
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert len(msgs) == 2 and not any(m["in_context"] for m in msgs)


@pytest.mark.parametrize("outcome", ["success", "storage_error", "close"])
def test_cancel_or_close_after_generation_wins_before_commit(make_api, monkeypatch, outcome):
    api, app = make_api()
    sid, tok = api.new_session()
    before_commit = threading.Event()
    resume = threading.Event()
    persist = app.state.chat._persist_result

    def pause_persist(*args, **kwargs):
        before_commit.set()
        assert resume.wait(5)
        if outcome == "storage_error":
            raise OSError("synthetic persist failure")
        return persist(*args, **kwargs)

    monkeypatch.setattr(app.state.chat, "_persist_result", pause_persist)
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(api.say, sid, tok, "Свяжите со специалистом", "late", 409)
        try:
            assert before_commit.wait(5)
            if outcome == "close":
                assert api.post(f"/api/v1/sessions/{sid}/close", tok).status_code == 200
            else:
                cancelled = api.post(f"/api/v1/sessions/{sid}/turns/late/cancel", tok)
                assert cancelled.json()["status"] == "cancelled"
        finally:
            resume.set()
        assert turn.result(timeout=5)["error"]["code"] == (
            "session_closed" if outcome == "close" else "turn_cancelled"
        )

    assert api.post(f"/api/v1/sessions/{sid}/turns/late/cancel", tok).json()["status"] == "cancelled"
    trace = api.get(f"/api/v1/sessions/{sid}/turns/late/debug", tok).json()
    assert trace["status"] == "cancelled"
    if outcome == "storage_error":
        assert trace["error"]["stage"] == "backend_persist"
    view = api.get(f"/api/v1/sessions/{sid}", tok).json()
    assert view["lead"] is None
    assert not any(message["in_context"] for message in view["messages"])


def test_engine_error_does_not_overwrite_cancellation(make_api):
    class FailingGate(GateEngine):
        def consult(self, request):
            super().consult(request)
            raise TimeoutError("synthetic engine timeout")

    eng = FailingGate()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(api.say, sid, tok, "__gate__ вопрос", "fail-late", 409)
        try:
            assert eng.entered.wait(5)
            cancelled = api.post(f"/api/v1/sessions/{sid}/turns/fail-late/cancel", tok)
            assert cancelled.json()["status"] == "cancelled"
        finally:
            eng.release.set()
        assert turn.result(timeout=5)["error"]["code"] == "turn_cancelled"
    assert api.post(f"/api/v1/sessions/{sid}/turns/fail-late/cancel", tok).json()["status"] == "cancelled"
    trace = api.get(f"/api/v1/sessions/{sid}/turns/fail-late/debug", tok).json()
    assert trace["status"] == "cancelled" and trace["error"]["stage"] == "engine"


def test_sse_stream(api):
    sid, tok = api.new_session()
    r = api.c.post(f"/api/v1/sessions/{sid}/messages/stream", headers={"X-Client-Token": tok},
                   json={"request_id": "sse-1", "text": "Вопрос про учёт электроэнергии"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [b.split("\n")[0].removeprefix("event: ") for b in r.text.strip().split("\n\n")]
    assert events == ["accepted", "final"]


def test_sse_accepted_turn_can_be_cancelled_immediately(make_api, monkeypatch):
    eng = GateEngine()
    api, app = make_api(engine=eng)
    sid, tok = api.new_session()
    serialize = backend_app._sse

    def cancel_at_acceptance(event, data):
        if event == "accepted":
            try:
                assert app.state.chat.cancel_turn(sid, data["request_id"]) == "cancelled"
            finally:
                eng.release.set()
        return serialize(event, data)

    monkeypatch.setattr(backend_app, "_sse", cancel_at_acceptance)
    stream = api.post(f"/api/v1/sessions/{sid}/messages/stream", tok,
                      json={"request_id": "sse-cancel", "text": "Вопрос __gate__ про учёт"})
    assert stream.status_code == 200
    assert "event: accepted" in stream.text and "event: error" in stream.text
    assert "turn_cancelled" in stream.text and "event: final" not in stream.text


def test_sse_rejected_admission_never_emits_accepted(api):
    sid, tok = api.new_session()
    api.post(f"/api/v1/sessions/{sid}/close", tok)
    stream = api.post(f"/api/v1/sessions/{sid}/messages/stream", tok,
                      json={"request_id": "sse-closed", "text": "Вопрос после закрытия"})
    assert stream.status_code == 200
    assert "session_closed" in stream.text and "event: accepted" not in stream.text


def test_retry_after_cancel_does_not_duplicate(make_api):
    eng = GateEngine()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    with ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(2):
            eng.entered.clear()
            eng.release.clear()
            turn = pool.submit(api.say, sid, tok, "вопрос __gate__ про учёт", "c-2", 409)
            try:
                assert eng.entered.wait(5)
                assert api.post(f"/api/v1/sessions/{sid}/turns/c-2/cancel", tok).json()["status"] == "cancelled"
            finally:
                eng.release.set()
            assert turn.result(timeout=5)["error"]["code"] == "turn_cancelled"
    r = api.say(sid, tok, "вопрос __gate__ про учёт", "c-2")
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert len(msgs) == 2 and all(m["in_context"] for m in msgs) and r["assistant_message_id"] == msgs[1]["id"]
    trace = api.get(f"/api/v1/sessions/{sid}/turns/c-2/debug", tok).json()
    assert trace["attempt"] == 3 and trace["status"] == "completed"
