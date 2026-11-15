import asyncio
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import httpx

from organizer_api import APIConfig, OrganizerClient
from solution import QUESTION_FALLBACK, SubmissionError, run_platform_submission, run_submission


SCRIPT = Path(__file__).resolve().parents[1] / "solution.py"


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.input_path = self.root / "questions.csv"
        self.output_path = self.root / "submission.csv"

    def write_questions(self, rows, encoding="utf-8"):
        with self.input_path.open("w", encoding=encoding, newline="") as source:
            csv.writer(source).writerows([("id", "question"), *rows])

    async def run_csv(self, handler):
        return await run_submission(self.root, handler, output_path=self.output_path)

    async def test_preserves_full_history_ids_order_and_csv_escaping(self):
        history = 'Клиент: Цех «А», счётчик "Север".\r\nКонсультант: Какая задача?\r\nКлиент: Нужен учёт.'
        second = 'Одиночный вопрос, без предыдущего диалога.'
        self.write_questions([("007", history), ("2", second)], encoding="utf-8-sig")
        inputs = []

        async def answer(question):
            inputs.append(question)
            if 'счётчик "Север"' in question:
                return 'По счётчику "Север" уточните данные.\nЗатем обсудим учёт.'
            return "Уточните задачу."

        self.assertEqual(await self.run_csv(answer), 2)
        self.assertEqual(inputs, [history, second])
        with self.output_path.open(encoding="utf-8", newline="") as target:
            self.assertEqual(list(csv.reader(target)), [
                ["id", "answer"],
                ["007", 'По счётчику "Север" уточните данные.\nЗатем обсудим учёт.'],
                ["2", "Уточните задачу."],
            ])
        self.assertFalse(self.output_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    async def test_invalid_input_is_detected_before_the_first_handler_call(self):
        for content in (
            "id,wrong\n1,text\n",
            "id,id,question\n1,2,text\n",
            "id,question\n1,first\n1,duplicate\n",
            "id,question\n1,first\n2\n",
            "id,question\n1,first\n2,second,extra\n",
            'id,question\n1,first\n2,"unfinished',
            "id,question\n1,first\n,second\n",
            "id,question\n1,first\n2,   \n",
        ):
            with self.subTest(content=content):
                self.input_path.write_text(content, encoding="utf-8")

                async def answer(_question):
                    self.fail("A malformed input file must not start generation")

                with self.assertRaises(SubmissionError):
                    await self.run_csv(answer)
                self.assertFalse(self.output_path.exists())

    async def test_invalid_answers_are_rejected_and_temp_file_removed(self):
        self.write_questions([("1", "first"), ("2", "second")])
        for invalid_answer in ("  \n", None, "я" * 10_001):
            with self.subTest(answer_length=len(invalid_answer) if invalid_answer else None):
                async def answer(question):
                    return "Валидный ответ" if question == "first" else invalid_answer

                with self.assertRaises(SubmissionError):
                    await self.run_csv(answer)
                self.assertEqual(list(self.root.iterdir()), [self.input_path])

    async def test_exact_character_limit_is_accepted(self):
        self.write_questions([("1", "question")])

        async def answer(_question):
            return "я" * 10_000

        await self.run_csv(answer)
        with self.output_path.open(encoding="utf-8", newline="") as target:
            self.assertEqual(len(next(csv.DictReader(target))["answer"]), 10_000)

    async def test_handler_failure_preserves_previous_output_without_partial_rows(self):
        self.write_questions([("1", "first"), ("2", "second")])
        previous = b"id,answer\r\nold,previous\r\n"
        self.output_path.write_bytes(previous)

        async def answer(question):
            if question == "second":
                raise RuntimeError("synthetic-private-payload")
            return "Новый ответ"

        with self.assertRaises(SubmissionError) as caught:
            await self.run_csv(answer)
        self.assertIn("№2", str(caught.exception))
        self.assertNotIn("synthetic-private-payload", str(caught.exception))
        self.assertEqual(self.output_path.read_bytes(), previous)
        self.assertEqual(set(self.root.iterdir()), {self.input_path, self.output_path})

    async def test_platform_adapter_keeps_rows_when_proxy_fails_on_one_question(self):
        archive = self.root / "knowledge-base.zip"
        with ZipFile(archive, "w") as package:
            package.writestr("meters.md", "# Счётчики\n\nСчётчики проверяют перед подключением.\n")
        self.write_questions([
            ("007", "Пользователь: Счётчики на объекте Кедр.\n"
                    "Ассистент: Что проверить?\nПользователь: Как подключить счётчики?"),
            ("02", "Какие счётчики подходят?"),
            ("11", "Как проверить счётчики?"),
        ], encoding="utf-8-sig")
        histories = []

        def proxy(request):
            body = json.loads(request.content)
            payload = json.loads(body["messages"][-1]["content"])
            histories.append(payload["history"])
            if len(histories) == 2:
                return httpx.Response(503, text="synthetic-private-proxy-error")
            answer = {
                "text": "Начните с проверки приборов.",
                "action": "answer",
                "summary": None,
                "evidence_ids": [payload["evidence"][0]["id"]],
                "support": {payload["evidence"][0]["id"]: payload["evidence"][0]["text"]},
            }
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(answer)},
            }]})

        config = APIConfig("https://fixture.invalid/v1", "synthetic", "synthetic-model", max_retries=0)
        client = OrganizerClient(config, transport=httpx.MockTransport(proxy))
        with patch.object(APIConfig, "from_env", return_value=config), \
                patch("organizer_api.OrganizerClient", return_value=client), \
                self.assertLogs("solution", level="WARNING") as logs:
            count = await run_platform_submission(self.root, output_path=self.output_path)

        self.assertEqual(count, 3)
        self.assertEqual([turn["role"] for turn in histories[0]], ["user", "assistant", "user"])
        self.assertEqual(histories[1], [{"role": "user", "text": "Какие счётчики подходят?"}])
        self.assertEqual(histories[2], [{"role": "user", "text": "Как проверить счётчики?"}])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("№2", logs.output[0])
        self.assertNotIn("synthetic-private-proxy-error", logs.output[0])
        self.assertNotIn("Какие счётчики", logs.output[0])
        with self.output_path.open(encoding="utf-8", newline="") as target:
            self.assertEqual(list(csv.reader(target)), [
                ["id", "answer"],
                ["007", "Начните с проверки приборов."],
                ["02", QUESTION_FALLBACK],
                ["11", "Начните с проверки приборов."],
            ])
        self.assertEqual(set(self.root.iterdir()), {archive, self.input_path, self.output_path})

    async def test_recovery_substitutes_invalid_answers_and_preserves_order(self):
        answers = {"blank": " \n", "nontext": None, "long": "я" * 10_001,
                   "unencodable": "\ud800", "valid": "Готово"}
        self.write_questions([(question, question) for question in answers])

        async def answer(question):
            return answers[question]

        with self.assertLogs("solution", level="WARNING") as logs:
            count = await run_submission(
                self.root, answer, output_path=self.output_path, recover_question_errors=True,
            )

        self.assertEqual(count, 5)
        self.assertEqual(len(logs.output), 4)
        with self.output_path.open(encoding="utf-8", newline="") as target:
            self.assertEqual(list(csv.reader(target)), [
                ["id", "answer"],
                ["blank", QUESTION_FALLBACK],
                ["nontext", QUESTION_FALLBACK],
                ["long", QUESTION_FALLBACK],
                ["unencodable", QUESTION_FALLBACK],
                ["valid", "Готово"],
            ])

    async def test_recovery_preserves_previous_output_when_all_questions_fail(self):
        self.write_questions([("01", "first"), ("02", "second")])
        previous = b"id,answer\r\nold,previous\r\n"
        self.output_path.write_bytes(previous)

        async def answer(_question):
            raise RuntimeError("synthetic-private-payload")

        with self.assertLogs("solution", level="WARNING") as logs:
            with self.assertRaisesRegex(SubmissionError, "ни одного ответа"):
                await run_submission(
                    self.root, answer, output_path=self.output_path, recover_question_errors=True,
                )

        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("synthetic-private-payload", "\n".join(logs.output))
        self.assertEqual(self.output_path.read_bytes(), previous)
        self.assertEqual(set(self.root.iterdir()), {self.input_path, self.output_path})

    async def test_cancellation_does_not_publish_partial_csv(self):
        self.write_questions([("1", "first"), ("2", "second")])
        entered = asyncio.Event()

        async def answer(question):
            if question == "second":
                entered.set()
                await asyncio.Future()
            return "Готовый ответ"

        task = asyncio.create_task(self.run_csv(answer))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(self.root.iterdir()), [self.input_path])


class SubmissionCommandTests(unittest.TestCase):
    def test_demo_uses_data_dir_and_writes_to_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "supplied input"
            data_dir.mkdir()
            (data_dir / "questions.csv").write_text(
                'id,question\n12,"строка 1\nстрока 2"\n03,другой вопрос\n', encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--demo"],
                cwd=root, env={**os.environ, "DATA_DIR": str(data_dir)},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "submission.csv").open(encoding="utf-8", newline="") as target:
                reader = csv.DictReader(target)
                self.assertEqual(reader.fieldnames, ["id", "answer"])
                self.assertEqual([row["id"] for row in reader], ["12", "03"])

    def test_plain_invocation_requires_platform_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            for name in ("LLM_BASE_URL", "OPENAI_BASE_URL", "BASE_URL"):
                env.pop(name, None)
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT)], cwd=directory, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Проверьте конфигурацию API организаторов", result.stderr)
            self.assertFalse((Path(directory) / "submission.csv").exists())


if __name__ == "__main__":
    unittest.main()
