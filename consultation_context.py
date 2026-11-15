"""Resolve the current consultation from the user turns of one conversation."""

import re
from collections.abc import Iterator
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
    r"\b(?:я\s+)?не\s+(?:(?:буду|хочу|готов\w*|надо|нужно)\s+"
    r"(?:(?:оставл[яе]ть|оставить|давать|дать|называть|предоставлять)\s+"
    r"(?:свой\s+)?(?:телефон|номер|контакты|заявку)|(?:мне\s+)?(?:пере)?звонить)|"
    r"(?:пере)?звоните(?:\s+мне)?|перезванивайте(?:\s+мне)?|оставляйте\s+заявку|дам\s+(?:телефон|номер))"
    r"|^\s*(?:давайте\s+)?без\s+(?:контактов|телефона|заявки)\b",
    re.I,
)
_CALLBACK = re.compile(
    r"\b(?:перезвоните(?:\s+мне)?|позвоните мне|свяжитесь со мной|позовите специалиста|"
    r"(?:хочу|можно)\s+(?:оставить заявку|(?:связаться|поговорить)\s+со специалистом)|"
    r"(?:оформите|примите)\s+заявку|свяжите(?:\s+меня)?\s+с(?:о специалистом| менеджером))\b",
    re.I,
)
_QUOTED = re.compile(r'«[^»\n]*»|"[^"\n]*"')
_CONTACT_INFORMATION = re.compile(r"\b(?:как|что|где|зачем|когда|куда|чтобы|если)\b", re.I)
_CONTACT_NEGATION = re.compile(r"\bне\s+(?:надо|нужно|стоит)\b", re.I)
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
_COUNT_HEADINGS = {
    "Количество счётчиков": r"(?:электро)?сч[её]тчик(?:и|ов)?",
    "Количество объектов": r"(?:объект(?:ы|ов)?|филиал(?:ы|ов)?|здани(?:е|я|й))",
}
_BEFORE = re.compile(r"\b(?:не|менее|более|меньше|больше|до|около|примерно|порядка|возможно)\s*$", re.I)
_POWER_VALUE = re.compile(rf"\b(?P<value>{_NUMBER}\s*{_POWER})\b", re.I)
_POWER_CONTEXT = re.compile(r"\b(?:мощност\w*|у нас|наш\w* объект\w*|сейчас)\b", re.I)
_DAY_NIGHT = re.compile(r"(?:разбив\w*|делени\w*).{0,65}\bдень\b.{0,15}\bночь\b", re.I)
_DAY_NIGHT_FACT = "Разбивка по времени суток"
_PLAN_WORDS = r"план\w*|проектн\w*|буд[еу]\w*|в следующ\w+|в перспективе"
_PLAN = re.compile(rf"\b(?:{_PLAN_WORDS})\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(?:если|допустим|предположим|гипотетически|например|могл\w*\s+бы)\b", re.I)
_REQUIREMENT = re.compile(r"\b(?:нужн\w*|необходим\w*|должн\w*|хотим|следует|надо|можно)\b", re.I)
_FACT_CLAUSES = re.compile(
    rf";\s*|(?:,\s*(?:(?:а|и|но)\s+)?|\s+(?:а|и|но)\s+)"
    rf"(?=(?:{_PLAN_WORDS}|сейчас|у нас|текущ\w*|фактическ\w*|действующ\w*)\b)",
    re.I,
)
_PLANNED_FACTS = {
    "Мощность": "Планируемая мощность",
    "Количество счётчиков": "Планируемое количество счётчиков",
    "Количество объектов": "Планируемое количество объектов",
}
_NAMED_OBJECT = re.compile(
    r"\b(?P<kind>(?i:склад|завод|площадк|объект|здани|филиал|бизнес-центр))[а-яё]*"
    r"[ \t]+(?P<name>«[^»\n]{1,60}»|\"[^\"\n]{1,60}\"|(?:№\s*|#)\d{1,8}|[А-ЯЁA-Z][\w-]{0,59})(?![\w-])",
)
_ANOTHER_OBJECT = re.compile(
    r"\b(?:друг\w*|нов\w*|соседн\w*|ещ[её]\s+одн\w*)\s+"
    r"(?:склад\w*|завод\w*|площадк\w*|объект\w*|здани\w*|филиал\w*|бизнес-центр\w*)\b",
    re.I,
)


def is_instruction(text: str) -> bool:
    return bool(_INSTRUCTION.search(text))


def requests_callback(text: str) -> bool:
    return not refuses_contact(text) and any(_contact_matches(text, _CALLBACK))


def refuses_contact(text: str) -> bool:
    return any(_contact_matches(text, _REFUSAL))


def _contact_matches(text: str, pattern: re.Pattern[str]) -> Iterator[re.Match[str]]:
    for sentence in _SENTENCE.split(text):
        yield from _contact_matches_in_sentence(sentence, pattern)


def _contact_matches_in_sentence(sentence: str, pattern: re.Pattern[str]) -> Iterator[re.Match[str]]:
    unquoted = _QUOTED.sub(lambda match: " " * len(match[0]), sentence)
    for match in pattern.finditer(unquoted):
        before = unquoted[:match.start()]
        clause = re.split(r"[,;]|\bи(?=\s*$)", before, flags=re.I)[-1]
        if _CONTACT_INFORMATION.search(clause):
            continue
        if pattern is _CALLBACK and _CONTACT_NEGATION.search(clause):
            continue
        yield match


def redact_input(text: str) -> str:
    return _REDACT.sub("[скрыто]", text)


