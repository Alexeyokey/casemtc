import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import httpx

from consultation import Turn
from knowledge_base import KnowledgeBase
from organizer_api import APIConfig, OrganizerClient
from rag_consultation import RagConsultant, configured_top_k


class RetrievalConfigurationTests(unittest.TestCase):
    def test_top_k_range_matches_the_organizer_sample(self):
        for value in (1, 30):
            with self.subTest(value=value), patch.dict("os.environ", {"TOP_K": str(value)}):
                self.assertEqual(configured_top_k(), value)

    def test_top_k_rejects_values_outside_the_organizer_range(self):
        for value in ("0", "31", "2.5"):
            with self.subTest(value=value), patch.dict("os.environ", {"TOP_K": value}):
                with self.assertRaisesRegex(ValueError, "от 1 до 30"):
                    configured_top_k()

    def test_top_k_defaults_to_five(self):
        with patch.dict("os.environ", {"TOP_K": ""}):
            self.assertEqual(configured_top_k(), 5)


class RagConsultationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        archive = Path(directory.name) / "knowledge-base.zip"
        with ZipFile(archive, "w") as target:
            target.writestr(
                "test-metering.md",
                "# Счётчики арендаторов\n\n"
                "Счётчики арендаторов передают показания после проверки совместимости.\n"
                "Для неподключённых электрических приборов можно проверить распознавание по фото.\n",
            )
        self.knowledge = KnowledgeBase.from_zip(archive)
        self.history = (
            Turn("user", "Как подключить счётчики арендаторов?"),
            Turn("assistant", "Уточните объект."),
            Turn("user", "Это бизнес-центр. С чего начать?"),
        )

    async def test_sends_retrieved_evidence_and_latest_message_to_organizer_client(self):
        def transport(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "synthetic-model")
            payload = json.loads(body["messages"][-1]["content"])
            self.assertEqual(payload["history"][-1]["text"], self.history[-1].text)
            evidence = payload["evidence"][0]
            self.assertIn("проверки совместимости", evidence["text"])
            self.assertIn("фото", evidence["text"])
            reply = {
                "text": "Начните с проверки совместимости приборов.",
                "action": "answer", "evidence_ids": [evidence["id"]],
                "support": {evidence["id"]: evidence["text"]},
            }
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(reply)},
            }]})

        config = APIConfig("https://fixture.invalid/v1", "synthetic", "synthetic-model")
        async with OrganizerClient(config, transport=httpx.MockTransport(transport)) as client:
            reply = await RagConsultant(self.knowledge, client.chat).consult(self.history)
        self.assertEqual(reply.text, "Начните с проверки совместимости приборов.")
        self.assertEqual(len(reply.evidence_ids), 1)

    async def test_no_evidence_never_sends_a_model_request(self):
        async def unexpected(_messages):
            self.fail("No evidence should not call the model")

        consultant = RagConsultant(self.knowledge, unexpected)
        reply = await consultant.consult((Turn("user", "Рецепт плова с шафраном"),))
        self.assertEqual(reply.action, "out_of_scope")
        self.assertIn("МТС EnergyTool", reply.text)
        callback = await consultant.consult((Turn("user", "Перезвоните, пожалуйста."),))
        self.assertEqual(callback.action, "offer_contact")
        self.assertTrue(callback.summary)

    async def test_rejects_model_references_to_unretrieved_sources(self):
        def transport(_request):
            reply = {"text": "Гарантируем скидку.", "action": "answer", "evidence_ids": ["invented"]}
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(reply)},
            }]})

        config = APIConfig("https://fixture.invalid", "synthetic", "synthetic-model")
        async with OrganizerClient(config, transport=httpx.MockTransport(transport)) as client:
            reply = await RagConsultant(self.knowledge, client.chat).consult(self.history)
        self.assertEqual(reply.action, "escalate")
        self.assertNotIn("Гарантируем", reply.text)
