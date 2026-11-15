"""Consultation regressions at the configured web and platform CLI entry points."""

import csv
import json
from zipfile import ZipFile

import httpx
import pytest

import backend.rag_engine as rag_engine
import organizer_api
from consultation_grounding import ESCALATE
from rag_consultation import CONTACT
from solution import main


DOCUMENTS = {
    "orem.md": (
        "Условия выхода на ОРЭМ",
        "Доступ на ОРЭМ зависит от категории объекта и документов.",
    ),
    "meters.md": (
        "Учёт счётчиков на складе",
        "Для учёта на складе проверьте совместимость счётчиков.",
    ),
    "photo.md": (
        "Учёт показаний счётчиков по фотографии",
        "МТС EnergyTool не распознаёт показания счётчиков по фотографии.",
    ),
    "archive.md": (
        "Хранение архива показаний и подключение",
        "Архив показаний хранится 12 месяцев.",
    ),
    "tariffs.md": (
        "Электроэнергия и выбор тарифа",
        "Тариф зависит от региона и категории потребителя.",
    ),
    "intake.md": (
        "Что требуется, чтобы оставить заявку на учёт",
        "Перед оформлением заявки на учёт уточните тип объекта.",
    ),
}


@pytest.fixture
def run_dialogue(tmp_path, monkeypatch, make_api):
    archive_path = tmp_path / "knowledge-base.zip"
    with ZipFile(archive_path, "w") as archive:
        for name, (heading, text) in DOCUMENTS.items():
            archive.writestr(name, f"# {heading}\n\n{text}\n")
    monkeypatch.setenv("ENERGYTOOL_KB_PATH", str(archive_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("LLM_MODEL", "synthetic-model")
    monkeypatch.setenv("TOP_K", "5")
    monkeypatch.chdir(tmp_path)

    def run(messages, model_replies):
        payloads = []

        def reply_to_request(request):
            assert request.method == "POST" and request.url.path == "/v1/chat/completions"
            payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
            payloads.append(payload)
            source_file, model_answer = model_replies[payload["history"][-1]["text"]]
            source = next(item for item in payload["evidence"] if item["id"].startswith(source_file + "#"))
            quote = DOCUMENTS[source_file][1]
            answer = json.dumps({
                "text": model_answer, "action": "answer", "evidence_ids": [source["id"]],
                "support": {source["id"]: quote},
            })
            return httpx.Response(200, json={"choices": [
                {"finish_reason": "stop", "message": {"content": answer}},
            ]})

        class TestProxyClient(organizer_api.OrganizerClient):
            def __init__(self, config, *, deadline=None, transport=None):
                super().__init__(config, deadline=deadline,
                                 transport=transport or httpx.MockTransport(reply_to_request))

        monkeypatch.setattr(organizer_api, "OrganizerClient", TestProxyClient)
        monkeypatch.setattr(rag_engine, "OrganizerClient", TestProxyClient)
        api, _ = make_api(engine_spec="backend.rag_engine:create_rag_engine")
        assert api.get("/api/v1/health").json()["engine"] == "backend.rag_engine:create_rag_engine"
        session, token = api.new_session()
        transcript, rows, web_turns, web_sources = [], [], [], []
        for index, message in enumerate(messages, start=1):
            turn_id = f"pr32-{index}"
            turn = api.say(session, token, message, request_id=turn_id)
            trace = api.get(f"/api/v1/sessions/{session}/turns/{turn_id}/debug", token)
            assert trace.status_code == 200
            web_turns.append(turn)
            web_sources.append([item["doc_id"] for item in trace.json()["used"]])
            transcript.append(f"Пользователь: {message}")
            rows.append((str(index), "\n".join(transcript)))
            transcript.append(f"Ассистент: {turn['answer']}")

        web_payloads = payloads.copy()
        payloads.clear()
        with (tmp_path / "questions.csv").open("w", encoding="utf-8", newline="") as questions:
            csv.writer(questions).writerows([("id", "question"), *rows])
        assert main([]) == 0
        with (tmp_path / "submission.csv").open(encoding="utf-8", newline="") as submission:
            csv_rows = list(csv.DictReader(submission))
        assert csv_rows == [
            {"id": str(index), "answer": turn["answer"]} for index, turn in enumerate(web_turns, start=1)
        ]
        assert payloads == web_payloads
        assert [payload["history"][-1]["text"] for payload in web_payloads] == list(model_replies)
        return web_turns, web_sources, web_payloads

    return run


def test_a_plan_does_not_replace_operating_power(run_dialogue):
    messages = (
        "У нас мощность 670 кВт. Хотим выйти на ОРЭМ.",
        "Планируемая мощность — 4 МВт.",
        "Подходим ли мы сейчас?",
    )
    answer = DOCUMENTS["orem.md"][1]
    turns, sources, payloads = run_dialogue(messages, dict.fromkeys(messages, ("orem.md", answer)))
    assert turns[-1]["answer"] == answer and sources[-1] == ["orem.md"]
    assert payloads[-1]["client_facts"] == {"Мощность": "670 кВт", "Планируемая мощность": "4 МВт"}


def test_switching_warehouses_keeps_their_own_meter_counts(run_dialogue):
    messages = (
        "Нужен учёт на складе А. У нас 20 счётчиков.",
        "А теперь про учёт на складе Б. С чего начать?",
        "Вернёмся к учёту на складе А. Какие счётчики подходят?",
    )
    answer = DOCUMENTS["meters.md"][1]
    turns, sources, payloads = run_dialogue(messages, dict.fromkeys(messages, ("meters.md", answer)))
    assert [turn["answer"] for turn in turns] == [answer] * 3
    assert sources == [["meters.md"]] * 3
    assert payloads[0]["client_facts"] == {"Количество счётчиков": "20"}
    assert payloads[1]["client_facts"] == {} and "складе Б" in payloads[1]["task"]
    assert all("20 счётчиков" not in item["text"] for item in payloads[1]["history"])
    assert payloads[2]["client_facts"] == {"Количество счётчиков": "20"}
    assert "складе А" in payloads[2]["task"]


@pytest.mark.parametrize(("question", "source", "model_answer", "accepted"), [
    ("Можно ли распознавать показания счётчиков по фотографии?", "photo.md",
     "МТС EnergyTool распознаёт показания счётчиков по фотографии.", False),
    ("Можно ли распознавать показания счётчиков по фотографии?", "photo.md", DOCUMENTS["photo.md"][1], True),
    ("Сколько времени потребуется для подключения архива показаний?", "archive.md",
     "Для подключения потребуется 12 месяцев.", False),
    ("Сколько времени хранится архив показаний?", "archive.md", "Срок хранения архива — 12 месяцев.", True),
], ids=("negation-lost", "negation-kept", "duration-reassigned", "duration-grounded"))
def test_quote_must_support_the_conclusion_not_just_the_words_or_number(
        run_dialogue, question, source, model_answer, accepted):
    turns, sources, _ = run_dialogue((question,), {question: (source, model_answer)})
    assert turns[0]["answer"] == (model_answer if accepted else ESCALATE)
    assert turns[0]["action"] == ("answer" if accepted else "escalate")
    assert sources[0] == ([source] if accepted else [])


def test_declined_callback_still_answers_the_tariff_question(run_dialogue):
    callback = "Свяжите со специалистом."
    refusal = "Я не хочу оставить заявку, расскажите про тарифы."
    answer = DOCUMENTS["tariffs.md"][1]
    turns, sources, _ = run_dialogue((callback, refusal), {refusal: ("tariffs.md", answer)})
    assert turns[0]["action"] == "collect_contacts" and turns[0]["lead"]["callback_requested"]
    assert turns[1]["answer"] == answer and turns[1]["action"] == "answer"
    assert sources == [[], ["tariffs.md"]]
    lead = turns[1]["lead"]
    assert lead["contact_refused"] and not lead["callback_requested"] and lead["contact"]["phone"] is None


def test_only_an_actual_callback_request_asks_for_contacts(run_dialogue):
    information = "Что требуется, чтобы оставить заявку на учёт?"
    callback = "Можно связаться со специалистом?"
    answer = DOCUMENTS["intake.md"][1]
    turns, sources, _ = run_dialogue((information, callback), {information: ("intake.md", answer)})
    assert turns[0]["answer"] == answer and turns[0]["action"] == "answer" and turns[0]["lead"] is None
    assert sources == [["intake.md"], []]
    assert turns[1]["answer"] == CONTACT and turns[1]["action"] == "collect_contacts"
    assert turns[1]["lead"]["callback_requested"] and turns[1]["lead"]["status"] == "draft"
