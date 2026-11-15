"""HTTP-контракты backend ↔ web (U1–U4). Версия совпадает с core.CONTRACT_VERSION."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from core.contracts import CONTRACT_VERSION, ActionType, ContactInfo, Evidence, LeadSummary, StageTiming


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(_M):
    code: str = Field(description="Машинный код: session_not_found, session_closed, turn_in_progress, "
                                  "request_id_conflict, engine_error, turn_cancelled, invalid_phone, "
                                  "phone_required, debug_disabled, archive_forbidden, validation_error")
    message: str = Field(description="Текст, который можно показать пользователю")
    request_id: Optional[str] = None
    retryable: bool = False


class ErrorResponse(_M):
    error: ErrorBody


# ---------------- sessions ----------------
class SessionCreateRequest(_M):
    close_session_id: Optional[str] = Field(
        default=None, description="Закрыть прежнюю сессию этого клиента (кнопка «Новый диалог»)"
    )


class SessionCreated(_M):
    contract_version: str = CONTRACT_VERSION
    session_id: str
    client_token: str = Field(description="Секрет вкладки; передавать в X-Client-Token")
    status: Literal["active"] = "active"
    created_at: str


class MessageView(_M):
    id: int
    seq: int
    role: Literal["user", "assistant"]
    content: str
    request_id: str
    in_context: bool
    created_at: str


class SessionView(_M):
    contract_version: str = CONTRACT_VERSION
    session_id: str
    status: Literal["active", "closed"]
    created_at: str
    closed_at: Optional[str] = None
    messages: list[MessageView]
    lead: Optional["LeadView"] = None


# ---------------- turns ----------------
class TurnRequest(_M):
    request_id: str = Field(min_length=1, max_length=64, description="UUID, генерируется клиентом "
                            "на каждую новую реплику; при повторе отправки — тот же")
    text: str = Field(min_length=1, max_length=10_000)


class TurnResponse(_M):
    contract_version: str = CONTRACT_VERSION
    session_id: str
    request_id: str
    status: Literal["completed"] = "completed"
    user_message_id: int
    assistant_message_id: int
    answer: str
    action: ActionType
    clarifying_question: Optional[str] = None
    lead: Optional["LeadView"] = None
    debug_available: bool = False
    replayed: bool = Field(default=False, description="true — ответ возвращён повторно по тому же request_id")


# ---------------- leads ----------------
LeadStatus = Literal["none", "draft", "ready", "saved"]


class LeadView(_M):
    lead_id: Optional[str] = None
    session_id: str
    status: LeadStatus = Field(description="none — нет данных; draft — нет валидного телефона; "
                                           "ready — можно сохранить; saved — сохранена")
    contact: ContactInfo
    contact_refused: bool = False
    callback_requested: bool = False
    missing_fields: list[str] = Field(default_factory=list, description="Что ещё можно предложить заполнить")
    summary: Optional[LeadSummary] = None
    offer_id: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
        description="Непрозрачный идентификатор версии контактов и резюме для подтверждения",
    )
    revision: int = 0
    saved_at: Optional[str] = None
    delivery: Literal["stored_only"] = Field(
        default="stored_only",
        description="Заявка только сохраняется в системе; CRM/почта/уведомления не подключены",
    )
    status_text: str = Field(description="Честное описание фактического состояния для пользователя")


class ContactsUpdate(_M):
    """Частичное обновление: переданные поля перезаписываются, null очищает поле."""

    full_name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=40)
    company: Optional[str] = Field(default=None, max_length=300)
    region: Optional[str] = Field(default=None, max_length=200)
    callback_requested: Optional[bool] = None


class LeadSaveRequest(_M):
    idempotency_key: str = Field(min_length=1, max_length=64)
    offer_id: str = Field(pattern=r"^[0-9a-f]{64}$")


# ---------------- debug ----------------
class TraceView(_M):
    contract_version: str = CONTRACT_VERSION
    trace_id: str
    session_id: str
    request_id: str
    attempt: int
    status: Literal["completed", "failed", "cancelled"]
    engine: str
    created_at: str
    timings: list[StageTiming]
    retrieved: list[Evidence]
    used: list[Evidence] = Field(description="Подмножество retrieved, на которое опирается ответ")
    used_missing_ids: list[str] = Field(
        default_factory=list, description="id из used_evidence_ids, которых нет среди retrieved — дефект"
    )
    action: Optional[str] = None
    error: Optional[dict[str, Any]] = None


SessionView.model_rebuild()
TurnResponse.model_rebuild()
