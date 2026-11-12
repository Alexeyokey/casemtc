"""Генерация JSON Schema и синтетических fixtures из DTO (S1).

    python scripts/gen_contracts.py          # перезаписать contracts/
    python scripts/gen_contracts.py --check  # CI: упасть, если contracts/ устарели

Все контакты и тексты вымышленные. Ни одного фрагмента закрытой БЗ.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.api_models import (  # noqa: E402
    ContactsUpdate, ErrorResponse, LeadSaveRequest, LeadView, SessionCreated, SessionCreateRequest,
    SessionView, TraceView, TurnRequest, TurnResponse,
)
from core.contracts import (  # noqa: E402
    ActionType, ClientContext, ConsultationRequest, ConsultationResult, ContactInfo, ContactStatus,
    Evidence, LeadDraft, LeadSummary, Message, Role, StageTiming,
)

SCHEMAS = {
    "ConsultationRequest": ConsultationRequest, "ConsultationResult": ConsultationResult,
    "SessionCreateRequest": SessionCreateRequest, "SessionCreated": SessionCreated, "SessionView": SessionView,
    "TurnRequest": TurnRequest, "TurnResponse": TurnResponse, "ContactsUpdate": ContactsUpdate,
    "LeadSaveRequest": LeadSaveRequest, "LeadView": LeadView, "TraceView": TraceView,
    "ErrorResponse": ErrorResponse,
}

SID, RID1, RID2 = "s_00000000000000000000000000000001", "r-0001", "r-0002"
EV = [
    Evidence(evidence_id="syn-001#0", doc_id="synthetic/product_a.md",
             doc_title="Вымышленный продукт А: учёт энергопотребления", section="Назначение",
             text="Продукт А (синтетический пример) собирает показания счётчиков и строит отчёты.",
             score=0.82, retriever="hybrid"),
    Evidence(evidence_id="syn-faq#3", doc_id="synthetic/faq.md", doc_title="Вымышленный FAQ",
             section="Цены", text="Стоимость в синтетической БЗ не указана; её называет менеджер.",
             score=0.41, retriever="hybrid"),
]
TIMINGS = [StageTiming(name="retrieval", ms=120.0), StageTiming(name="generation", ms=2300.0)]
SUMMARY = LeadSummary(
    task="Учёт энергопотребления на складах",
    known_facts=["масштаб: 3 объекта"],
    assumptions=[],
    discussed_solutions=["Вымышленный продукт А: учёт энергопотребления"],
    unknowns=["ФИО контактного лица", "Компания"],
    next_step="Связаться с клиентом по телефону и уточнить неизвестные сведения",
)
PHONE_ONLY = ContactInfo(phone="+79990000001")  # вымышленный номер


def fixtures() -> dict[str, object]:
    ctx1 = ClientContext(topic="Учёт энергопотребления на складах", facts={"масштаб": "3 объекта"})
    hist = [Message(role=Role.user, content="Нужно считать электроэнергию на 3 объектах складов"),
            Message(role=Role.assistant, content="Для учёта подойдёт продукт А. Расскажу подробнее?")]
    ctx_phone = ctx1.model_copy(update={"contact": PHONE_ONLY, "contact_status": ContactStatus.phone_valid,
                                        "callback_requested": True})
    ctx_refused = ctx1.model_copy(update={"contact_status": ContactStatus.refused})
    lead_saved = LeadView(lead_id="lead_0000000000000001", session_id=SID, status="saved", contact=PHONE_ONLY,
                          callback_requested=True, missing_fields=["ФИО", "Компания", "Регион"], summary=SUMMARY,
                          revision=1, saved_at="2026-11-11T10:00:05.000+00:00",
                          status_text="Заявка сохранена в системе. Отправка в CRM или на почту не подключена.")
    return {
        # ---------- ядро
        "core/request_first_turn": ConsultationRequest(
            session_id=SID, request_id=RID1, message="Нужно считать электроэнергию на 3 объектах складов"),
        "core/result_answer_plain": ConsultationResult(
            session_id=SID, request_id=RID1, action=ActionType.answer,
            answer="Для учёта энергопотребления подойдёт продукт А: он собирает показания и строит отчёты. "
                   "Стоимость назовёт менеджер.",
            used_evidence_ids=["syn-001#0"], client_context=ctx1, timings=TIMINGS),
        "core/result_answer_debug": ConsultationResult(
            session_id=SID, request_id=RID1, action=ActionType.answer,
            answer="Для учёта энергопотребления подойдёт продукт А.", used_evidence_ids=["syn-001#0"],
            retrieved=EV, client_context=ctx1, timings=TIMINGS),
        "core/result_clarify": ConsultationResult(
            session_id=SID, request_id=RID1, action=ActionType.clarify,
            answer="Уточните, пожалуйста: сколько объектов нужно подключить?",
            clarifying_question="Сколько объектов нужно подключить?", client_context=ClientContext()),
        "core/request_callback_phone_only": ConsultationRequest(
            session_id=SID, request_id=RID2, history=hist, client_context=ctx1,
            message="Свяжите меня со специалистом, телефон +7 999 000-00-01"),
        "core/result_lead_phone_only": ConsultationResult(
            session_id=SID, request_id=RID2, action=ActionType.lead_ready,
            answer="Спасибо, телефон получен. Подтвердите сохранение заявки.", client_context=ctx_phone,
            lead_draft=LeadDraft(contact=PHONE_ONLY, callback_requested=True, summary=SUMMARY)),
        "core/result_contacts_refused": ConsultationResult(
            session_id=SID, request_id=RID2, action=ActionType.answer,
            answer="Хорошо, контакты не обязательны — продолжим консультацию.", client_context=ctx_refused),
        # ---------- HTTP
        "http/turn_request": TurnRequest(request_id=RID1, text="Нужно считать электроэнергию на 3 объектах"),
        "http/turn_response": TurnResponse(
            session_id=SID, request_id=RID1, user_message_id=1, assistant_message_id=2,
            answer="Для учёта энергопотребления подойдёт продукт А.", action=ActionType.answer,
            debug_available=True),
        "http/contacts_update_wrong_then_fix": ContactsUpdate(phone="+7 999 000-00-01", full_name="Иван Тестов"),
        "http/lead_view_draft_no_phone": LeadView(
            lead_id="lead_0000000000000001", session_id=SID, status="draft", contact=ContactInfo(),
            callback_requested=True, missing_fields=["Телефон", "ФИО", "Компания", "Регион"], summary=SUMMARY,
            status_text="Заявка не сохранена: для связи нужен телефон. Консультацию можно продолжать, "
                        "история диалога сохраняется."),
        "http/lead_view_saved_phone_only": lead_saved,
        "http/lead_view_ready_phone_only": LeadView(
            lead_id="lead_0000000000000001", session_id=SID, status="ready", contact=PHONE_ONLY,
            callback_requested=True, missing_fields=["ФИО", "Компания", "Регион"], summary=SUMMARY,
            offer_id="a" * 64, status_text="Проверьте контакты и резюме перед сохранением заявки."),
        "http/lead_view_refused": LeadView(
            session_id=SID, status="none", contact=ContactInfo(), contact_refused=True,
            missing_fields=["Телефон", "ФИО", "Компания", "Регион"],
            status_text="Вы отказались оставлять контакты. Консультацию можно продолжать."),
        "http/trace_view": TraceView(
            trace_id="t_00000000000000000001", session_id=SID, request_id=RID1, attempt=1, status="completed",
            engine="core.stub_engine:StubEngine", created_at="2026-11-11T10:00:03.000+00:00",
            timings=[StageTiming(name="engine.retrieval", ms=120.0), StageTiming(name="engine.generation", ms=2300.0),
                     StageTiming(name="total", ms=2460.0)],
            retrieved=EV, used=[EV[0]], action="answer"),
        "http/error_turn_in_progress": ErrorResponse.model_validate({"error": {
            "code": "turn_in_progress", "message": "Дождитесь ответа на предыдущее сообщение",
            "request_id": RID2, "retryable": True}}),
    }


def render() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for name, model in SCHEMAS.items():
        out[ROOT / "contracts" / "schemas" / f"{name}.schema.json"] = json.dumps(
            model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
    for name, obj in fixtures().items():
        out[ROOT / "contracts" / "fixtures" / f"{name}.json"] = obj.model_dump_json(indent=2) + "\n"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    files = render()
    stale = [p for p, c in files.items() if not p.exists() or p.read_text(encoding="utf-8") != c]
    if a.check:
        for p in stale:
            print("stale:", p.relative_to(ROOT))
        return 1 if stale else 0
    for p, c in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c, encoding="utf-8")
    print(f"written {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
