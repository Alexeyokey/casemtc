"""S3: заявка — полный набор, только телефон, неверный/исправленный, отказ, идемпотентность."""
from __future__ import annotations

from pathlib import Path
from threading import Thread

BASE = "/api/v1/sessions/{}/lead"
FORBIDDEN_CLAIMS = ("уведомлен", "отправлен", "менеджер свяжется", "позвонит")
MISSING_OFFER = "0" * 64


def _save(api, sid, token, key, offer=None):
    if offer is None:
        offer = api.get(BASE.format(sid), token).json()["offer_id"] or MISSING_OFFER
    return api.post(BASE.format(sid) + "/save", token, json={"idempotency_key": key, "offer_id": offer})


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
    saved = _save(api, sid, tok, "k1", v["offer_id"]).json()
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
    lead = _save(api, sid, tok, "k", r["lead"]["offer_id"]).json()
    c = lead["contact"]
    assert c == {"full_name": None, "phone": "+79990000003", "company": None, "region": None}
    assert "Компания" in lead["missing_fields"] and "ФИО" in lead["missing_fields"]
    assert "3 объектах" in " ".join(lead["summary"]["known_facts"])


def test_invalid_then_corrected_phone(api):
    sid, tok = api.new_session()
    r = api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "12-34"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_phone"
    assert api.get(BASE.format(sid), tok).json()["contact"]["phone"] is None
    assert _save(api, sid, tok, "k").status_code == 422
    fixed = api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+7 999 000 00 04"}).json()
    assert fixed["status"] == "ready"
    # исправление до сохранения
    fixed = api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+7 999 000 00 05"}).json()
    saved = _save(api, sid, tok, "k", fixed["offer_id"]).json()
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
    err = _save(api, sid, tok, "k")
    assert err.status_code == 409 and err.json()["error"]["code"] == "contacts_refused"
    assert len(api.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 6


def test_repeat_save_no_duplicate_and_revision(api, settings):
    sid, tok = api.new_session()
    api.put(BASE.format(sid) + "/contacts", tok, json={"phone": "+79990000006"})
    a = _save(api, sid, tok, "same").json()
    b = _save(api, sid, tok, "same", a["offer_id"]).json()
    c = _save(api, sid, tok, "other", a["offer_id"]).json()
    assert a["lead_id"] == b["lead_id"] == c["lead_id"]
    assert a["revision"] == b["revision"] == c["revision"] == 1
    # изменение контакта после сохранения → новая ревизия той же заявки
    api.put(BASE.format(sid) + "/contacts", tok, json={"full_name": "Пётр Тестов"})
    d = _save(api, sid, tok, "third").json()
    assert d["lead_id"] == a["lead_id"] and d["revision"] == 2 and d["contact"]["full_name"] == "Пётр Тестов"
    leads = api.c.get("/api/v1/archive/leads", headers={"X-Archive-Token": "test-archive-token"}).json()
    assert len(leads) == 1
    assert len(list(Path(settings.leads_dir).glob("*.md"))) == 1


def test_lead_survives_restart(make_api):
    api, app = make_api()
    sid, tok = api.new_session()
    api.say(sid, tok, "Свяжите со специалистом по учёту, телефон 89990000007")
    assert _save(api, sid, tok, "k").status_code == 200
    app.state.db.close()
    api2, _ = make_api()
    v = api2.get(BASE.format(sid), tok).json()
    assert v["status"] == "saved" and v["contact"]["phone"] == "+79990000007"
    assert len(api2.get(f"/api/v1/sessions/{sid}", tok).json()["messages"]) == 2


def test_summary_does_not_invent_company_budget_or_promises(api):
    sid, tok = api.new_session()
    api.say(sid, tok, "Свяжите со специалистом, телефон +79990000008")
    lead = _save(api, sid, tok, "k").json()
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


def test_saved_snapshot_changes_only_after_new_preview_is_confirmed(api, settings):
    sid, tok = api.new_session()
    route = BASE.format(sid)
    original_phone, new_phone = "+79990000011", "+79990000022"
    api.put(route + "/contacts", tok, json={"phone": original_phone})
    first = _save(api, sid, tok, "original").json()
    file = Path(settings.leads_dir, f"{first['lead_id']}.md")

    updated = api.put(route + "/contacts", tok, json={"phone": new_phone}).json()
    assert updated["status"] == "ready" and updated["contact"]["phone"] == new_phone
    api.say(sid, tok, "Уточню задачу: нужен учёт по арендаторам бизнес-центра")
    latest = api.get(route, tok).json()
    assert latest["status"] == "ready" and latest["revision"] == 1
    assert latest["offer_id"] != first["offer_id"]
    assert original_phone in api.get(route + ".md", tok).text
    assert original_phone in file.read_text("utf-8")

    stale = _save(api, sid, tok, "new", first["offer_id"])
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_offer"
    conflicting = _save(api, sid, tok, "original", latest["offer_id"])
    assert conflicting.status_code == 409 and conflicting.json()["error"]["code"] == "lead_request_conflict"
    second = _save(api, sid, tok, "new", latest["offer_id"]).json()
    assert second["lead_id"] == first["lead_id"] and second["revision"] == 2
    assert _save(api, sid, tok, "original", first["offer_id"]).json() == first
    assert new_phone in file.read_text("utf-8")


def test_existing_phone_cannot_be_saved_after_refusal(api):
    sid, tok = api.new_session()
    route = BASE.format(sid)
    phone = "+79990000011"
    offer = api.put(route + "/contacts", tok, json={"phone": phone}).json()["offer_id"]
    refused = api.post(route + "/refuse", tok).json()
    assert refused["contact_refused"] and refused["contact"]["phone"] is None and refused["offer_id"] is None
    response = _save(api, sid, tok, "stale-after-refusal", offer)
    assert response.status_code == 409 and response.json()["error"]["code"] == "contacts_refused"
    api.say(sid, tok, "Хочу продолжить консультацию про учёт")
    assert api.get(route, tok).json()["contact"]["phone"] is None
    new_offer = api.put(route + "/contacts", tok, json={"phone": phone}).json()["offer_id"]
    assert _save(api, sid, tok, "explicit-confirm", new_offer).status_code == 200


def test_idempotent_retry_repairs_failed_markdown_projection(make_api, tmp_path):
    blocked = tmp_path / "blocked-lead-directory"
    blocked.write_text("fixture", encoding="utf-8")
    api, _ = make_api(leads_dir=str(blocked))
    sid, tok = api.new_session()
    route = BASE.format(sid)
    phone = "+79990000011"
    offer = api.put(route + "/contacts", tok, json={"phone": phone}).json()["offer_id"]
    missing_file = _save(api, sid, tok, "recover-file", offer)
    assert missing_file.status_code == 503 and missing_file.json()["error"]["retryable"]
    assert phone in api.get(route + ".md", tok).text
    blocked.unlink()

    replay = _save(api, sid, tok, "recover-file", offer).json()
    assert replay["revision"] == 1
    assert phone in (blocked / f"{replay['lead_id']}.md").read_text("utf-8")


def test_contact_form_edit_wins_over_turn_started_with_old_contact(make_api):
    from tests.conftest import GateEngine

    engine = GateEngine()
    api, _ = make_api(engine=engine)
    sid, tok = api.new_session()
    route = BASE.format(sid)
    api.put(route + "/contacts", tok, json={"phone": "+79990000011"})
    out = {}
    worker = Thread(target=lambda: out.setdefault("reply", api.say(sid, tok, "Вопрос __gate__ про учёт", "gate")))
    worker.start()
    try:
        assert engine.entered.wait(5)
        api.put(route + "/contacts", tok, json={"phone": "+79990000022"})
    finally:
        engine.release.set()
        worker.join(5)
    assert out["reply"]["lead"]["contact"]["phone"] == "+79990000022"
    assert api.get(route, tok).json()["contact"]["phone"] == "+79990000022"
