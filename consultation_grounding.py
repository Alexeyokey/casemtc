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
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|,\s+|\n+")
_SOFT_LINE_BREAK = re.compile(r"(?<!\n)\n(?![ \t]*(?:\n|(?:[-+*]|\d+[.)])[ \t]+|\|))")
_WORDS = re.compile(r"\b[а-яa-z]{4,}\b")
_NEGATED_PREDICATE = re.compile(
    r"\bне\s+"
    r"(?:(?:всегда|можно|может|могут|следует|нужно|обязательно|будет|будут)\s+)*"
    r"(?P<predicate>[а-я]{4,})\b"
)
_NON_PREDICATES = {
    "более", "выше", "дольше", "менее", "меньше", "ниже", "позднее", "раньше", "ранее", "только",
}
_QUANTITY_FILLER = re.compile(
    r"^(?:более|возмож|друг|занима|менее|может|можно|нужн|около|период|потреб|"
    r"пример|состав|срок|требу|хвати|через)",
)
_QUANTITY_RELATIONS = (
    re.compile(r"\b(?:хран\w*|глубин\w*)\b"),
    re.compile(r"\b(?:подключ\w*|внедр\w*|монтаж\w*)\b"),
)


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


def _terms(text: str) -> set[str]:
    return {word[:5] for word in _WORDS.findall(text) if not _QUANTITY_FILLER.match(word)}


def _negated_terms(text: str) -> set[str]:
    return {
        match["predicate"][:5] for match in _NEGATED_PREDICATE.finditer(text)
        if match["predicate"] not in _NON_PREDICATES
    }


def _contradicts_negation(answer: str, quote: str) -> bool:
    answer_negated, quote_negated = _negated_terms(answer), _negated_terms(quote)
    shared = _terms(answer) & _terms(quote)
    reversed_terms = shared & (answer_negated ^ quote_negated)
    return bool(reversed_terms and shared - reversed_terms)


def _same_quantity_context(answer: str, quote: str) -> bool:
    # Match what the number describes, excluding the quantity itself and generic
    # expressions like "period" or "may take". Keep distinct operations apart.
    answer_context = _NUMBER.sub(" ", answer)
    quote_context = _NUMBER.sub(" ", quote)
    answer_relations = {pattern for pattern in _QUANTITY_RELATIONS if pattern.search(answer_context)}
    quote_relations = {pattern for pattern in _QUANTITY_RELATIONS if pattern.search(quote_context)}
    if answer_relations and answer_relations != quote_relations:
        return False
    return bool(_terms(answer_context) & _terms(quote_context))


def _valid_support(raw: object, evidence: dict[str, Evidence], used: list[str]) -> dict[str, str] | None:
    if not isinstance(raw, dict) or set(raw) != set(used):
        return None
    support = {}
    for source_id, value in raw.items():
        if not isinstance(value, str) or not 12 <= len(value.strip()) <= 2_000:
            return None
        article = _SOFT_LINE_BREAK.sub(" ", unicodedata.normalize("NFKC", evidence[source_id].text).casefold())
        # A Markdown soft wrap does not end a clause. A quote after that wrap
        # could otherwise omit the negation or condition at the previous line.
        quote = r"\s+".join(re.escape(word) for word in _normalized(value).split())
        match = re.search(
            rf"(?:^|(?<=[.!?])(?=\s)|(?<=\n))\s*(?P<quote>{quote})"
            rf"(?:[.!?](?=\s|$)|(?<=[.!?])(?=\s|$)|(?=[ \t]*(?:\n|$)))",
            article,
        )
        if match is None:
            return None
        support[source_id] = match["quote"]
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
    proof_clauses = [
        (part, _amounts(part)) for part in _CLAUSE.split(proof.casefold().replace("ё", "е")) if part
    ]
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
        amounts = _amounts(sentence)
        client_amounts = stated_amounts if _CLIENT_REPORT.match(sentence) and not commerce else set()
        for statement in _CLAUSE.split(sentence.casefold().replace("ё", "е")):
            if any(_contradicts_negation(statement, quote) for quote, _ in proof_clauses):
                return fallback
            for amount in _amounts(statement) - client_amounts:
                if not any(amount in quoted and _same_quantity_context(statement, quote)
                           for quote, quoted in proof_clauses):
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
