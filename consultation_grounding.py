"""Check the organizer model's reply before exposing it through web or CSV."""

import json
import re
import unicodedata

from consultation import Reply
from consultation_context import is_instruction, redact_input
from knowledge_base import Evidence


NO_EVIDENCE = (
    "В базе пока недостаточно данных для ответа. "
    "Уточните задачу и тип объекта, если они ещё не указаны."
)
ESCALATE = (
    "В базе знаний нет достаточного подтверждения для вашей ситуации. "
    "Эти условия стоит уточнить у специалиста МТС EnergyTool."
)
OUT_OF_SCOPE = "Я могу помочь с задачами по энергопотреблению и продуктам МТС EnergyTool."
_UNSAFE_OUTPUT = re.compile(
    r"<[^>]+>|\x60\x60\x60|https?://|\b[\w./-]+\.(?:md|zip|env|csv|json|py)\b|"
    r"\bauthorization\b",
    re.I,
)
_PROMISE = re.compile(
    r"\b(?:гарантиру\w*|гарантирован\w*|обеща\w*|точно\s+(?:сэкономи\w*|окуп\w*|сниз\w*|допуст\w*)|"
    r"обязательно\s+(?:окуп\w*|сэконом\w*|перезвон\w*)|перезвоним|мы\s+свяжемся)\b",
    re.I,
)
_COMMERCE = re.compile(
    r"\b(?:цен[аыеу]|стоим\w*|стои[тм]\w*|тариф\w*|бесплат\w*|скидк\w*|окуп\w*|"
    r"эконом\w*|сэконом\w*|выгод\w*)\b",
    re.I,
)
_OREM_ASSURANCE = re.compile(
    r"\b(?:гарантирован\w*|точно|можете|сможете)\s+(?:выйти|выходить|попасть)\s+на\s+орэм\b|"
    r"\b(?:достаточн\w*|хватит)\s+(?:\w+\s+){0,4}(?:выход\w*|допуск\w*)\s+на\s+орэм\b",
    re.I,
)
_GAP = re.compile(
    r"\b(?:уточн\w*|рассчита\w* индивидуально|зависит от|"
    r"нет (?:данных|подтверждения|информации)|не (?:указан\w*|извест\w*|подтвержд\w*|гаранти\w*))\b",
    re.I,
)
_CURRENCY = r"₽|руб(?:\.|л[а-яё]*)?|коп(?:\.|е[а-яё]*)?"
_UNITS = (
    rf"квт\s*[·*⋅-]?\s*ч|мвт\s*[·*⋅-]?\s*ч|гвт\s*[·*⋅-]?\s*ч|"
    rf"kw\s*h|mw\s*h|квт|мвт|гвт|вт|kw|mw|w|{_CURRENCY}|%|"
    r"процент\w*|лет|год\w*|месяц\w*|недел\w*|дн(?:я|ей)|сут(?:ок|ки)|час\w*|"
    r"(?:электро)?сч[её]тчик\w*|объект\w*|филиал\w*"
)
_NUMBER = re.compile(
    rf"(?<![\w.,])(?P<bound>не\s+менее|не\s+более|не\s+ниже|не\s+выше|"
    rf"как\s+минимум|от|до|менее|более|свыше|около|примерно)?\s*"
    rf"(?P<value>\d+(?:[ \u00a0\u202f]\d{{3}})*(?:[,.]\d+)?)"
    rf"(?:\s*[-–—]\s*(?P<end>\d+(?:[,.]\d+)?))?"
    rf"\s*(?P<unit>{_UNITS})?(?!\w)",
    re.I,
)
_UNIT_FAMILIES = (
    (r"руб.*|₽", "₽"),
    (r"коп.*", "коп."),
    (r"квт[·*⋅-]?ч|kwh", "квтч"),
    (r"мвт[·*⋅-]?ч|mwh", "мвтч"),
    (r"гвт[·*⋅-]?ч", "гвтч"),
    (r"kw", "квт"),
    (r"mw", "мвт"),
    (r"w", "вт"),
    (r"лет|год\w*", "год"),
    (r"месяц\w*", "месяц"),
    (r"час\w*", "час"),
    (r"дн\w*|сут\w*", "сутки"),
    (r"процент\w*", "%"),
    (r"(?:электро)?счётчик\w*|(?:электро)?счетчик\w*", "счетчик"),
    (r"объект\w*", "объект"),
    (r"филиал\w*", "филиал"),
)
_CLIENT_REPORT = re.compile(
    r"^\s*(?:вы (?:указали|сообщили)|по вашим данным|по вашему описанию|у вас)\b", re.I,
)
_LOCAL_SCOPE = re.compile(
    r"\b(?:только (?:для|при) [^,.!?;—]+|для пилот\w+|при условии [^,.!?;—]+)",
    re.I,
)
_FACT_UNITS = {"Количество счётчиков": "счётчиков", "Количество объектов": "объектов"}
_BOUND_ALIASES = {"как минимум": "не менее", "не ниже": "не менее", "не выше": "не более", "свыше": "более"}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _unit(text: str) -> str:
    unit = "".join(text.casefold().split())
    return next((name for pattern, name in _UNIT_FAMILIES if re.fullmatch(pattern, unit)), unit)


