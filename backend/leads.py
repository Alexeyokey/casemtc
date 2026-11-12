"""Сбор и сохранение заявки менеджеру (S3, issue #12).

Правила (QA05/QA06):
* сохранить заявку можно только с валидным телефоном; ФИО/компания/регион
  предлагаем, но не требуем и никогда не подставляем вымышленные;
* без телефона — черновик + полная история, консультация продолжается;
* «сохранена» означает только запись в БД и файл заявки, без CRM/почты;
* повтор сохранения не создаёт дубль (одна заявка на сессию + idempotency_key).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from core.contracts import ClientContext, ContactInfo, ContactStatus, LeadDraft, LeadSummary
from core.phone import normalize_phone

from .api_models import ContactsUpdate, LeadView
from .db import Database, dumps, loads, now_iso
from .errors import ApiError

OPTIONAL_FIELDS = {"full_name": "ФИО", "company": "Компания", "region": "Регион"}


class LeadService:
    def __init__(self, db: Database, leads_dir: str | Path):
        self.db = db
        self.leads_dir = Path(leads_dir)

    # ------------------------------------------------------------ helpers
    def _ctx(self, session_id: str) -> ClientContext:
        row = self.db.one("SELECT client_context_json FROM sessions WHERE id=?", (session_id,))
        return ClientContext.model_validate(loads(row["client_context_json"]))

    def _save_ctx(self, conn, session_id: str, ctx: ClientContext) -> None:
        conn.execute(
            "UPDATE sessions SET client_context_json=?, updated_at=? WHERE id=?",
            (ctx.model_dump_json(), now_iso(), session_id),
        )

    def _row(self, session_id: str):
        return self.db.one("SELECT * FROM leads WHERE session_id=?", (session_id,))

    @staticmethod
    def _sanitize_contact(c: ContactInfo) -> ContactInfo:
        """Пустые строки → None; телефон нормализуется, невалидный отбрасывается."""
        def clean(v: Optional[str]) -> Optional[str]:
            v = (v or "").strip()
            return v or None
        return ContactInfo(
            full_name=clean(c.full_name),
            phone=normalize_phone(c.phone) if c.phone else None,
            company=clean(c.company),
            region=clean(c.region),
        )

    def _ensure_row(self, conn, session_id: str) -> None:
        exists = conn.execute("SELECT 1 FROM leads WHERE session_id=?", (session_id,)).fetchone()
        if not exists:
            ts = now_iso()
            conn.execute(
                "INSERT INTO leads(id, session_id, status, contact_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                ("lead_" + uuid.uuid4().hex[:16], session_id, "draft", ContactInfo().model_dump_json(), ts, ts),
            )

    # ------------------------------------------------------------ engine sync
    def sync_from_engine(self, conn, session_id: str, ctx: ClientContext, draft: Optional[LeadDraft]) -> ClientContext:
        """Вызывается внутри транзакции хода. Нормализует контакты из ответа
        engine и обновляет черновик. Возвращает очищенный контекст."""
        ctx = ctx.model_copy(deep=True)
        source = draft.contact if draft else ctx.contact
        merged = self._sanitize_contact(
            ContactInfo(
                full_name=source.full_name or ctx.contact.full_name,
                phone=source.phone or ctx.contact.phone,
                company=source.company or ctx.contact.company,
                region=source.region or ctx.contact.region,
            )
        )
        ctx.contact = merged
        if draft and draft.callback_requested:
            ctx.callback_requested = True
        if ctx.contact_status != ContactStatus.refused:
            if merged.phone:
                ctx.contact_status = ContactStatus.phone_valid
            elif any([merged.full_name, merged.company, merged.region]):
                ctx.contact_status = ContactStatus.partial

        need_row = bool(draft) or ctx.callback_requested or any(merged.model_dump().values())
        if need_row:
            self._ensure_row(conn, session_id)
            summary = draft.summary if draft and draft.summary else None
            row = conn.execute("SELECT summary_json FROM leads WHERE session_id=?", (session_id,)).fetchone()
            summary_json = summary.model_dump_json() if summary else row["summary_json"]
            conn.execute(
                "UPDATE leads SET contact_json=?, callback_requested=?, summary_json=?, updated_at=? "
                "WHERE session_id=?",
                (merged.model_dump_json(), int(ctx.callback_requested), summary_json, now_iso(), session_id),
            )
        return ctx

    # ------------------------------------------------------------ API ops
    def view(self, session_id: str) -> LeadView:
        ctx = self._ctx(session_id)
        row = self._row(session_id)
        refused = ctx.contact_status == ContactStatus.refused
        contact = ctx.contact
        summary = None
        if row:
            summary = LeadSummary.model_validate(loads(row["summary_json"])) if row["summary_json"] else None
            if row["status"] == "saved":
                contact = ContactInfo.model_validate(loads(row["contact_json"]))
        if summary is None and row:
            summary = self.fallback_summary(session_id, ctx)
        missing = [label for f, label in OPTIONAL_FIELDS.items() if not getattr(contact, f)]
        if not contact.phone:
            missing.insert(0, "Телефон")

        if row and row["status"] == "saved":
            status = "saved"
            text = ("Заявка сохранена в системе. Менеджер увидит её в списке заявок. "
                    "Отправка в CRM или на почту не подключена.")
        elif contact.phone:
            status = "ready"
            text = "Данных достаточно: проверьте контакты и подтвердите сохранение заявки."
        elif row or ctx.callback_requested:
            status = "draft"
            text = ("Заявка не сохранена: для связи нужен телефон. Консультацию можно продолжать, "
                    "история диалога сохраняется.") if not refused else (
                    "Вы отказались оставлять контакты — заявка не создана, менеджер не сможет связаться. "
                    "Консультацию можно продолжать, история сохраняется.")
        else:
            status = "none"
            text = ("Вы отказались оставлять контакты. Консультацию можно продолжать."
                    if refused else "Заявка ещё не начата.")
        return LeadView(
            lead_id=row["id"] if row else None,
            session_id=session_id,
            status=status,
            contact=contact,
            contact_refused=refused,
            callback_requested=ctx.callback_requested,
            missing_fields=missing,
            summary=summary,
            revision=row["revision"] if row else 0,
            saved_at=row["saved_at"] if row else None,
            status_text=text,
        )

    def update_contacts(self, session_id: str, upd: ContactsUpdate) -> LeadView:
        fields = upd.model_dump(exclude_unset=True)
        if "phone" in fields and fields["phone"]:
            normalized = normalize_phone(fields["phone"])
            if not normalized:
                raise ApiError(422, "invalid_phone",
                               "Номер телефона не распознан. Пример: +7 916 123-45-67.")
            fields["phone"] = normalized
        with self.db.tx() as conn:
            ctx = self._ctx(session_id)
            data = ctx.contact.model_dump()
            for k in ("full_name", "phone", "company", "region"):
                if k in fields:
                    data[k] = fields[k]
            ctx.contact = self._sanitize_contact(ContactInfo(**data))
            if fields.get("callback_requested") is not None:
                ctx.callback_requested = bool(fields["callback_requested"])
            if ctx.contact.phone:
                ctx.contact_status = ContactStatus.phone_valid
            elif any([ctx.contact.full_name, ctx.contact.company, ctx.contact.region]):
                ctx.contact_status = ContactStatus.partial
            elif ctx.contact_status != ContactStatus.refused:
                ctx.contact_status = ContactStatus.requested
            self._save_ctx(conn, session_id, ctx)
            self._ensure_row(conn, session_id)
            row = conn.execute("SELECT status FROM leads WHERE session_id=?", (session_id,)).fetchone()
            if row["status"] != "saved":
                conn.execute(
                    "UPDATE leads SET contact_json=?, callback_requested=?, updated_at=? WHERE session_id=?",
                    (ctx.contact.model_dump_json(), int(ctx.callback_requested), now_iso(), session_id),
                )
        return self.view(session_id)

    def refuse(self, session_id: str) -> LeadView:
        with self.db.tx() as conn:
            ctx = self._ctx(session_id)
            ctx.contact_status = ContactStatus.refused
            ctx.callback_requested = False
            self._save_ctx(conn, session_id, ctx)
        return self.view(session_id)

    def save(self, session_id: str, idempotency_key: str) -> LeadView:
        ctx = self._ctx(session_id)
        row = self._row(session_id)
        if row and row["idempotency_key"] == idempotency_key and row["status"] == "saved":
            return self.view(session_id)  # повтор того же запроса
        if not ctx.contact.phone:
            raise ApiError(422, "phone_required",
                           "Чтобы сохранить заявку, укажите телефон для связи. "
                           "Без телефона консультация продолжится, а история сохранится.")
        summary = None
        if row and row["summary_json"]:
            summary = LeadSummary.model_validate(loads(row["summary_json"]))
        summary = summary or self.fallback_summary(session_id, ctx)
        contact_json = ctx.contact.model_dump_json()
        with self.db.tx() as conn:
            self._ensure_row(conn, session_id)
            cur = conn.execute("SELECT * FROM leads WHERE session_id=?", (session_id,)).fetchone()
            unchanged = (cur["status"] == "saved" and cur["contact_json"] == contact_json
                         and cur["summary_json"] == summary.model_dump_json())
            if unchanged:
                conn.execute("UPDATE leads SET idempotency_key=? WHERE session_id=?", (idempotency_key, session_id))
            else:
                ts = now_iso()
                conn.execute(
                    "UPDATE leads SET status='saved', contact_json=?, summary_json=?, callback_requested=?, "
                    "revision=revision+1, idempotency_key=?, updated_at=?, saved_at=? WHERE session_id=?",
                    (contact_json, summary.model_dump_json(), int(ctx.callback_requested),
                     idempotency_key, ts, ts, session_id),
                )
        view = self.view(session_id)
        self._write_file(view)
        return view

    # ------------------------------------------------------------ summary/render
    def fallback_summary(self, session_id: str, ctx: ClientContext) -> LeadSummary:
        """Резюме только из сказанного клиентом и сохранённого контекста.
        Используется, если engine не прислал своё резюме. Ничего не выдумывает."""
        last_user = self.db.one(
            "SELECT content FROM messages WHERE session_id=? AND role='user' AND in_context=1 "
            "ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        task = ctx.topic or (last_user["content"] if last_user else "Клиент запросил обратную связь")
        unknowns = [label for f, label in OPTIONAL_FIELDS.items() if not getattr(ctx.contact, f)]
        return LeadSummary(
            task=task[:300],
            known_facts=[f"{k}: {v}" for k, v in ctx.facts.items()],
            assumptions=[f"{k}: {v}" for k, v in ctx.assumptions.items()],
            discussed_solutions=[],
            unknowns=unknowns,
            next_step="Связаться с клиентом и уточнить задачу" if ctx.contact.phone
            else "Контакт для связи не получен",
        )

    @staticmethod
    def render_markdown(view: LeadView) -> str:
        c, s = view.contact, view.summary
        na = "не указано"
        lines = [
            f"# Заявка {view.lead_id}",
            "",
            f"- Статус: {'сохранена' if view.status == 'saved' else view.status} (ревизия {view.revision})",
            f"- Сохранена: {view.saved_at or '—'}",
            f"- Сессия: {view.session_id}",
            "- Доставка: только сохранение в системе; CRM, почта и уведомления не подключены",
            "",
            "## Контакты",
            f"- Телефон: {c.phone or na}",
            f"- ФИО: {c.full_name or na}",
            f"- Компания: {c.company or na}",
            f"- Регион: {c.region or na}",
            f"- Клиент просил связаться: {'да' if view.callback_requested else 'нет'}",
            "",
            "## Резюме для менеджера",
        ]
        if s:
            def block(title: str, items: list[str]) -> list[str]:
                return [f"**{title}:**"] + ([f"- {i}" for i in items] if items else ["- —"]) + [""]
            lines += [f"**Задача:** {s.task}", ""]
            lines += block("Известно со слов клиента", s.known_facts)
            lines += block("Предположения (требуют проверки)", s.assumptions)
            lines += block("Обсуждённые решения", s.discussed_solutions)
            lines += block("Неизвестно", s.unknowns)
            lines += [f"**Следующий шаг:** {s.next_step}", ""]
        else:
            lines += ["—", ""]
        lines.append("Полная история диалога хранится отдельно в архиве сессий.")
        return "\n".join(lines) + "\n"

    def _write_file(self, view: LeadView) -> Path:
        self.leads_dir.mkdir(parents=True, exist_ok=True)
        path = self.leads_dir / f"{view.lead_id}.md"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(self.render_markdown(view), encoding="utf-8")
        tmp.replace(path)
        return path

    def list_saved(self) -> list[LeadView]:
        rows = self.db.all("SELECT session_id FROM leads WHERE status='saved' ORDER BY saved_at DESC")
        return [self.view(r["session_id"]) for r in rows]

    def by_id(self, lead_id: str) -> LeadView:
        row = self.db.one("SELECT session_id FROM leads WHERE id=?", (lead_id,))
        if not row:
            raise ApiError(404, "lead_not_found", "Заявка не найдена")
        return self.view(row["session_id"])
