import asyncio
import csv
import json
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.rag_engine import RagEngine
from backend.settings import Settings
from consultation import parse_csv_history
from knowledge_base import KnowledgeBase
from organizer_api import APIConfig, OrganizerClient
from rag_consultation import RagConsultant
from solution import run_submission


def test_web_and_csv_share_rag_and_web_confirms_a_local_lead(tmp_path):
    archive = tmp_path / "knowledge-base.zip"
    with ZipFile(archive, "w") as target:
        target.writestr("metering.md", "# Учёт счётчиков арендаторов\n\nПроверьте совместимость счётчиков.")
    question = "С чего начать учёт показаний счётчиков арендаторов?"
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

    transport = httpx.MockTransport(model)
    config = APIConfig("https://fixture.invalid/v1", "synthetic", "synthetic-model")
    knowledge = KnowledgeBase.from_zip(archive)
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
        assert lead["summary"]["task"] == "У нас 12 счётчиков."
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
