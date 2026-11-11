"""Common conversation values for the web and platform adapters."""

import re
from dataclasses import dataclass
from typing import Literal


Role = Literal["user", "assistant"]
Action = Literal["answer", "clarify", "offer_contact"]
ROLE_PREFIX = re.compile(r"^(Пользователь|Ассистент):[ \t]*", re.MULTILINE)


@dataclass(frozen=True)
class Turn:
    role: Role
    text: str


@dataclass(frozen=True)
class Reply:
    text: str
    action: Action
    summary: str | None = None
    evidence_ids: tuple[str, ...] = ()


def parse_csv_history(question: str) -> tuple[Turn, ...]:
    """Read one CSV cell as a standalone question or a labelled transcript."""
    question = question.strip()
    if not question:
        raise ValueError("Вопрос пуст.")

    markers = list(ROLE_PREFIX.finditer(question))
    if not markers or markers[0].start() != 0:
        return (Turn("user", question),)

    turns = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(question)
        text = question[marker.end():end].strip()
        if not text:
            raise ValueError("В диалоге есть пустая реплика.")
        role: Role = "user" if marker[1] == "Пользователь" else "assistant"
        turns.append(Turn(role, text))

    if turns[0].role != "user" or turns[-1].role != "user":
        raise ValueError("Диалог должен начинаться и заканчиваться репликой пользователя.")
    return tuple(turns)