def _amounts(text: str) -> set[tuple[str, str, str, str]]:
    amounts = set()
    for match in _NUMBER.finditer(_normalized(text)):
        bound = match["bound"] or ""
        start = match["value"].replace(" ", "").replace(",", ".")
        end = (match["end"] or "").replace(" ", "").replace(",", ".")
        amounts.add((_BOUND_ALIASES.get(bound, bound), start, end, _unit(match["unit"] or "")))
    return amounts


def _valid_support(raw: object, evidence: dict[str, Evidence], used: list[str]) -> dict[str, str] | None:
    if not isinstance(raw, dict) or set(raw) != set(used):
        return None
    support = {}
    for source_id, value in raw.items():
        if not isinstance(value, str) or not 12 <= len(value.strip()) <= 2_000:
            return None
        article = unicodedata.normalize("NFKC", evidence[source_id].text).casefold()
        # Require a full sentence or line so the quote cannot drop a nearby negation.
        quote = r"\s+".join(re.escape(word) for word in _normalized(value).split())
        if not re.search(
            rf"(?:^|(?<=[.!?\n]))\s*{quote}(?:[.!?]|(?<=[.!?])|(?=[ \t]*(?:\n|$)))", article,
        ):
            return None
        support[source_id] = value
    return support


def safe_source(source: Evidence) -> bool:
    return not any(is_instruction(value) or redact_input(value) != value
                   for value in (source.path, source.heading, source.text))


def read_reply(raw: str, evidence: tuple[Evidence, ...], *, client_facts: dict[str, str]) -> Reply:
    fallback = Reply(ESCALATE, "escalate")
    lines = raw.strip().splitlines()
    if len(lines) > 2 and lines[0] in ("```json", "```") and lines[-1] == "```":
        raw = "\n".join(lines[1:-1])
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        return fallback
    if not isinstance(result, dict):
        return fallback

    text = result.get("text")
    action = result.get("action")
    used = result.get("evidence_ids")
    allowed = {source.id: source for source in evidence}
    if (
        not isinstance(text, str) or not text.strip() or len(text) > 10_000
        or len(text.split()) > 150
        or action not in ("answer", "clarify", "escalate", "out_of_scope")
        or not isinstance(used, list)
        or any(not isinstance(key, str) or key not in allowed for key in used)
        or redact_input(text) != text or is_instruction(text) or _UNSAFE_OUTPUT.search(text)
        or any(key in text for key in allowed)
    ):
        return fallback
    if action == "out_of_scope":
        return Reply(OUT_OF_SCOPE, "out_of_scope")
    support = _valid_support(result.get("support", {}), allowed, used)
    if support is None or (action == "answer" and not support):
        return fallback

    sentences = re.split(r"(?<=[.!?])\s+|\n+|[,;]\s+(?:но|однако|также)\s+", text)
    normalized_text = _normalized(text)
    proof = "\n".join(support.values())
    proven_amounts = _amounts(proof)
    scoped_amounts = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", proof):
        if scope := _LOCAL_SCOPE.search(part):
            scoped_amounts.append((_amounts(part), _normalized(scope[0])))
    stated_amounts = _amounts(" ".join(
        f"{value} {_FACT_UNITS.get(name, '')}" for name, value in client_facts.items()
    ))
    for sentence in sentences:
        commerce = _COMMERCE.search(sentence)
        assertion = re.sub(r"\bне\s+(?:гарантиру\w*|обеща\w*)", "", sentence, flags=re.I)
        if _PROMISE.search(assertion) or _OREM_ASSURANCE.search(assertion):
            return fallback
        provided_amounts = proven_amounts
        if _CLIENT_REPORT.match(sentence) and not commerce:
            provided_amounts = proven_amounts | stated_amounts
        amounts = _amounts(sentence)
        if amounts - provided_amounts:
            return fallback
        if any(amounts & restricted and scope not in normalized_text
               for restricted, scope in scoped_amounts):
            return fallback
        if commerce and not (_GAP.search(sentence) and not amounts):
            # Commercial statements are safe to reproduce only as a supported condition.
            if _normalized(sentence).strip(" .") not in _normalized(proof):
                return fallback
        if (action in ("clarify", "escalate") and not support and sentence.strip()
                and not sentence.rstrip().endswith("?") and not _GAP.search(sentence)):
            return fallback

    return Reply(text.strip(), action, evidence_ids=tuple(dict.fromkeys(used)))
