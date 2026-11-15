"""The preview and export use the contacts from the same lead revision."""

from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from backend.rag_engine import RagEngine
from knowledge_base import KnowledgeBase
from organizer_api import APIConfig


NAME = "Иван Примеров"
NAME_LABELS = {"ФИО", "ФИО контактного лица"}
CONTACT_LABELS = NAME_LABELS | {"Телефон", "Компания", "Регион"}


@pytest.fixture(params=["stub", "rag"])
def lead_api(request, tmp_path, make_api):
    engine = None
    if request.param == "rag":
        archive_path = tmp_path / "knowledge-base.zip"
        with ZipFile(archive_path, "w") as archive:
            archive.writestr("metering.md", "# Учёт счётчиков\n\nПроверьте совместимость счётчиков.")

        def unexpected_generation(_request):
            pytest.fail("Сбор контактов по явному запросу не должен вызывать модель.")

        engine = RagEngine(KnowledgeBase.from_zip(archive_path),
                           APIConfig("https://fixture.invalid/v1", "synthetic", "synthetic-model"),
                           transport=httpx.MockTransport(unexpected_generation))
    api, _ = make_api(engine=engine)
    return api, request.param


@pytest.mark.parametrize("cleared_name", [None, ""], ids=["null", "empty"])
def test_preview_and_saved_revisions_follow_optional_name(lead_api, settings, cleared_name):
    api, engine = lead_api
    session, token = api.new_session()
    path = f"/api/v1/sessions/{session}/lead"

    def contacts(fields):
        response = api.put(path + "/contacts", token, json=fields)
        assert response.status_code == 200, response.text
        return response.json()

    def save(offer, key):
        response = api.post(path + "/save", token, json={
            "offer_id": offer["offer_id"], "idempotency_key": key,
        })
        assert response.status_code == 200, response.text
        return response.json()

    def markdown():
        response = api.get(path + ".md", token)
        assert response.status_code == 200, response.text
        return response.text

    reply = api.say(session, token, "Нужен учёт электроэнергии. Свяжите со специалистом.")
    assert reply["action"] == "collect_contacts"
    initial = reply["lead"]["summary"]
    other_unknowns = set(initial["unknowns"]) - CONTACT_LABELS
    if engine == "stub":
        assert "Масштаб (количество объектов)" in other_unknowns

    phone_only = contacts({"phone": "+7 999 000-00-31"})
    assert phone_only["status"] == "ready" and phone_only["contact"]["full_name"] is None
    assert NAME_LABELS.intersection(phone_only["summary"]["unknowns"])
    first = save(phone_only, "phone-only")
    assert first["revision"] == 1 and first["summary"] == phone_only["summary"]
    first_markdown = markdown()
    assert "- ФИО: не указано" in first_markdown

    named = contacts({"full_name": NAME, "company": "ООО Пример", "region": "Регион Пример"})
    assert named["status"] == "ready" and named["offer_id"] != phone_only["offer_id"]
    assert named["contact"]["full_name"] == NAME and named["missing_fields"] == []
    assert named["summary"]["task"] == initial["task"]
    assert set(named["summary"]["unknowns"]) == other_unknowns
    assert markdown() == first_markdown

    second = save(named, "named")
    assert second["lead_id"] == first["lead_id"] and second["revision"] == 2
    assert second["summary"] == named["summary"]
    second_markdown = markdown()
    assert f"- ФИО: {NAME}" in second_markdown and "- Компания: ООО Пример" in second_markdown
    assert "- Регион: Регион Пример" in second_markdown
    assert not CONTACT_LABELS.intersection(line.removeprefix("- ") for line in second_markdown.splitlines())
    assert all(f"- {unknown}" in second_markdown for unknown in other_unknowns)
    assert save(named, "named") == second
    same_contacts = contacts({"phone": "8 (999) 000-00-31", "full_name": NAME})
    assert same_contacts["status"] == "saved" and same_contacts["revision"] == 2
    assert same_contacts["offer_id"] == named["offer_id"] and markdown() == second_markdown

    cleared = contacts({"full_name": cleared_name})
    assert cleared["status"] == "ready" and cleared["offer_id"] != named["offer_id"]
    assert cleared["contact"]["full_name"] is None and cleared["missing_fields"] == ["ФИО"]
    assert NAME_LABELS.intersection(cleared["summary"]["unknowns"])
    assert set(cleared["summary"]["unknowns"]) - NAME_LABELS == other_unknowns
    assert markdown() == second_markdown

    third = save(cleared, "cleared")
    assert third["lead_id"] == first["lead_id"] and third["revision"] == 3
    assert third["summary"] == cleared["summary"]
    third_markdown = markdown()
    assert "- ФИО: не указано" in third_markdown and NAME not in third_markdown
    assert Path(settings.leads_dir, f"{third['lead_id']}.md").read_text("utf-8") == third_markdown
