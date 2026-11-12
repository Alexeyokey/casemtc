"""S3: заявка — полный набор, только телефон, неверный/исправленный, отказ, идемпотентность."""
from __future__ import annotations

from pathlib import Path

BASE = "/api/v1/sessions/{}/lead"
FORBIDDEN_CLAIMS = ("уведомлен", "отправлен", "менеджер свяжется", "позвонит")


def _no_false_delivery(text: str):
    low = text.lower()
    assert not any(c in low for c in FORBIDDEN_CLAIMS), text


def test_full_contact_set(api, settings):
    sid, tok = api.new_session()
    api.say(sid, tok, "Нужно считать электроэнергию на 4 объектах")
    v = api.put(BASE.format(sid) + "/contacts", tok, json={
        "full_name": "Иван Тестов", "phone": "8 (999) 000-00-02", "company": "ООО Вымысел", "region": "Тестовая обл."
    }).json()
    assert v["status"] == "ready" and v["contact"]["phone"] == "+79990000002" and v["missing_fields"] == []
    saved = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k1"}).json()
    assert saved["status"] == "saved" and saved["delivery"] == "stored_only" and saved["revision"] == 1
    _no_false_delivery(saved["status_text"])
    md = Path(settings.leads_dir, f"{saved['lead_id']}.md").read_text("utf-8")
    assert "+79990000002" in md and "ООО Вымысел" in md and "CRM" in md


def test_phone_only_via_chat(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Хочу учёт электроэнергии на 3 объектах")
    r = api.say(sid, tok, "Свяжите со специалистом")
    assert r["action"] == "collect_contacts" and r["lead"]["status"] == "draft"
    r = api.say(sid, tok, "Мой телефон +7 999 000-00-03")
    assert r["action"] == "lead_ready" and r["lead"]["status"] == "ready"
    lead = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k"}).json()
    c = lead["contact"]
    assert c == {"full_name": None, "phone": "+79990000003", "company": None, "region": None}
    assert "Компания" in lead["missing_fields"] and "ФИО" in lead["missing_fields"]
    assert "3 объектах" in " ".join(lead["summary"]["known_facts"])


def test_invalid_then_corrected_phone(api):
    sid, tok = api.new_session()
    r = api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "12-34"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_phone"
    assert api.get(BASE.format(sid), tok).json()["contact"]["phone"] is None
    assert api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k"}).status_code == 422
    fixed = api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+7 999 000 00 04"}).json()
    assert fixed["status"] == "ready"
    # исправление до сохранения
    fixed = api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+7 999 000 00 05"}).json()
    saved = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k"}).json()
    assert saved["contact"]["phone"] == "+79990000005"


def test_invalid_phone_in_chat_is_not_accepted(api):
    sid, tok = api.new_session()
    r = api.say(sid, tok, "Перезвоните мне, номер 123-45")
    assert r["action"] == "collect_contacts"
    assert api.get(BASE.format(sid), tok).json()["contact"]["phone"] is None


def test_full_refusal_keeps_consulting(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Интересует учёт электроэнергии")
    r = api.say(sid, tok, "Не хочу оставлять телефон")
    assert r["action"] == "answer"
    v = api.post(BASE.format(sid) + "/refuse", tok).json()
    assert v["contact_refused"] is True and v["status"] in {"none", "draft"}
    _no_false_delivery(v["status_text"])
    r = api.say(sid, tok, "Расскажите тогда про телеметрию оборудования")
    assert r["answer"]
    err = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k"})
    assert err.status_code == 422 and err.json()["error"]["code"] == "phone_required"
    assert len(api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 6


def test_repeat_save_no_duplicate_and_revision(api, settings):
    sid, tok = api.new_session()
    api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+79990000006"})
    a = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "same"}).json()
    b = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "same"}).json()
    c = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "other"}).json()
    assert a["lead_id"] == b["lead_id"] == c["lead_id"]
    assert a["revision"] == b["revision"] == c["revision"] == 1
    # изменение контакта после сохранения → новая ревизия той же заявки
    api.put(BASE.format(sid) + "/contacts", tok, json={"full_name": "Пётр Тестов"})
    d = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "third"}).json()
    assert d["lead_id"] == a["lead_id"] and d["revision"] == 2 and d["contact"]["full_name"] == "Пётр Тестов"
    leads = api.c.get("/api/v1/archive/leads", headers={"X-Archive-Token": "test-archive-token"}).json()
    assert len(leads) == 1
    assert len(list(Path(settings.leads_dir).glob("*.md"))) == 1


def test_lead_survives_restart(make_api):
    api, app = make_api()
    sid, tok = api.new_session()
    api.say(sid, tok, "Свяжите со специалистом по учёту, телефон 89990000007")
    api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k"})
    app.state.db.close()
    api2, _ = make_api()
    v = api2.get(BASE.format(sid), tok).json()
    assert v["status"] == "saved" and v["contact"]["phone"] == "+79990000007"
    assert len(api2.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 2


def test_summary_does_not_invent_company_budget_or_promises(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Свяжите со специалистом, телефон +79990000008")
    lead = api.post(BASE.format(sid) + "/save", tok, json={"idempotency_key": "k"}).json()
    s = lead["summary"]
    text = " ".join([s["task"], *s["known_facts"], *s["discussed_solutions"], s["next_step"]]).lower()
    assert "бюджет" not in text and "руб" not in text and "скидк" not in text
    assert lead["contact"]["company"] is None
    md = api.get(f"/api/v1/sessions/{sid}/lead.md", tok).text
    assert "Компания: не указано" in md


def test_contacts_feed_back_into_engine_context(make_api):
    from tests.conftest import RecordingEngine
    eng = RecordingEngine()
    api, _ = make_api(engine=eng)
    sid, tok = api.new_session()
    api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+79990000009", "company": "АО Пример"})
    api.say(sid, tok, "Что дальше?")
    ctx = eng.requests[-1].client_context
    assert ctx.contact.phone == "+79990000009" and ctx.contact.company == "АО Пример"
