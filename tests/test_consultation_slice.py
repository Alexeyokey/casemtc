import csv
import io
import json
import re
import sqlite3
import tempfile
import unittest
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

from consultation import Turn, parse_csv_history
from examples.demo_consultation import answer_question, consult
from examples.demo_web import create_app
from solution import run_submission


FIRST = "Нужен учёт по арендаторам, счётчики уже установлены."
SECOND = "Часть показаний получаем по фото, часть снимает техник. С чего начать?"


class ConsultationTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_multiline_messages_without_trusting_assistant_facts(self):
        history = parse_csv_history(
            "Пользователь: Нужен учёт.\r\nЭто в бизнес-центре.\r\n"
            "Ассистент: Для арендаторов есть счётчики и сбор по фото.\r\n"
            "Пользователь: С чего начать?"
        )
        self.assertEqual(history[0], Turn("user", "Нужен учёт.\r\nЭто в бизнес-центре."))
        self.assertEqual(consult(history).action, "clarify")

    def test_contact_refusal_overrides_callback_and_keeps_context(self):
        history = (
            Turn("user", FIRST),
            Turn("user", SECOND),
            Turn("user", "Можете связаться со специалистом? Но телефон я пока не оставлю."),
        )
        reply = consult(history)
        self.assertEqual(reply.action, "answer")
        self.assertIsNone(reply.summary)
        self.assertIn("продолжить без телефона", reply.text)

    async def test_csv_uses_shared_consultation_and_isolates_question_ids(self):
        history = (Turn("user", FIRST), Turn("assistant", "Как собираете показания?"), Turn("user", SECOND))
        expected = consult(history)
        self.assertEqual(expected.action, "answer")
        self.assertEqual(len(expected.evidence_ids), 2)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / "questions.csv").open("w", encoding="utf-8", newline="") as source:
                csv.writer(source).writerows([
                    ("id", "question"),
                    ("015", f"Пользователь: {FIRST}\nАссистент: Как собираете показания?\nПользователь: {SECOND}"),
                    ("other", "С чего начать?"),
                ])
            output = root / "submission.csv"
            self.assertEqual(await run_submission(root, answer_question, output_path=output), 2)
            with output.open(encoding="utf-8", newline="") as target:
                reader = csv.DictReader(target)
                self.assertEqual(reader.fieldnames, ["id", "answer"])
                rows = list(reader)

        self.assertEqual(rows[0], {"id": "015", "answer": expected.text})
        self.assertEqual(rows[1], {"id": "other", "answer": consult((Turn("user", "С чего начать?"),)).text})
        self.assertNotEqual(rows[1]["answer"], expected.text)


class WebConsultationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "demo.sqlite3"
        self.app = create_app(self.db_path)

    def request(self, method, url, **form):
        target = urlsplit(url)
        body = urlencode(form).encode()
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            REQUEST_METHOD=method,
            PATH_INFO=target.path,
            QUERY_STRING=target.query,
            CONTENT_TYPE="application/x-www-form-urlencoded",
            CONTENT_LENGTH=str(len(body)),
            **{"wsgi.input": io.BytesIO(body)},
        )
        result = {}

        def start_response(status, headers):
            result.update(status=int(status.split()[0]), headers=dict(headers))

        result["body"] = b"".join(self.app(environ, start_response)).decode()
        return result

    def start_session(self):
        response = self.request("POST", "/sessions")
        self.assertEqual(response["status"], 303)
        location = response["headers"]["Location"]
        session_id = parse_qs(urlsplit(location).query)["session_id"][0]
        return session_id, location

    def post(self, route, session_id, **form):
        return self.request("POST", route, session_id=session_id, **form)

    def offer_id(self, page):
        return re.search(r'name="offer_id" value="(\d+)"', self.request("GET", page)["body"])[1]

    def test_health_reports_sqlite_readiness_without_creating_a_database(self):
        ready = self.request("GET", "/health")
        self.assertEqual(ready["status"], 200)
        self.assertEqual(ready["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(ready["body"]), {"status": "ok"})

        self.db_path.unlink()
        unavailable = self.request("GET", "/health")
        self.assertEqual(unavailable["status"], 503)
        self.assertEqual(json.loads(unavailable["body"]), {"status": "unavailable"})
        self.assertFalse(self.db_path.exists())

    def test_web_persists_lead_and_preserves_isolated_consultations(self):
        session_id, page = self.start_session()
        self.assertEqual(self.post("/chat", session_id, message=FIRST)["status"], 303)
        response = self.post("/chat", session_id, message=SECOND)
        self.assertEqual(response["status"], 303)
        self.assertEqual(urlsplit(response["headers"]["Location"]).fragment, "latest-reply")
        expected = consult((Turn("user", FIRST), Turn("user", SECOND)))
        self.assertIn(escape(expected.text), self.request("GET", page)["body"])

        refusal = "Не готов оставить телефон. Что подготовить?"
        self.assertEqual(self.post("/chat", session_id, message=refusal)["status"], 303)
        self.assertIn("продолжить без телефона", self.request("GET", page)["body"])
        self.assertEqual(self.post("/lead", session_id, phone="+70000000000")["status"], 409)

        self.assertEqual(self.post("/chat", session_id, message="Перезвоните, пожалуйста.")["status"], 303)
        offer_id = self.offer_id(page)
        self.assertEqual(self.post("/lead", session_id, phone="123", offer_id=offer_id)["status"], 400)
        for _ in range(2):
            response = self.post("/lead", session_id, phone="+70000000000", offer_id=offer_id)
            self.assertEqual(response["status"], 303)
            self.assertEqual(urlsplit(response["headers"]["Location"]).fragment, "saved-lead")

        new_id, new_page = self.start_session()
        self.assertNotEqual(new_id, session_id)
        self.assertEqual(self.post("/chat", new_id, message="С чего начать?")["status"], 303)
        self.assertNotIn(escape(expected.text), self.request("GET", new_page)["body"])

        self.app = create_app(self.db_path)
        self.assertIn(escape(expected.text), self.request("GET", page)["body"])
        with sqlite3.connect(self.db_path) as connection:
            leads = connection.execute("SELECT session_id, phone, summary FROM leads").fetchall()
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0][:2], (session_id, "+70000000000"))
        self.assertIn("по фото и вручную", leads[0][2])

    def test_a_form_from_an_old_tab_cannot_save_a_different_summary(self):
        session_id, page = self.start_session()
        self.post("/chat", session_id, message="Перезвоните, пожалуйста.")
        old_offer = self.offer_id(page)
        self.post("/chat", session_id, message=FIRST)
        self.post("/chat", session_id, message="Перезвоните, пожалуйста.")
        self.assertNotEqual(old_offer, self.offer_id(page))
        stale = self.post("/lead", session_id, phone="+70000000000", offer_id=old_offer)
        self.assertEqual(stale["status"], 409)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM leads").fetchone(), (0,))
