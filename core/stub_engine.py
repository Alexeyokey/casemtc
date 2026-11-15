"""Детерминированный синтетический engine для параллельной разработки.

НЕ является консультантом: не обращается к LLM и закрытой БЗ, отвечает
по трём вымышленным фрагментам. Нужен, чтобы backend, UI и CSV-адаптер
работали по контракту до готовности C1–C4/R3. Подменяется через env
CONSULTATION_ENGINE=<module>:<Factory>.

Служебные маркеры для тестов отказоустойчивости: '__fail__' — исключение
этапа генерации, '__slow__' — задержка 1.5 с.
"""
from __future__ import annotations

import re
import time

from .contracts import (
    ActionType,
    ClientContext,
    ConsultationRequest,
    ConsultationResult,
    ContactStatus,
    Evidence,
    LeadDraft,
    LeadSummary,
    StageTiming,
)
from .phone import extract_phone

SYNTHETIC_KB = [
    Evidence(
        evidence_id="syn-001#0",
        doc_id="synthetic/product_a.md",
        doc_title="Вымышленный продукт А: учёт энергопотребления",
        section="Назначение",
        text="Продукт А (синтетический пример) собирает показания счётчиков и строит отчёты.",
    ),
    Evidence(
        evidence_id="syn-002#0",
        doc_id="synthetic/product_b.md",
        doc_title="Вымышленный продукт Б: телеметрия оборудования",
        section="Применимость",
        text="Продукт Б (синтетический пример) подходит для удалённого мониторинга оборудования.",
    ),
    Evidence(
        evidence_id="syn-faq#3",
        doc_id="synthetic/faq.md",
        doc_title="Вымышленный FAQ",
        section="Цены",
        text="Стоимость в синтетической БЗ не указана; её называет менеджер.",
    ),
]

_CALLBACK = re.compile(r"(свяж|связать|перезвон|позвон|менеджер|специалист|оставить заявку)", re.I)
_REFUSE = re.compile(
    r"(не (буду|хочу) (оставлять|давать|называть)|без (контактов|телефона)|не дам (телефон|номер))", re.I
)
_NAME = re.compile(r"меня зовут\s+([А-ЯЁA-Z][\w\-]+(?:\s+[А-ЯЁA-Z][\w\-]+){0,2})")
_COMPANY = re.compile(r"(?:компания|организация|ООО|АО|ИП)\s+[«\"]?([^»\".,\n]{2,60})", re.I)
_OBJECTS = re.compile(r"(\d+)\s+(объект\w*|здани\w*|филиал\w*|счётчик\w*|счетчик\w*)", re.I)


class StubEngine:
    def consult(self, request: ConsultationRequest) -> ConsultationResult:
        t0 = time.perf_counter()
        msg = request.message
        ctx = request.client_context.model_copy(deep=True)

        if "__slow__" in msg:
            time.sleep(1.5)

        # --- обновление контекста только из слов клиента ---
        if ctx.topic is None and len(msg) > 15:
            ctx.topic = msg[:200]
        if m := _OBJECTS.search(msg):
            ctx.facts["масштаб"] = f"{m.group(1)} {m.group(2)}"
        if m := _NAME.search(msg):
            ctx.contact.full_name = m.group(1)
        if m := _COMPANY.search(msg):
            ctx.contact.company = m.group(1).strip()
        phone = extract_phone(msg)
        if phone:
            ctx.contact.phone = phone
        if _CALLBACK.search(msg):
            ctx.callback_requested = True
        refused_now = bool(_REFUSE.search(msg))
        if refused_now:
            ctx.contact_status = ContactStatus.refused
            ctx.callback_requested = False
        elif ctx.contact.phone:
            ctx.contact_status = ContactStatus.phone_valid
        elif ctx.contact.full_name or ctx.contact.company:
            ctx.contact_status = ContactStatus.partial

        # --- поиск (синтетический) ---
        t_r = time.perf_counter()
        retrieved = [e for e in SYNTHETIC_KB if _overlap(msg, e.text)] or SYNTHETIC_KB[:2]
        retrieval_ms = (time.perf_counter() - t_r) * 1000

        # --- генерация ---
        t_g = time.perf_counter()
        if "__fail__" in msg:
            raise RuntimeError("synthetic generation failure")
        clarifying = None
        used: list[str] = []
        if refused_now:
            # Подтверждаем отказ только в той реплике, где он прозвучал;
            # дальше консультация идёт обычным путём (регрессия из ручной приёмки S5).
            action = ActionType.answer
            answer = ("Хорошо, контакты не обязательны — продолжим консультацию. "
                      "Расскажите, какую задачу хотите решить.")
        elif ctx.callback_requested and ctx.contact.phone:
            action = ActionType.lead_ready
            answer = ("Спасибо, телефон получен. Проверьте данные заявки и подтвердите сохранение — "
                      "после сохранения менеджер увидит её в списке заявок.")
        elif ctx.callback_requested:
            action = ActionType.collect_contacts
            answer = ("Чтобы специалист мог связаться с вами, оставьте, пожалуйста, телефон. "
                      "По желанию — ФИО и компанию.")
        elif len(msg.strip()) < 12 and not ctx.topic:
            action = ActionType.clarify
            clarifying = "Уточните, пожалуйста, какую задачу вы хотите решить и для какого объекта?"
            answer = clarifying
        else:
            action = ActionType.answer
            used = [retrieved[0].evidence_id]
            answer = ("По синтетической базе знаний: " + retrieved[0].text +
                      " Если хотите, расскажу подробнее или помогу оставить заявку.")
        generation_ms = (time.perf_counter() - t_g) * 1000

        lead = None
        if ctx.callback_requested or ctx.contact.phone:
            lead = LeadDraft(
                contact=ctx.contact,
                callback_requested=ctx.callback_requested,
                summary=_summary(request, ctx, used),
            )

        total_ms = (time.perf_counter() - t0) * 1000
        return ConsultationResult(
            session_id=request.session_id,
            request_id=request.request_id,
            answer=answer,
            action=action,
            clarifying_question=clarifying,
            used_evidence_ids=used,
            retrieved=retrieved if request.debug else [],
            client_context=ctx,
            lead_draft=lead,
            timings=[
                StageTiming(name="retrieval", ms=retrieval_ms),
                StageTiming(name="generation", ms=generation_ms),
                StageTiming(name="engine_total", ms=total_ms),
            ],
        )


def _overlap(a: str, b: str) -> bool:
    wa = {w for w in re.findall(r"\w{5,}", a.lower())}
    wb = {w for w in re.findall(r"\w{5,}", b.lower())}
    return bool(wa & wb)


def _summary(req: ConsultationRequest, ctx: ClientContext, used: list[str]) -> LeadSummary:
    task = ctx.topic or next(
        (m.content for m in reversed(req.history) if m.role.value == "user"), req.message
    )
    unknowns = []
    if not ctx.contact.full_name:
        unknowns.append("ФИО контактного лица")
    if not ctx.contact.company:
        unknowns.append("Компания")
    if "масштаб" not in ctx.facts:
        unknowns.append("Масштаб (количество объектов)")
    return LeadSummary(
        task=task[:300],
        known_facts=[f"{k}: {v}" for k, v in ctx.facts.items()],
        assumptions=[f"{k}: {v}" for k, v in ctx.assumptions.items()],
        discussed_solutions=[e.doc_title or e.doc_id for e in SYNTHETIC_KB if e.evidence_id in used],
        unknowns=unknowns,
        next_step="Связаться с клиентом по телефону и уточнить неизвестные сведения",
    )
