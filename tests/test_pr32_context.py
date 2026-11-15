import pytest

from consultation import Turn
from consultation_context import analyze_history, refuses_contact, requests_callback, task_text


def resolve(*messages):
    return analyze_history(tuple(Turn("user", message) for message in messages))


@pytest.mark.parametrize(
    ("message", "callback", "refusal", "remaining"),
    [
        ("Я не хочу оставить заявку, расскажите про тарифы.", False, True, "расскажите про тарифы"),
        ("Что требуется, чтобы оставить заявку на учёт?", False, False, "Что требуется, чтобы оставить заявку на учёт"),
        ("Что будет, если я напишу «позвоните мне»?", False, False, "Что будет, если я напишу «позвоните мне»"),
        ("Можно связаться со специалистом?", True, False, ""),
        ("Свяжите меня со специалистом.", True, False, ""),
        ("Объясните тарифы. И перезвоните мне.", True, False, "Объясните тарифы"),
        ("Расскажите как настроить учёт и перезвоните мне", True, False, "Расскажите как настроить учёт"),
        ("Не надо мне звонить, расскажите про учёт.", False, True, "расскажите про учёт"),
    ],
)
def test_callback_requires_an_explicit_user_request(message, callback, refusal, remaining):
    assert requests_callback(message) is callback
    assert refuses_contact(message) is refusal
    assert task_text(message) == remaining


def test_planned_and_hypothetical_numbers_do_not_replace_actual_numbers():
    situation = resolve(
        "У нас мощность 670 кВт. Хотим выйти на ОРЭМ.",
        "Планируемая мощность — 4 МВт.",
        "Если увеличить мощность до 5 МВт, изменится ли порядок?",
        "Подходим ли мы сейчас?",
    )
    assert situation.facts == {"Мощность": "670 кВт", "Планируемая мощность": "4 МВт"}
    assert "У нас мощность 670 кВт" in situation.task
    assert "У нас мощность 4 МВт" not in situation.query
    assert "5 МВт" not in situation.query
    counters = resolve(
        "Хотим учёт по арендаторам. У нас на объекте 40 счётчиков.",
        "В следующем году планируем установить 60 счётчиков.",
        "Если установить 80 счётчиков, документ подходит?",
        "С чего начать сейчас?",
    )
    assert counters.facts == {
        "Количество счётчиков": "40",
        "Планируемое количество счётчиков": "60",
    }


def test_current_and_planned_values_can_appear_in_one_turn():
    situation = resolve("Сейчас мощность 670 кВт и планируем 4 МВт.")
    assert situation.facts == {"Мощность": "670 кВт", "Планируемая мощность": "4 МВт"}


def test_objects_with_the_same_topic_have_separate_facts_and_history():
    warehouse_a = "Нужен учёт на складе А. У нас 20 счётчиков."
    warehouse_b = "А теперь про учёт на складе Б. С чего начать?"
    second = resolve(warehouse_a, warehouse_b)
    assert second.topic == "metering"
    assert second.facts == {}
    assert "складе А" not in second.query and "20" not in second.query
    assert [turn.text for turn in second.history] == [warehouse_b]

    restored = resolve(
        warehouse_a,
        warehouse_b,
        "У нас на складе Б 15 счётчиков.",
        "Вернёмся к учёту на складе А. Не 20 счётчиков, а 12.",
    )
    assert restored.facts == {"Количество счётчиков": "12"}
    assert "складе Б" not in restored.query and "15" not in restored.query and "20" not in restored.query
