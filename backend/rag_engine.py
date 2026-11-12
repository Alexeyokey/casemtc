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
    ActionType, ClientContext, ConsultationRequest, ConsultationResult,
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
        if _REFUSAL.search(message):
            ctx.contact_status = ContactStatus.refused
            ctx.callback_requested = False
            return self._result(request, ctx, ActionType.answer,
                                "Хорошо, продолжим консультацию без заявки. Чем ещё помочь?")

        callback = requests_callback(message)
        if callback:
            ctx.callback_requested = True
            ctx.contact_status = ContactStatus.requested
            task = " ".join(part.strip() for part in re.split(r"(?<=[.!?])\s+", message)
                            if not requests_callback(part))
            if len(task) > 12:
                ctx.topic = redact_text(task)[:300]
        phone = extract_phone(message) if ctx.callback_requested else None
        if phone:
            ctx.contact.phone = phone
            ctx.contact_status = ContactStatus.phone_valid

        if callback or phone:
            return self._contact_result(request, ctx)

        history = tuple(Turn(item.role.value, redact_text(item.content)) for item in request.history)
        with self.slots:
            turn = asyncio.run(self._consult((*history, Turn("user", redact_text(message)))))
        reply = turn.reply
        if reply.action == "offer_contact" and ctx.contact_status != ContactStatus.refused:
            ctx.callback_requested = True
            ctx.contact_status = ContactStatus.requested
            return self._contact_result(request, ctx, turn)

        if len(message.strip()) > 12:
            ctx.topic = redact_text(message).strip()[:300]
        action = ActionType.clarify if reply.action == "clarify" else ActionType.answer
        return self._result(request, ctx, action, reply.text, turn=turn)

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
        summary = LeadSummary(
            task=ctx.topic or "Клиент попросил связаться со специалистом",
            known_facts=[f"{key}: {value}" for key, value in ctx.facts.items()],
            assumptions=[f"{key}: {value}" for key, value in ctx.assumptions.items()],
            unknowns=[label for name, label in (("full_name", "ФИО"), ("company", "Компания"),
                                               ("region", "Регион")) if not getattr(ctx.contact, name)],
        )
        lead = LeadDraft(contact=ctx.contact, callback_requested=True, summary=summary)
        return self._result(request, ctx, action, answer, lead, turn)

    @staticmethod
    def _result(request: ConsultationRequest, ctx: ClientContext, action: ActionType, answer: str,
                lead: LeadDraft | None = None, turn: RagTurn | None = None) -> ConsultationResult:
        return ConsultationResult(
            session_id=request.session_id, request_id=request.request_id,
            answer=answer, action=action,
            clarifying_question=answer if action == ActionType.clarify else None,
            client_context=ctx, lead_draft=lead,
            used_evidence_ids=list(turn.reply.evidence_ids) if turn and not lead else [],
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