def task_text(text: str) -> str:
    request_pattern = _REFUSAL if refuses_contact(text) else _CALLBACK
    parts = []
    for part in _SENTENCE.split(text):
        spans = [match.span() for match in _contact_matches_in_sentence(part, request_pattern)]
        for start, end in reversed(spans):
            part = part[:start] + part[end:]
        part = part.strip(" ,.!?—-")
        if spans:
            part = re.sub(r"\s+и$", "", part, flags=re.I)
        if spans:
            part = re.sub(r"\s+(?:а|и|но)$", "", part, flags=re.I)
        if part and part.casefold() not in {"а", "и", "но", "пожалуйста", "да", "нет", "спасибо", "[скрыто]"}:
            parts.append(part)
    return " ".join(parts)


def _count(part: str, units: str, heading: str) -> str | None:
    replacement = re.search(
        rf"\bне\s+\d+\s+{units},?\s+а\s+(?P<before>\d+)\b"
        rf"|\bне\s+\d+,?\s+а\s+(?P<after>\d+)\s+{units}\b",
        part, re.I,
    )
    if replacement:
        return replacement.group(replacement.lastgroup)
    count_unit = "|".join(_COUNT_UNITS.values())
    match = re.search(
        rf"\b(?:(?P<before>\d+)\s+{units}\b|{heading}\b\s*[:=—–-]?\s*"
        rf"(?P<after>\d+)\b(?!\s+(?:{count_unit})\b))",
        part, re.I,
    )
    if match and not _BEFORE.search(part[:match.start()]):
        return match.group(match.lastgroup)
    return None


def _record_facts(facts: dict[str, str], text: str) -> None:
    for sentence in _SENTENCE.split(text):
        if sentence.rstrip().endswith("?") and not _CORRECTION.search(sentence):
            continue
        if _HYPOTHETICAL.search(sentence):
            continue
        for part in _FACT_CLAUSES.split(sentence):
            _record_clause(facts, part)


def _record_clause(facts: dict[str, str], part: str) -> None:
    planned = bool(_PLAN.search(part))
    if not planned and _REQUIREMENT.search(part) and not _CORRECTION.search(part):
        return
    for label, units in _COUNT_UNITS.items():
        if value := _count(part, units, _COUNT_HEADINGS[label]):
            facts[_PLANNED_FACTS[label] if planned else label] = value
    if planned or _POWER_CONTEXT.search(part) or _CORRECTION.search(part):
        positive = _OLD_VALUE.sub("", part)
        if (match := _POWER_VALUE.search(positive)) and not _BEFORE.search(positive[:match.start()]):
            facts[_PLANNED_FACTS["Мощность"] if planned else "Мощность"] = match["value"]
    if not planned:
        if match := _DAY_NIGHT.search(part):
            absent = re.search(r"\bбез\s*$", part[:match.start()], re.I) or re.search(r"\bнет\b", part, re.I)
            facts[_DAY_NIGHT_FACT] = "отсутствует" if absent else "день и ночь"
        elif _CORRECTION.search(part) and re.search(r"\bсуточн\w+ итог\b", part, re.I):
            facts[_DAY_NIGHT_FACT] = "отсутствует"


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
        elif name == _DAY_NIGHT_FACT:
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


def _object_names(text: str) -> set[str]:
    return {
        match["kind"].casefold() + " " + "".join(match["name"].strip('«»"').casefold().split())
        for match in _NAMED_OBJECT.finditer(text)
    }


def analyze_history(history: tuple[Turn, ...], *, initial_task: str = "",
                    initial_facts: dict[str, str] | None = None) -> TurnContext:
    """Replay the dialogue. Only client statements update the task and its facts."""
    topics: dict[tuple[str | None, str | None], _Topic] = {}
    active = _Topic(initial_task, dict(initial_facts or {}))
    active_name = next(iter(_subjects(initial_task)), None)
    initial_objects = _object_names(initial_task)
    active_object = next(iter(initial_objects)) if len(initial_objects) == 1 else None
    if active_name or active_object:
        topics[(active_name, active_object)] = active
    outside = False
    for index, turn in enumerate(history):
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
        named_objects = _object_names(subject)
        multiple_objects = len(named_objects) > 1
        another_object = bool(_ANOTHER_OBJECT.search(subject))
        new_object = None
        if named_objects or another_object:
            if multiple_objects or not named_objects:
                new_object = f"anonymous:{index}"
            else:
                new_object = next(iter(named_objects))
            new_name = new_name or active_name
        elif switch and _RETURN.match(text):
            for name, obj in reversed(topics):
                if (name, obj) != (active_name, active_object) and (new_name is None or name == new_name):
                    new_name, new_object = name, obj
                    break
        elif new_name == active_name or (new_name is None and not switch):
            new_name, new_object = active_name, active_object

        new_scope = (new_name, new_object)
        if new_scope != (active_name, active_object):
            if not switch and not active_name and active.task and not topics and not new_object:
                active.task = subject[:300]
            else:
                active = topics.get(new_scope) or _Topic("")
        elif switch and not any(new_scope):
            active = _Topic("")
        active_name, active_object = new_scope
        if any(new_scope):
            topics.pop(new_scope, None)
            topics[new_scope] = active
        if not active.task and len(content) > 12 and (active_name or not _ELLIPSIS.match(text)):
            active.task = (content if requests_callback(text) or refuses_contact(text) else subject.strip())[:300]
        old = active.facts.copy()
        if not multiple_objects:
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
