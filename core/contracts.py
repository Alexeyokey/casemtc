"""Общие контракты ядра консультации (S1, issue #2).

Модуль не зависит от HTTP, CSV, БД и UI: его импортируют и web-backend,
и solution.py. Любое изменение полей — отдельный PR с повышением
CONTRACT_VERSION по правилам из docs/contracts.md.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "1.0.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------
# Сообщения и контекст клиента
# --------------------------------------------------------------------------
class Role(str, Enum):
    user = "user"
    assistant = "assistant"


class Message(_Model):
    """Одна реплика ТЕКУЩЕГО диалога. Архив прошлых сессий сюда не попадает."""

    role: Role
    content: str = Field(min_length=1, max_length=20_000)


class ContactInfo(_Model):
    """Контакты клиента. Отсутствующее поле = None, никогда не выдумывается."""

    full_name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(
        default=None,
        description="Нормализованный телефон в формате E.164 (+7XXXXXXXXXX) или None",
        max_length=20,
    )
    company: Optional[str] = Field(default=None, max_length=300)
    region: Optional[str] = Field(default=None, max_length=200)


class ContactStatus(str, Enum):
    unknown = "unknown"        # ещё не спрашивали / клиент не ответил
    requested = "requested"    # консультант попросил контакты
    partial = "partial"        # есть что-то, но нет валидного телефона
    phone_valid = "phone_valid"  # валидный телефон есть — заявку можно сохранить
    refused = "refused"        # клиент явно отказался оставлять контакты


class ClientContext(_Model):
    """Состояние консультации текущей сессии.

    Владелец семантики полей facts/topic — C1 (Максим). Backend хранит объект
    как есть и передаёт его в следующий ход той же сессии; в новую сессию
    передаётся пустой ClientContext().
    """

    topic: Optional[str] = Field(default=None, description="Текущая тема/задача клиента")
    facts: dict[str, str] = Field(
        default_factory=dict,
        description="Известные факты о ситуации клиента, сказанные самим клиентом",
    )
    assumptions: dict[str, str] = Field(
        default_factory=dict, description="Предположения консультанта — не факты"
    )
    contact: ContactInfo = Field(default_factory=ContactInfo)
    contact_status: ContactStatus = ContactStatus.unknown
    callback_requested: bool = Field(
        default=False, description="Клиент явно попросил связаться/перезвонить"
    )
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Расширения C1 без смены версии контракта"
    )


# --------------------------------------------------------------------------
# Доказательства (R3, Влад)
# --------------------------------------------------------------------------
class Evidence(_Model):
    """Фрагмент БЗ, найденный поиском. В обычный ответ клиенту не выводится."""

    evidence_id: str = Field(description="Стабильный id фрагмента внутри индекса")
    doc_id: str
    doc_title: Optional[str] = None
    section: Optional[str] = None
    text: str = Field(description="Текст фрагмента (только в debug/trace)")
    score: Optional[float] = None
    retriever: Optional[str] = Field(default=None, description="bm25 / dense / hybrid / rerank")


# --------------------------------------------------------------------------
# Резюме и черновик заявки (S3)
# --------------------------------------------------------------------------
class LeadSummary(_Model):
    """Резюме для менеджера: актуальная задача, без пересказа всего диалога."""

    task: str = Field(min_length=1, description="Актуальная задача клиента")
    known_facts: list[str] = Field(default_factory=list, description="Сказано клиентом")
    assumptions: list[str] = Field(default_factory=list, description="Предположения, требуют проверки")
    discussed_solutions: list[str] = Field(
        default_factory=list, description="Продукты/решения, обсуждённые по БЗ"
    )
    unknowns: list[str] = Field(default_factory=list, description="Что ещё не известно")
    next_step: str = Field(default="Связаться с клиентом и уточнить неизвестные сведения")


class LeadDraft(_Model):
    contact: ContactInfo = Field(default_factory=ContactInfo)
    callback_requested: bool = False
    summary: Optional[LeadSummary] = None


# --------------------------------------------------------------------------
# Вход/выход одного хода
# --------------------------------------------------------------------------
class ActionType(str, Enum):
    answer = "answer"                    # полезный ответ по БЗ
    clarify = "clarify"                  # нужен факт клиента, меняющий подбор
    collect_contacts = "collect_contacts"  # просим ФИО/телефон/компанию
    lead_ready = "lead_ready"            # данных достаточно для сохранения заявки
    escalate = "escalate"                # в БЗ нет ответа — честно передаём специалисту
    out_of_scope = "out_of_scope"        # вне компетенции/prompt injection


class StageTiming(_Model):
    name: str = Field(description="retrieval / generation / total / ...")
    ms: float = Field(ge=0)
    status: Literal["ok", "error", "skipped"] = "ok"
    error: Optional[str] = None


class ConsultationRequest(_Model):
    contract_version: str = CONTRACT_VERSION
    session_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    history: list[Message] = Field(
        default_factory=list, description="Предыдущие реплики ТОЛЬКО этой сессии"
    )
    message: str = Field(min_length=1, max_length=10_000, description="Текущая реплика клиента")
    client_context: ClientContext = Field(default_factory=ClientContext)
    debug: bool = Field(default=False, description="Собирать retrieved evidence в результат")


class ConsultationResult(_Model):
    contract_version: str = CONTRACT_VERSION
    session_id: str
    request_id: str
    answer: str = Field(
        min_length=1,
        max_length=10_000,
        description="Текст для клиента: без имён файлов, rules и сырого RAG-контекста",
    )
    action: ActionType
    clarifying_question: Optional[str] = None
    used_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Фрагменты, на которые реально опирается ответ (подмножество retrieved)",
    )
    retrieved: list[Evidence] = Field(
        default_factory=list, description="Всё, что вернул поиск; заполняется при debug"
    )
    client_context: ClientContext = Field(
        default_factory=ClientContext, description="Обновлённое состояние для следующего хода"
    )
    lead_draft: Optional[LeadDraft] = None
    timings: list[StageTiming] = Field(default_factory=list)

    @model_validator(mode="after")
    def _used_subset_of_retrieved(self) -> "ConsultationResult":
        if self.retrieved:
            known = {e.evidence_id for e in self.retrieved}
            missing = [i for i in self.used_evidence_ids if i not in known]
            if missing:
                raise ValueError(f"used_evidence_ids not in retrieved: {missing}")
        if self.action == ActionType.clarify and not self.clarifying_question:
            raise ValueError("action=clarify requires clarifying_question")
        return self


__all__ = [
    "CONTRACT_VERSION",
    "ActionType",
    "ClientContext",
    "ConsultationRequest",
    "ConsultationResult",
    "ContactInfo",
    "ContactStatus",
    "Evidence",
    "LeadDraft",
    "LeadSummary",
    "Message",
    "Role",
    "StageTiming",
    "utcnow",
]
