import json

import pytest

from consultation_grounding import ESCALATE, read_reply
from knowledge_base import Evidence


@pytest.mark.parametrize(
    ("quote", "answer"),
    [
        (
            "МТС EnergyTool не распознаёт показания счётчиков по фотографии.",
            "МТС EnergyTool распознаёт показания счётчиков по фотографии.",
        ),
        (
            "МТС EnergyTool распознаёт показания счётчиков по фотографии.",
            "Показания счётчиков по фотографии не распознаются.",
        ),
        (
            "Архив показаний хранится 12 месяцев.",
            "Для подключения потребуется 12 месяцев.",
        ),
        (
            "Архив показаний хранится 12 месяцев, подключение занимает 3 месяца.",
            "Подключение архива занимает 12 месяцев.",
        ),
        (
            "После подключения архив показаний хранится 12 месяцев.",
            "Подключение архива занимает 12 месяцев.",
        ),
    ],
)
def test_quote_does_not_license_opposite_or_unrelated_claim(quote, answer):
    source = Evidence("s1", "source.md", "Учёт", quote)
    raw = json.dumps({
        "text": answer, "action": "answer", "evidence_ids": [source.id],
        "support": {source.id: quote},
    })
    reply = read_reply(raw, (source,), client_facts={})
    assert reply.text == ESCALATE
    assert reply.action == "escalate"
    assert not reply.evidence_ids


@pytest.mark.parametrize(
    ("source_text", "quote", "answer"),
    [
        (
            "Сервис не\nпередаёт данные в АИИС КУЭ.",
            "передаёт данные в АИИС КУЭ",
            "Сервис передаёт данные в АИИС КУЭ.",
        ),
        (
            "Сервис не\nпередаёт данные в АИИС КУЭ.",
            "Сервис не\nпередаёт данные в АИИС КУЭ.",
            "Сервис передаёт данные в АИИС КУЭ.",
        ),
        (
            "Сервис не передаёт данные в АИИС КУЭ.",
            "Сервис не\n\nпередаёт данные в АИИС КУЭ.",
            "Сервис передаёт данные в АИИС КУЭ.",
        ),
        (
            "Архив показаний хранится 12.5 месяцев.",
            "Архив показаний хранится 12",
            "Архив показаний хранится 12.",
        ),
    ],
)
def test_cannot_drop_negation_or_decimal_fraction_at_quote_boundary(source_text, quote, answer):
    source = Evidence("s1", "source.md", "Учёт", source_text)
    raw = json.dumps({
        "text": answer, "action": "answer", "evidence_ids": [source.id],
        "support": {source.id: quote},
    })
    assert read_reply(raw, (source,), client_facts={}).action == "escalate"


@pytest.mark.parametrize(
    ("quote", "answer"),
    [
        (
            "МТС EnergyTool не распознаёт показания счётчиков по фотографии.",
            "Показания счётчиков по фотографии не распознаются.",
        ),
        (
            "Сервис не\nпередаёт данные в АИИС КУЭ.",
            "Сервис не передаёт данные в АИИС КУЭ.",
        ),
        (
            "Архив показаний хранится 12 месяцев.",
            "Срок хранения архива — 12 месяцев.",
        ),
        (
            "Архив показаний хранится не менее 12 месяцев.",
            "Архив хранится как минимум 12 месяцев.",
        ),
        (
            "Для бизнес-центра сначала проверьте совместимость счётчиков.",
            "Начните с проверки совместимости счётчиков.",
        ),
    ],
)
def test_preserves_supported_paraphrases(quote, answer):
    source = Evidence("s1", "source.md", "Учёт", quote)
    raw = json.dumps({
        "text": answer, "action": "answer", "evidence_ids": [source.id],
        "support": {source.id: quote},
    })
    reply = read_reply(raw, (source,), client_facts={})
    assert reply.text == answer
    assert reply.action == "answer"
    assert reply.evidence_ids == (source.id,)
