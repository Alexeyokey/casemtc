"""Adapt the shared async consultant to the synchronous chat service."""
from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path

import httpx

from consultation import Turn
from core.contracts import (
    ActionType, ClientContext, ContactInfo, ConsultationRequest, ConsultationResult,
    ContactStatus, Evidence, LeadDraft, LeadSummary, StageTiming,
)
from core.phone import extract_phone
from knowledge_base import KnowledgeBase
from organizer_api import APIConfig, OrganizerClient
from rag_consultation import RagConsultant, RagTurn, configured_top_k, requests_callback

from .redaction import redact_text


_REFUSAL = re.compile(
    r"не (?:буду|хочу) (?:оставлять|давать|называть) (?:свой )?(?:телефон|номер|контакты)"
    r"|не (?:звоните|перезванивайте)|без (?:контактов|телефона)|не дам (?:телефон|номер)",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CORRECTION = re.compile(r"^\s*(?:уточню|уточняю|уточнение|поправка|исправлюсь|на самом деле)\b", re.IGNORECASE)
_SELF_REPORT = re.compile(r"\b(?:у нас|на нашем объекте|установлено)\b", re.IGNORECASE)
_COUNTER = r"(?:электро)?сч[её]тчик[а-яё]*"
_COUNTER_COUNT = re.compile(
    rf"\b(?:(?P<before>\d+)\s+{_COUNTER}|{_COUNTER}\s*[:=]?\s+(?P<after>\d+))\b", re.IGNORECASE,
)
_COUNTER_REPLACEMENT = re.compile(
    rf"\bне\s+\d+(?:\s+{_COUNTER}\s*,?\s*а\s+(?P<before>\d+)"
    rf"|\s*,?\s*а\s+(?P<after>\d+)\s+{_COUNTER})\b", re.IGNORECASE,
)
_QUALIFIED_COUNT = re.compile(r"\b(?:не|менее|более|меньше|больше|до|около|примерно|порядка)\s*$", re.IGNORECASE)
_COUNTER_FACT = "Количество счётчиков"


def _remember_task(ctx: ClientContext, message: str) -> None:
    text = redact_text(message).strip()
    if ctx.topic is None and len(text) > 12:
        ctx.topic = text[:300]

    for part in _SENTENCE.split(text):
        explicit_correction = _CORRECTION.search(part)
        if part.endswith("?") and not explicit_correction:
            continue
        replacement = _COUNTER_REPLACEMENT.search(part)
        correction = replacement or explicit_correction or ", а не " in part.casefold()
        count = replacement or _COUNTER_COUNT.search(part)
        if count and (correction or _SELF_REPORT.search(part)) and (
            replacement or not _QUALIFIED_COUNT.search(part[:count.start(count.lastgroup)])
        ):
            value = count.group(count.lastgroup)
            prior = ctx.facts.get(_COUNTER_FACT)
            ctx.facts[_COUNTER_FACT] = value
            matches = list(_COUNTER_COUNT.finditer(ctx.topic or ""))
            if len(matches) == 1 and (prior is not None or correction):
                previous = matches[0]
                start, end = previous.span(previous.lastgroup)
                if prior is None or previous.group(previous.lastgroup) == prior:
                    ctx.topic = ctx.topic[:start] + value + ctx.topic[end:]
        elif correction:
            ctx.facts["Уточнение клиента"] = part[:300]


class RagEngine:
    def __init__(self, knowledge: KnowledgeBase, config: APIConfig, *, top_k: int = 5,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.knowledge = knowledge
        self.config = config
        self.top_k = top_k
        self.transport = transport
        self.slots = threading.BoundedSemaphore(config.max_workers)

    def consult(self, request: ConsultationRequest) -> ConsultationResult:
        ctx = request.client_context.model_copy(deep=True)
        message = request.message
        refusal = _REFUSAL.search(message)
        if refusal:
            ctx.contact_status = ContactStatus.refused
            ctx.callback_requested = False
            ctx.contact = ContactInfo()
            remainder = (message[:refusal.start()] + " " + message[refusal.end():]).lstrip(" .,!?:;—-\n\t").rstrip()
            if len(remainder) <= 12:
                return self._result(request, ctx, ActionType.answer,
                                    "Хорошо, продолжим консультацию без заявки. Чем ещё помочь?")
            _remember_task(ctx, remainder)

        callback = refusal is None and requests_callback(message)
        if callback:
            ctx.callback_requested = True
            ctx.contact_status = ContactStatus.requested
            task = " ".join(part.strip() for part in _SENTENCE.split(message)
                            if not requests_callback(part))
            _remember_task(ctx, task)
        phone = extract_phone(message) if ctx.callback_requested else None
        if phone:
            ctx.contact.phone = phone
            ctx.contact_status = ContactStatus.phone_valid

        if callback or phone:
            return self._contact_result(request, ctx)

        if refusal is None:
            _remember_task(ctx, message)
        history = tuple(Turn(item.role.value, redact_text(item.content)) for item in request.history)
        with self.slots:
            turn = asyncio.run(self._consult((*history, Turn("user", redact_text(message)))))
        reply = turn.reply
        if reply.action == "offer_contact":
            if ctx.contact_status == ContactStatus.refused:
                return self._result(request, ctx, ActionType.clarify,
                                    "Продолжим без заявки. Уточните, пожалуйста, какую задачу нужно решить.",
                                    turn=turn)
            ctx.callback_requested = True
            ctx.contact_status = ContactStatus.requested
            return self._contact_result(request, ctx, turn)

        action = ActionType.clarify if reply.action == "clarify" else ActionType.answer
        lead = self._draft(ctx) if ctx.callback_requested or ctx.contact.phone else None
        return self._result(request, ctx, action, reply.text, lead, turn)

    async def _consult(self, history: tuple[Turn, ...]) -> RagTurn:
        # The client lives on the event loop of this turn and closes there.
        async with OrganizerClient(self.config, transport=self.transport) as client:
            return await RagConsultant(self.knowledge, client.chat, top_k=self.top_k).consult_turn(history)

    def _contact_result(self, request: ConsultationRequest, ctx: ClientContext,
                        turn: RagTurn | None = None) -> ConsultationResult:
        ctx.contact_status = ContactStatus.phone_valid if ctx.contact.phone else ContactStatus.requested
        action = ActionType.lead_ready if ctx.contact.phone else ActionType.collect_contacts
        answer = ("Проверьте контакты и резюме, затем подтвердите сохранение заявки." if ctx.contact.phone
                  else "Оставьте телефон для связи со специалистом. ФИО и компанию можно указать по желанию.")
        return self._result(request, ctx, action, answer, self._draft(ctx), turn)

    @staticmethod
    def _draft(ctx: ClientContext) -> LeadDraft:
        summary = LeadSummary(
            task=ctx.topic or "Клиент попросил связаться со специалистом",
            known_facts=[f"{key}: {value}" for key, value in ctx.facts.items()],
            assumptions=[f"{key}: {value}" for key, value in ctx.assumptions.items()],
            unknowns=[label for name, label in (("full_name", "ФИО"), ("company", "Компания"),
                                               ("region", "Регион")) if not getattr(ctx.contact, name)],
        )
        return LeadDraft(contact=ctx.contact, callback_requested=ctx.callback_requested, summary=summary)

    @staticmethod
    def _result(request: ConsultationRequest, ctx: ClientContext, action: ActionType, answer: str,
                lead: LeadDraft | None = None, turn: RagTurn | None = None) -> ConsultationResult:
        return ConsultationResult(
            session_id=request.session_id, request_id=request.request_id,
            answer=answer, action=action,
            clarifying_question=answer if action == ActionType.clarify else None,
            client_context=ctx, lead_draft=lead,
            used_evidence_ids=list(turn.reply.evidence_ids) if turn and answer == turn.reply.text else [],
            retrieved=[
                Evidence(evidence_id=item.id, doc_id=item.path, section=item.heading,
                         text=item.text, retriever="lexical") for item in turn.retrieved
            ] if turn and request.debug else [],
            timings=[StageTiming(name=name, ms=ms) for name, ms in turn.timings] if turn else [],
        )


def create_rag_engine() -> RagEngine:
    archive = os.getenv("ENERGYTOOL_KB_PATH")
    if not archive:
        raise ValueError("Укажите путь к архиву базы знаний в ENERGYTOOL_KB_PATH.")
    config = APIConfig.from_env()
    return RagEngine(KnowledgeBase.from_zip(Path(archive)), config, top_k=configured_top_k())
