"""S5: сквозные сценарии через HTTP API (на синтетическом engine).

Это backend-часть приёмки. Обязательный web-путь через UI Катрины проверяется
отдельно по docs/acceptance.md после интеграции U2/U3/U4.
"""
from __future__ import annotations

import uuid

from tests.conftest import RecordingEngine


def test_specialist_request_phone_without_company_to_saved_lead(make_api, settings):
    api, _ = make_api()
    sid, tok = api.new_session()
    r1 = api.say(sid, tok, "Нужно считать электроэнергию на 3 объектах, что подойдёт?")
    assert r1["action"] == "answer" and r1["lead"] is None
    r2 = api.say(sid, tok, "Свяжите меня со специалистом")
    assert r2["action"] == "collect_contacts" and r2["lead"]["status"] == "draft"
    r3 = api.say(sid, tok, "+7 999 000-00-21")
    assert r3["action"] == "lead_ready" and r3["lead"]["status"] == "ready"
    preview = api.get(f"/api/v1/sessions/{sid}/lead", tok).json()
    assert preview["contact"]["company"] is None and "Компания" in preview["missing_fields"]
    key = str(uuid.uuid4())
    confirmation = {"idempotency_key": key, "offer_id": preview["offer_id"]}
    saved = api.post(f"/api/v1/sessions/{sid}/lead/save", tok, json=confirmation).json()
    again = api.post(f"/api/v1/sessions/{sid}/lead/save", tok, json=confirmation).json()
    assert saved == again and saved["status"] == "saved"
    md = api.get(f"/api/v1/sessions/{sid}/lead.md", tok).text
    assert "+79990000021" in md and "3 объектах" in md and "CRM, почта и уведомления не подключены" in md
    # менеджер/проверяющий видит заявку и полный диалог
    h = {"X-Archive-Token": "test-archive-token"}
    assert [l["lead_id"] for l in api.c.get("/api/v1/archive/leads", headers=h).json()] == [saved["lead_id"]]
    assert len(api.c.get(f"/api/v1/archive/sessions/{sid}", headers=h).json()["messages"]) == 6


def test_refusal_continue_and_full_history_saved(make_api):
    api, app = make_api()
    sid, tok = api.new_session()
    api.say(sid, tok, "Интересует удалённый мониторинг оборудования")
    r = api.say(sid, tok, "Не буду оставлять телефон, просто ответьте")
    assert r["action"] == "answer"
    r = api.say(sid, tok, "Подходит ли это для показаний счётчиков?")
    assert r["answer"]
    lead = api.get(f"/api/v1/sessions/{sid}/lead", tok).json()
    assert lead["contact_refused"] and lead["status"] != "saved"
    assert "менеджер не сможет связаться" in lead["status_text"] or "Консультацию можно продолжать" in lead["status_text"]
    api.post(f"/api/v1/sessions/{sid}/close", tok)
    app.state.db.close()
    api2, _ = make_api()
    rec = api2.c.get(f"/api/v1/archive/sessions/{sid}", headers={"X-Archive-Token": "test-archive-token"}).json()
    assert rec["status"] == "closed" and len(rec["messages"]) == 6
    assert rec["lead"] is None or rec["lead"]["status"] == "draft"


def test_topic_change_new_dialog_restart(make_api):
    eng = RecordingEngine()
    api, app = make_api(engine=eng)
    s1, t1 = api.new_session()
    api.say(s1, t1, "Учёт электроэнергии на 9 объектах")
    api.say(s1, t1, "А теперь про телеметрию оборудования")
    s2, t2 = api.new_session(close=(s1, t1))
    api.say(s2, t2, "Здравствуйте, что у вас есть?")
    assert eng.requests[-1].history == [] and "масштаб" not in eng.requests[-1].client_context.facts
    app.state.db.close()
    api2, _ = make_api(engine=eng)
    assert api2.get(f"/api/v1/sessions/{s1}", t1).json()["status"] == "closed"
    api2.say(s2, t2, "Продолжим после перезапуска, про учёт")
    assert len(eng.requests[-1].history) == 2  # только своя сессия


def test_lost_response_retry_and_db_error(make_api):
    api, app = make_api()
    sid, tok = api.new_session()
    # клиент не получил ответ (обрыв) и повторяет с тем же request_id
    a = api.say(sid, tok, "Вопрос про отчёты по счётчикам", "lost-1")
    b = api.say(sid, tok, "Вопрос про отчёты по счётчикам", "lost-1")
    assert a["assistant_message_id"] == b["assistant_message_id"] and b["replayed"]
    # ошибка БД при сохранении ответа не оставляет полузаписанного хода
    chat = app.state.chat
    orig = chat.leads.sync_from_engine

    def boom(*a, **k):
        raise RuntimeError("disk I/O error")

    chat.leads.sync_from_engine = boom
    r = api.c.post(f"/api/v1/sessions/{sid}/messages", headers={"X-Client-Token": tok},
                   json={"request_id": "db-1", "text": "Вопрос во время сбоя БД"})
    chat.leads.sync_from_engine = orig
    assert r.status_code == 500 and r.json()["error"]["code"] == "storage_error"
    assert r.json()["error"]["retryable"] is True
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert [m["role"] for m in msgs if m["in_context"]] == ["user", "assistant"]
    assert api.get(f"/api/v1/sessions/{sid}/turns/db-1/debug", tok).json()["error"]["stage"] == "backend_persist"
    # повтор сразу, без перезапуска, без дублей
    api.say(sid, tok, "Вопрос во время сбоя БД", "db-1")
    msgs = api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]
    assert len(msgs) == 4 and all(m["in_context"] for m in msgs)


def test_cannot_access_foreign_session_anywhere(api):
    s1, t1 = api.new_session()
    _, t2 = api.new_session()
    api.say(s1, t1, "Свяжитесь со мной, +79990000022")
    for method, path in [("get", f"/api/v1/sessions/{s1}"), ("get", f"/api/v1/sessions/{s1}/lead"),
                         ("get", f"/api/v1/sessions/{s1}/lead.md"), ("post", f"/api/v1/sessions/{s1}/close"),
                         ("post", f"/api/v1/sessions/{s1}/lead/refuse")]:
        r = getattr(api, method)(path, t2)
        assert r.status_code == 404, path
        assert "+7999" not in r.text
