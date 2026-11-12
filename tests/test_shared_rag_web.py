import asyncio
import csv
import json
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.rag_engine import RagEngine
from backend.settings import Settings
from consultation import parse_csv_history
from knowledge_base import KnowledgeBase
from organizer_api import APIConfig, OrganizerClient
from rag_consultation import RagConsultant
from solution import run_submission


@pytest.fixture
def rag_fixture(tmp_path):
    archive = tmp_path / "knowledge-base.zip"
    with ZipFile(archive, "w") as target:
        target.writestr("metering.md", "# Учёт счётчиков арендаторов\n\nПроверьте совместимость счётчиков.")
    answer = "Начните с проверки совместимости счётчиков."
    calls = []

    def model(request):
        data = json.loads(request.content)
        payload = json.loads(data["messages"][-1]["content"])
        calls.append(payload)
        result = {"text": answer, "action": "answer", "summary": None,
                  "evidence_ids": [payload["evidence"][0]["id"]]}
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(result)}},
        ]})

    return (KnowledgeBase.from_zip(archive), APIConfig("https://fixture.invalid/v1", "synthetic", "synthetic-model"),
            httpx.MockTransport(model), answer, calls)


def test_web_and_csv_share_rag_and_web_confirms_a_local_lead(tmp_path, rag_fixture):
    knowledge, config, transport, answer, calls = rag_fixture
    question = "С чего начать учёт показаний счётчиков арендаторов?"
    settings = Settings(db_path=str(tmp_path / "chat.sqlite3"), leads_dir=str(tmp_path / "leads"),
                        build_sha="fixture-build")
    with TestClient(create_app(settings, RagEngine(knowledge, config, transport=transport))) as web:
        assert web.get("/").status_code == 200
        assert web.get("/demo.js").status_code == 200
        assert web.get("/api/v1/health").json()["build_sha"] == "fixture-build"
        session = web.post("/api/v1/sessions", json={}).json()
        path = f"/api/v1/sessions/{session['session_id']}"
        headers = {"X-Client-Token": session["client_token"]}

        def send(request_id, text):
            response = web.post(f"{path}/messages", headers=headers, json={"request_id": request_id, "text": text})
            assert response.status_code == 200, response.text
            return response.json()

        assert send("search", question)["answer"] == answer
        trace = web.get(f"{path}/turns/search/debug", headers=headers).json()
        assert [item["evidence_id"] for item in trace["used"]] == [calls[0]["evidence"][0]["id"]]
        assert {"engine.retrieval", "engine.generation"} <= {item["name"] for item in trace["timings"]}

        correction = "У нас 12 счётчиков. Свяжите со специалистом."
        assert send("callback", correction)["action"] == "collect_contacts"
        lead = send("phone", "+7 999 000-00-31")["lead"]
        assert lead["summary"]["task"] == question
        assert lead["summary"]["known_facts"] == ["Количество счётчиков: 12"]
        saved = web.post(f"{path}/lead/save", headers=headers, json={
            "idempotency_key": "confirmed", "offer_id": lead["offer_id"],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["contact"]["company"] is None
        assert web.get(f"{path}/lead.md", headers=headers).status_code == 200
        assert len(calls) == 1

    with (tmp_path / "questions.csv").open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows((("id", "question"), ("001", question)))

    async def csv_run():
        async with OrganizerClient(config, transport=transport) as client:
            consultant = RagConsultant(knowledge, client.chat)

            async def answer_question(value):
                return (await consultant.consult(parse_csv_history(value))).text

            return await run_submission(tmp_path, answer_question, output_path=tmp_path / "submission.csv")

    assert asyncio.run(csv_run()) == 1
    with (tmp_path / "submission.csv").open(encoding="utf-8", newline="") as file:
        assert list(csv.DictReader(file)) == [{"id": "001", "answer": answer}]
    assert calls[0] == calls[1]


@pytest.mark.parametrize("correction", (
    "Уточнение: счётчиков 87, а не 12. Это влияет на совместимость?",
    "У нас не 12 счётчиков, а 87. Это влияет на совместимость?",
    "Уточнение: у нас 87 электросчётчиков, а не 12, это влияет на совместимость?",
))
def test_rag_correction_refreshes_offer_without_losing_project_context(make_api, rag_fixture, correction):
    knowledge, config, transport, answer, _ = rag_fixture
    api, _ = make_api(engine=RagEngine(knowledge, config, transport=transport))
    sid, token = api.new_session()
    route = f"/api/v1/sessions/{sid}/lead"
    api.say(sid, token, "Нужен учёт арендаторов в бизнес-центре. У нас 12 счётчиков. Свяжите со специалистом.")
    offer = api.put(route + "/contacts", token, json={"phone": "+7 999 000-00-31"}).json()
    followup = api.say(sid, token, "А какие ограничения?")["lead"]
    assert followup["offer_id"] == offer["offer_id"]
    assert followup["summary"] == offer["summary"]

    response = api.say(sid, token, correction, request_id="correction")
    assert response["answer"] == answer
    latest = response["lead"]
    assert "Нужен учёт арендаторов в бизнес-центре" in latest["summary"]["task"]
    assert "87 счётчиков" in latest["summary"]["task"] and "12" not in latest["summary"]["task"]
    assert latest["summary"]["known_facts"] == ["Количество счётчиков: 87"]
    assert latest["offer_id"] != offer["offer_id"]
    trace = api.get(f"/api/v1/sessions/{sid}/turns/correction/debug", token).json()
    assert trace["used"]

    stale = api.post(route + "/save", token, json={"idempotency_key": "save", "offer_id": offer["offer_id"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_offer"
    saved = api.post(route + "/save", token, json={"idempotency_key": "save", "offer_id": latest["offer_id"]})
    assert saved.status_code == 200 and saved.json()["summary"] == latest["summary"]


def test_rag_answers_a_product_question_in_the_same_turn_as_contact_refusal(make_api, rag_fixture):
    knowledge, config, transport, answer, calls = rag_fixture
    api, _ = make_api(engine=RagEngine(knowledge, config, transport=transport))
    sid, token = api.new_session()
    route = f"/api/v1/sessions/{sid}/lead"
    api.say(sid, token, "Свяжите со специалистом.")
    offer = api.put(route + "/contacts", token, json={"phone": "+7 999 000-00-31"}).json()
    reply = api.say(sid, token, "Не хочу оставлять телефон. Как начать учёт счётчиков арендаторов?")
    assert reply["answer"] == answer and len(calls) == 1
    assert reply["lead"]["contact_refused"] and reply["lead"]["contact"]["phone"] is None
    save = api.post(route + "/save", token, json={"idempotency_key": "save", "offer_id": offer["offer_id"]})
    assert save.status_code == 409 and save.json()["error"]["code"] == "contacts_refused"
