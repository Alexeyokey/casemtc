"""Resolve the current consultation from the user turns of one conversation."""

import re
from dataclasses import dataclass, field

from consultation import Turn


_TOPICS = {
    "orem": r"\bорэм\b|оптов\w* рын\w*",
    "losses": r"(?:поиск\w*|анализ\w*|выявлен\w*|снижен\w*)\s+(?:коммерческ\w*\s+)?потер|\bпо потер\w*|небаланс",
    "tariffs": r"\bтариф\w*|ценов\w+ категори",
    "metering": r"\bуч[её]т\w*|\b(?:электро)?сч[её]тчик\w*|\bпоказани\w*|\bарендатор\w*",
}
_TOPICS = {name: re.compile(pattern, re.I) for name, pattern in _TOPICS.items()}
_SWITCH = re.compile(
    r"^\s*(?:а теперь|теперь|новая тема|другая тема|другой вопрос|сменим тему|"
    r"верн[её]мся(?:\s+к)?|возвращаясь(?:\s+к)?)\b[\s,:—-]*", re.I,
)
_RETURN = re.compile(r"^\s*(?:верн[её]мся|возвращаясь)\b", re.I)
_ELLIPSIS = re.compile(r"^\s*(?:а\s+|и\s+)?(?:(?:можно\s+)?по фото\w*|сколько(?: стоит| времени)?|с чего начать)\b", re.I)
_CORRECTION = re.compile(r"\b(?:уточню|уточнение|поправка|исправлюсь|на самом деле)\b|,?\s+а не\s+", re.I)
_REFUSAL = re.compile(
    r"\bне\s+(?:(?:буду|хочу|готов\w*)\s+(?:оставлять|давать|называть|предоставлять)"
    r"\s+(?:свой\s+)?(?:телефон|номер|контакты)|звоните|перезванивайте|дам\s+(?:телефон|номер))"
    r"|\bбез\s+(?:контактов|телефона)\b",
    re.I,
)
_CALLBACK = re.compile(
    r"\b(?:перезвоните|позвоните мне|свяжитесь со мной|позовите специалиста|оставить заявку|"
    r"(?:хочу|можно)\s+(?:связаться|поговорить)\s+со специалистом|свяжите\s+с(?:о специалистом| менеджером))\b",
    re.I,
)
_INSTRUCTION = re.compile(
    r"\b(?:system|developer)\s*(?:prompt|message)\b|"
    r"\b(?:системн\w+|скрыт\w+|служебн\w+)\s+(?:промпт\w*|инструкци\w*)\b|"
    r"\b(?:игнорируй\w*|забудь\w*|отмени\w*|обойди\w*)\s+(?:\w+\s+){0,4}"
    r"(?:инструкци\w*|правил\w*|ограничени\w*)|"
    r"\b(?:ignore|override|disregard)\s+(?:\w+\s+){0,5}(?:instructions?|rules?|policy)\b|"
    r"\b(?:раскрой|покажи|выведи|распечатай)\w*\s+(?:\w+\s+){0,4}"
    r"(?:ключ(?:и|а)?\s+(?:api|доступа)|переменн\w+\s+окружени\w*|парол\w*|токен\w*)\b",
    re.I,
)
_OUTSIDE = re.compile(r"\b(?:погод\w*|гороскоп\w*|кратер\w*|рецепт\w*\s+(?:плова|пирога)|напиши\w*\s+стих\w*)\b", re.I)
_OLD_VALUE = re.compile(
    r"\bне\s+\d+(?:[.,]\d+)?(?:\s+\w+){0,2},?\s+а\s+(?=\d)"
    r"|,?\s+а не\s+\d+(?:[.,]\d+)?(?:\s+(?:[км]?вт|сч[её]тчик\w*|объект\w*))?",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_REDACT = re.compile(
    r"\b[\w.+-]+@[\w.-]+\.\w+\b|\bsk-[\w-]{12,}|"
    r"\b(?:api[_-]?key|password|token|authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+|"
    r"(?<!\w)(?:\+?7|8)[ \t()\-]*(?:\d[ \t()\-]*){9}\d(?!\d)|"
    r"(?<=телефон )\d(?:[ \t()\-]*\d){9}(?!\d)",
    re.I,
)
_NUMBER = r"\d+(?:[.,]\d+)?"
_POWER = r"(?:мвт|квт|вт|mw|kw|w)"
_COUNT_UNITS = {
    "Количество счётчиков": r"(?:электро)?сч[её]тчик[а-яё]*",
    "Количество объектов": r"(?:объект|филиал|здани)[а-яё]*",
}
_BEFORE = re.compile(r"\b(?:не|менее|более|меньше|больше|до|около|примерно|порядка|возможно)\s*$", re.I)
_POWER_VALUE = re.compile(rf"\b(?P<value>{_NUMBER}\s*{_POWER})\b", re.I)
_DAY_NIGHT = re.compile(r"(?:разбив\w*|делени\w*).{0,65}\bдень\b.{0,15}\bночь\b", re.I)


def is_instruction(text: str) -> bool:
    return bool(_INSTRUCTION.search(text))


def requests_callback(text: str) -> bool:
    return not refuses_contact(text) and bool(_CALLBACK.search(text))


def refuses_contact(text: str) -> bool:
    return bool(_REFUSAL.search(text))


def redact_input(text: str) -> str:
    return _REDACT.sub("[скрыто]", text)


def task_text(text: str) -> str:
    return " ".join(
        remaining for part in _SENTENCE.split(text)
        if (remaining := _CALLBACK.sub("", _REFUSAL.sub("", part)).strip(" ,.!?—-"))
        and remaining.casefold() not in {"пожалуйста", "да", "нет", "спасибо", "[скрыто]"}
    )


def _count(part: str, units: str) -> str | None:
    replacement = re.search(
        rf"\bне\s+\d+\s+{units},?\s+а\s+(?P<before>\d+)\b"
        rf"|\bне\s+\d+,?\s+а\s+(?P<after>\d+)\s+{units}\b",
        part, re.I,
    )
    if replacement:
        return replacement.group(replacement.lastgroup)
    match = re.search(rf"\b(?:(?P<before>\d+)\s+{units}|{units}\s*[:=]?\s+(?P<after>\d+))\b", part, re.I)
    if match and not _BEFORE.search(part[:match.start()]):
        return match.group(match.lastgroup)
    return None


def _record_facts(facts: dict[str, str], text: str) -> None:
    for part in _SENTENCE.split(text):
        if part.rstrip().endswith("?") and not _CORRECTION.search(part):
            continue
        for label, units in _COUNT_UNITS.items():
            if value := _count(part, units):
                facts[label] = value
        if re.search(r"\b(?:мощност\w*|у нас|наш\w* объект\w*)\b", part, re.I):
            positive = _OLD_VALUE.sub("", part)
            if (match := _POWER_VALUE.search(positive)) and not _BEFORE.search(positive[:match.start()]):
                facts["Мощность"] = match["value"]
        if match := _DAY_NIGHT.search(part):
            absent = re.search(r"\bбез\s*$", part[:match.start()], re.I) or re.search(r"\bнет\b", part, re.I)
            facts["Разбивка по времени суток"] = "отсутствует" if absent else "день и ночь"
        elif _CORRECTION.search(part) and re.search(r"\bсуточн\w+ итог\b", part, re.I):
            facts["Разбивка по времени суток"] = "отсутствует"


def _update_task(task: str, old: dict[str, str], current: dict[str, str]) -> str:
    for name, value in current.items():
        previous = old.get(name)
        if not previous or value == previous:
            continue
        if name in _COUNT_UNITS:
            units = _COUNT_UNITS[name]
            task = re.sub(rf"\b{re.escape(previous)}(?=\s+{units}\b)", value, task, flags=re.I)
            task = re.sub(rf"(\b{units}\s*[:=]?\s+){re.escape(previous)}\b",
                          lambda match: match[1] + value, task, flags=re.I)
        elif name == "Мощность":
            task = re.sub(rf"(?<!\w){re.escape(previous)}(?!\w)", value, task, flags=re.I)
        else:
            task = re.sub(r"\b(?:с|без)\s+разбивк\w*\s+на\s+день\s+и\s+ночь",
                          "без разбивки на день и ночь" if value == "отсутствует" else "с разбивкой на день и ночь",
                          task, flags=re.I)
    return task


@dataclass
class _Topic:
    task: str
    facts: dict[str, str] = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)
    correction: str = ""
    detail: str = ""


