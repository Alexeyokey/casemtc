"""Adapt the shared async consultant to the synchronous chat service."""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import httpx

from consultation import Turn
from consultation_context import TurnContext, analyze_history, refuses_contact, requests_callback, task_text
from core.contracts import (
    ActionType, ClientContext, ContactInfo, ConsultationRequest, ConsultationResult,
    ContactStatus, Evidence, LeadDraft, LeadSummary, StageTiming,
)
from core.phone import extract_phone
from knowledge_base import KnowledgeBase
from organizer_api import APIConfig, OrganizerClient
from rag_consultation import CONTACT, CONTINUE, OUT_OF_SCOPE, RagConsultant, RagTurn, configured_top_k

from .redaction import redact_text


_ACTIONS = {
    "answer": ActionType.answer,
    "clarify": ActionType.clarify,
    "escalate": ActionType.escalate,
    "out_of_scope": ActionType.out_of_scope,
}


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
        history = tuple(Turn(item.role.value, redact_text(item.content)) for item in request.history)
        history += (Turn("user", redact_text(message)),)
        context = analyze_history(
            history, initial_task=(ctx.topic or "") if not request.history else "",
            initial_facts=ctx.facts if not request.history else None,
        )
        if context.outside:
            return self._result(request, ctx, ActionType.out_of_scope, OUT_OF_SCOPE)

        if context.task:
            ctx.topic = context.task
        elif request.history:
            ctx.topic = None
        ctx.facts = context.facts
        refusal = refuses_contact(message)
        if refusal:
            ctx.contact_status = ContactStatus.refused
            ctx.callback_requested = False
            ctx.contact = ContactInfo()
            if len(task_text(message)) <= 12:
                return self._result(request, ctx, ActionType.answer, CONTINUE)

        callback = requests_callback(message)
        if callback:
            ctx.callback_requested = True
        phone = extract_phone(message) if ctx.callback_requested else None
        if phone:
            ctx.contact.phone = phone

        if callback or phone:
            return self._contact_result(request, ctx)

        with self.slots:
            turn = asyncio.run(self._consult(history, context))
        reply = turn.reply
        lead = self._draft(ctx) if ctx.callback_requested or ctx.contact.phone else None
        return self._result(request, ctx, _ACTIONS[reply.action], reply.text, lead, turn)

    async def _consult(self, history: tuple[Turn, ...], context: TurnContext) -> RagTurn:
        async with OrganizerClient(self.config, transport=self.transport) as client:
            return await RagConsultant(self.knowledge, client.chat, top_k=self.top_k).consult_turn(
                history, context=context,
            )

    def _contact_result(self, request: ConsultationRequest, ctx: ClientContext,
                        turn: RagTurn | None = None) -> ConsultationResult:
        ctx.contact_status = ContactStatus.phone_valid if ctx.contact.phone else ContactStatus.requested
        action = ActionType.lead_ready if ctx.contact.phone else ActionType.collect_contacts
        answer = ("Проверьте контакты и резюме, затем подтвердите сохранение заявки." if ctx.contact.phone
                  else CONTACT)
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
