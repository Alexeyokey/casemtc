"""Cross-adapter checks with local search and prescribed synthetic model replies."""

import asyncio
import csv
import io
import json
import re
import sqlite3
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults
from zipfile import ZipFile

from consultation import parse_csv_history
from examples.demo_web import create_app
from knowledge_base import KnowledgeBase
from organizer_api import ChatResult
from rag_consultation import NO_EVIDENCE, RagConsultant
from solution import run_submission


FIXTURE = Path(__file__).with_name("fixtures") / "energytool_acceptance.json"


class IntegrationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.turns = self.fixture["turns"]
        self.scenarios_by_message = {turn["message"]: name for name, turn in self.turns.items()}
        archive = self.directory / "knowledge-base.zip"
        with ZipFile(archive, "w") as destination:
            for path, markdown in self.fixture["documents"].items():
                destination.writestr(path, markdown)
        self.calls = []
        self.consultant = RagConsultant(KnowledgeBase.from_zip(archive), self.fixture_model)

    async def fixture_model(self, messages):
        payload = json.loads(messages[-1]["content"])
        history = payload["history"]
        self.assertEqual(history[-1]["role"], "user")
        name = self.scenarios_by_message[history[-1]["text"]]
        turn = self.turns[name]
        self.assertIn("reply", turn, "Запрос без сведений в базе не должен вызывать модель")
        sources = {source["id"].rsplit("#L", 1)[0]: source["id"] for source in payload["evidence"]}
        self.assertTrue(set(turn["sources"]).issubset(sources), f"Не хватает источника для {name}")
        previous_user_text = "\n".join(item["text"] for item in history[:-1] if item["role"] == "user")
        for expected_fact in turn.get("history_contains", []):
            self.assertIn(expected_fact, previous_user_text)
        self.calls.append(name)
        result = {**turn["reply"], "evidence_ids": [sources[path] for path in turn["sources"]]}
        return ChatResult(json.dumps(result, ensure_ascii=False), "stop", None)

    def transcript(self, names):
        lines = []
        for name in names[:-1]:
            turn = self.turns[name]
            lines.extend((f'Пользователь: {turn["message"]}', f'Ассистент: {turn["reply"]["text"]}'))
        lines.append(f'Пользователь: {self.turns[names[-1]]["message"]}')
        return "\n".join(lines)

    def test_csv_delivers_synthetic_scenarios_and_isolates_rows(self):
        cases = self.fixture["csv_cases"]
        with (self.directory / "questions.csv").open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(("id", "question"))
            writer.writerows((case["id"], self.transcript(case["turns"])) for case in cases)

        async def answer(question):
            history = parse_csv_history(question)
            turn = self.turns[self.scenarios_by_message[history[-1].text]]
            reply = await self.consultant.consult(history)
            self.assertEqual(reply.action, turn.get("reply", {}).get("action", "clarify"))
            return reply.text

        output = self.directory / "submission.csv"
        self.assertEqual(asyncio.run(run_submission(self.directory, answer, output_path=output)), len(cases))
        with output.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            self.assertEqual(reader.fieldnames, ["id", "answer"])
            results = list(reader)

        self.assertEqual(self.calls, ["business-mixed", "loss-ready", "loss-correction"])
        expected = [
            {"id": case["id"], "answer": self.turns[case["turns"][-1]].get("reply", {}).get("text", NO_EVIDENCE)}
            for case in cases
        ]
        self.assertEqual(results, expected)

    def test_web_persists_only_confirmed_current_lead_and_isolates_new_session(self):
        case = self.fixture["web_case"]
        db_path = self.directory / "web.sqlite3"

        def consult(history):
            return asyncio.run(self.consultant.consult(history))

        app = create_app(db_path, consult=consult)
        session_id, page = _session(app)
        offers = []
        for name in case["turns"]:
            with self.subTest(turn=name):
                turn = self.turns[name]
                status, _, _ = _request(app, "POST", "/chat", session_id=session_id, message=turn["message"])
                self.assertEqual(status, 303)
                status, _, html = _request(app, "GET", page)
                self.assertEqual(status, 200)
                self.assertIn(escape(turn["reply"]["text"]), html)
                offer = re.search(r'name="offer_id" value="(\d+)"', html)
                if turn["reply"]["action"] == "offer_contact":
                    self.assertIsNotNone(offer)
                    offers.append(offer[1])
                    self.assertIn(escape(turn["reply"]["summary"]), html)
                else:
                    self.assertIsNone(offer)
                if name == "contact-refusal":
                    rejected, _, _ = _request(app, "POST", "/lead", session_id=session_id, phone=case["phone"])
                    self.assertEqual(rejected, 409)

        self.assertEqual(len(offers), 2)
        old_offer, current_offer = offers
        self.assertNotEqual(old_offer, current_offer)
        for offer_id, phone, expected_status in (
            (old_offer, case["phone"], 409),
            (current_offer, case["invalid_phone"], 400),
            (current_offer, case["phone"], 303),
            (current_offer, case["phone"], 303),
        ):
            status, _, _ = _request(app, "POST", "/lead", session_id=session_id, phone=phone, offer_id=offer_id)
            self.assertEqual(status, expected_status)

        app = create_app(db_path, consult=consult)
        _, _, original = _request(app, "GET", page)
        self.assertIn("Заявка сохранена", original)
        current_summary = self.turns[case["turns"][-1]]["reply"]["summary"]
        with sqlite3.connect(db_path) as db:
            leads = db.execute("SELECT session_id, phone, summary FROM leads").fetchall()
        self.assertEqual(leads, [(session_id, case["phone"], current_summary)])

        new_id, new_page = _session(app)
        self.assertNotEqual(new_id, session_id)
        status, _, _ = _request(
            app, "POST", "/chat", session_id=new_id, message=self.turns[case["new_session_turn"]]["message"],
        )
        self.assertEqual(status, 303)
        _, _, isolated = _request(app, "GET", new_page)
        self.assertIn(escape(NO_EVIDENCE), isolated)
        self.assertNotIn(escape(current_summary), isolated)
        self.assertEqual(self.calls, case["turns"])


def _session(app):
    status, headers, _ = _request(app, "POST", "/sessions")
    if status != 303:
        raise AssertionError(f"Не удалось создать сессию: {status}")
    page = headers["Location"]
    return parse_qs(urlsplit(page).query)["session_id"][0], page


def _request(app, method, url, **fields):
    parsed = urlsplit(url)
    body = urlencode(fields).encode()
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        REQUEST_METHOD=method,
        PATH_INFO=parsed.path,
        QUERY_STRING=parsed.query,
        CONTENT_TYPE="application/x-www-form-urlencoded",
        CONTENT_LENGTH=str(len(body)),
        **{"wsgi.input": io.BytesIO(body)},
    )
    response = []

    def capture(status, headers):
        response.extend((int(status.split()[0]), dict(headers)))

    html = b"".join(app(environ, capture)).decode()
    return response[0], response[1], html