@dataclass(frozen=True)
class TurnContext:
    topic: str | None
    task: str
    facts: dict[str, str]
    query: str
    history: tuple[Turn, ...]
    outside: bool


def _subjects(text: str) -> list[str]:
    return [name for name, pattern in _TOPICS.items() if pattern.search(text)]


def analyze_history(history: tuple[Turn, ...], *, initial_task: str = "",
                    initial_facts: dict[str, str] | None = None) -> TurnContext:
    """Replay the dialogue. Only client statements update the task and its facts."""
    topics: dict[str, _Topic] = {}
    active = _Topic(initial_task, dict(initial_facts or {}))
    active_name = next(iter(_subjects(initial_task)), None)
    if active_name:
        topics[active_name] = active
    outside = False
    for turn in history:
        if turn.role == "assistant":
            if not outside and active.turns and not is_instruction(turn.text):
                active.turns.append(Turn("assistant", redact_input(turn.text)))
            continue
        text = redact_input(turn.text.strip())
        outside = is_instruction(text) or bool(_OUTSIDE.search(text) and not _subjects(text))
        if outside:
            continue
        switch = _SWITCH.match(text)
        content = task_text(text)
        subject = text[switch.end():] if switch else text
        subjects = _subjects(subject)
        new_name = subjects[0] if subjects else None
        if active_name in subjects and not switch:
            new_name = active_name
        if switch and not new_name and _RETURN.match(text):
            new_name = next((name for name in reversed(topics) if name != active_name), None)
        if new_name and new_name != active_name:
            if not switch and active_name is None and active.task and not topics:
                active.task = subject[:300]
            else:
                active = topics.get(new_name) or _Topic("")
            active_name = new_name
            topics.pop(active_name, None)
            topics[active_name] = active
        elif switch and not new_name:
            active, active_name = _Topic(""), None
        if not active.task and len(content) > 12 and (active_name or not _ELLIPSIS.match(text)):
            active.task = (subject if switch else text).strip()[:300] if not requests_callback(text) else content[:300]
        old = active.facts.copy()
        _record_facts(active.facts, text)
        active.task = _update_task(active.task, old, active.facts)
        changed = old != active.facts
        active.detail = _update_task(active.detail, old, active.facts)
        if _CORRECTION.search(text):
            active.correction = content[:300]
        elif changed or (not text.endswith("?") and len(content) > 15):
            if changed:
                active.correction = ""
            active.detail = content[:300]
        active.turns.append(Turn("user", text))

    latest = task_text(redact_input(history[-1].text)) if history and not outside else ""
    if not active.task and _ELLIPSIS.match(latest):
        latest = ""
    fact_terms = ", ".join(f"{name}: {value}" for name, value in active.facts.items())
    parts = (latest[:2_000], active.task, active.correction, active.detail, fact_terms)
    search_terms = (_OLD_VALUE.sub("", part).strip(" .!?") for part in parts)
    query = "\n".join(dict.fromkeys(filter(None, search_terms)))
    return TurnContext(active_name, active.task, active.facts, query, tuple(active.turns[-12:]), outside)
