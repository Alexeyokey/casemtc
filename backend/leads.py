"""Сбор и сохранение заявки менеджеру (S3, issue #12).

Правила (QA05/QA06):
* сохранить заявку можно только с валидным телефоном; ФИО/компания/регион
  предлагаем, но не требуем и никогда не подставляем вымышленные;
* без телефона — черновик + полная история, консультация продолжается;
* сохранённая ревизия меняется только по явному подтверждению её черновика;
* БД хранит заявку и ключ повтора, из сохранённой ревизии строится Markdown.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

from core.contracts import ClientContext, ContactInfo, ContactStatus, LeadDraft, LeadSummary
from core.phone import normalize_phone

from .api_models import ContactsUpdate, LeadView
from .db import Database, dumps, loads, now_iso
from .errors import ApiError

OPTIONAL_FIELDS = {"full_name": "ФИО", "company": "Компания", "region": "Регион"}
_CONTACT_SUMMARY_LABELS = {label.casefold() for label in OPTIONAL_FIELDS.values()} | {"фио контактного лица"}


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

    def _update_draft(self, conn, session_id: str, ctx: ClientContext, summary: Optional[LeadSummary], *,
                      context_changed: bool = False, contacts_changed: bool = False) -> None:
        self._ensure_row(conn, session_id)
        row = self._row(session_id)
        summary_json = summary.model_dump_json() if summary else row["draft_summary_json"]
        if ctx.contact_status == ContactStatus.refused or (context_changed and summary is None):
            summary_json = None
        elif contacts_changed and summary_json:
            draft = LeadSummary.model_validate_json(summary_json)
            unknowns = [label for label in draft.unknowns
                        if label.strip().casefold() not in _CONTACT_SUMMARY_LABELS]
            unknowns.extend(label for field, label in OPTIONAL_FIELDS.items() if not getattr(ctx.contact, field))
            summary_json = draft.model_copy(update={"unknowns": unknowns}).model_dump_json()
        conn.execute("UPDATE leads SET draft_summary_json=?, updated_at=? WHERE session_id=?",
                     (summary_json, now_iso(), session_id))
        if row["status"] == "draft":
            conn.execute("UPDATE leads SET contact_json=?, callback_requested=?, summary_json=? WHERE session_id=?",
                         (ctx.contact.model_dump_json(), int(ctx.callback_requested), summary_json, session_id))

    @staticmethod
    def _contact_state(ctx: ClientContext) -> tuple[ContactInfo, ContactStatus, bool]:
        return ctx.contact, ctx.contact_status, ctx.callback_requested

    def sync_from_engine(
        self, conn, session_id: str, ctx: ClientContext, draft: Optional[LeadDraft], *,
        started_ctx: Optional[ClientContext] = None,
    ) -> ClientContext:
        """Merge the completed turn without overriding a newer contact form or refusal."""
        current = self._ctx(session_id)
        ctx = ctx.model_copy(deep=True)
        if started_ctx and self._contact_state(started_ctx) != self._contact_state(current):
            ctx.contact, ctx.contact_status, ctx.callback_requested = self._contact_state(current)
            summary = None
        else:
            source = draft.contact if draft else ctx.contact
            ctx.contact = self._sanitize_contact(ContactInfo(
                full_name=source.full_name or ctx.contact.full_name,
                phone=source.phone or ctx.contact.phone,
                company=source.company or ctx.contact.company,
                region=source.region or ctx.contact.region,
            ))
            if draft and draft.callback_requested:
                ctx.callback_requested = True
            if ctx.contact_status == ContactStatus.refused:
                ctx.contact = ContactInfo()
                ctx.callback_requested = False
            elif ctx.contact.phone:
                ctx.contact_status = ContactStatus.phone_valid
            elif any(ctx.contact.model_dump().values()):
                ctx.contact_status = ContactStatus.partial
            summary = draft.summary if draft else None

        if draft or ctx.callback_requested or any(ctx.contact.model_dump().values()) or self._row(session_id):
            context_changed = (ctx.topic != current.topic or ctx.facts != current.facts
                               or ctx.assumptions != current.assumptions)
            self._update_draft(conn, session_id, ctx, summary, context_changed=context_changed)
        return ctx

    def _load_draft(self, session_id: str):
        row = self.db.one(
            "SELECT l.*, s.client_context_json, "
            "(SELECT content FROM messages WHERE session_id=s.id AND role='user' AND in_context=1 "
            "ORDER BY seq DESC LIMIT 1) AS last_user FROM sessions s "
            "LEFT JOIN leads l ON l.session_id=s.id WHERE s.id=?",
            (session_id,),
        )
        ctx = ClientContext.model_validate(loads(row["client_context_json"]))
        return row, ctx

    @staticmethod
    def _missing_fields(contact: ContactInfo) -> list[str]:
        missing = [label for field, label in OPTIONAL_FIELDS.items() if not getattr(contact, field)]
        return missing if contact.phone else ["Телефон", *missing]

    def _draft_view(self, session_id: str, row, ctx: ClientContext) -> LeadView:
        refused = ctx.contact_status == ContactStatus.refused
        has_draft = bool(row["id"] or ctx.callback_requested or any(ctx.contact.model_dump().values()))
        summary = None
        if row["draft_summary_json"]:
            summary = LeadSummary.model_validate_json(row["draft_summary_json"])
        elif has_draft and not refused:
            summary = self.fallback_summary(ctx, row["last_user"])

        offer_id = None
        if refused:
            status = "draft" if has_draft else "none"
            text = "Вы отказались оставлять контакты. Консультацию можно продолжать."
        elif ctx.contact.phone:
            status = "ready"
            text = "Проверьте контакты и резюме перед сохранением заявки."
            snapshot = dumps([session_id, ctx.contact.model_dump(), summary.model_dump(), ctx.callback_requested])
            offer_id = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        elif has_draft:
            status = "draft"
            text = ("Заявка не сохранена: для связи нужен телефон. Консультацию можно продолжать, "
                    "история диалога сохраняется.")
        else:
            status = "none"
            text = "Заявка ещё не начата."
        return LeadView(lead_id=row["id"], session_id=session_id, status=status, contact=ctx.contact,
                        contact_refused=refused, callback_requested=ctx.callback_requested,
                        missing_fields=self._missing_fields(ctx.contact), summary=summary, offer_id=offer_id,
                        revision=row["revision"] or 0, saved_at=row["saved_at"], status_text=text)

    def _saved_view(self, row) -> LeadView:
        contact = ContactInfo.model_validate_json(row["contact_json"])
        summary = LeadSummary.model_validate_json(row["summary_json"])
        return LeadView(lead_id=row["id"], session_id=row["session_id"], status="saved", contact=contact,
                        callback_requested=bool(row["callback_requested"]), missing_fields=self._missing_fields(contact),
                        summary=summary, revision=row["revision"], saved_at=row["saved_at"],
                        status_text="Заявка сохранена в системе. Отправка в CRM или на почту не подключена.")

    @staticmethod
    def _matches_saved(row, draft: LeadView) -> bool:
        return (row["status"] == "saved" and row["contact_json"] == draft.contact.model_dump_json()
                and draft.summary is not None and row["summary_json"] == draft.summary.model_dump_json()
                and bool(row["callback_requested"]) == draft.callback_requested)

    # ------------------------------------------------------------ API ops
    def view(self, session_id: str) -> LeadView:
        row, ctx = self._load_draft(session_id)
        draft = self._draft_view(session_id, row, ctx)
        if row["status"] == "saved" and (draft.contact_refused or self._matches_saved(row, draft)):
            return self._saved_view(row).model_copy(
                update={"offer_id": draft.offer_id, "contact_refused": draft.contact_refused},
            )
        return draft

    def saved_view(self, session_id: str) -> LeadView:
        row = self._row(session_id)
        if not row or row["status"] != "saved":
            raise ApiError(404, "lead_not_found", "Сохранённая заявка не найдена")
        return self._saved_view(row)

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
            previous_contact = ctx.contact
            data = previous_contact.model_dump()
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
            self._update_draft(conn, session_id, ctx, None, contacts_changed=ctx.contact != previous_contact)
            return self.view(session_id)

    def refuse(self, session_id: str) -> LeadView:
        with self.db.tx() as conn:
            ctx = self._ctx(session_id)
            ctx.contact = ContactInfo()
            ctx.contact_status = ContactStatus.refused
            ctx.callback_requested = False
            self._save_ctx(conn, session_id, ctx)
            if self._row(session_id):
                self._update_draft(conn, session_id, ctx, None)
            return self.view(session_id)

    def save(self, session_id: str, idempotency_key: str, offer_id: str) -> LeadView:
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT offer_id, response_json FROM lead_save_requests WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["offer_id"] != offer_id:
                    raise ApiError(409, "lead_request_conflict", "Этот ключ уже использован для другой версии заявки.")
                response = LeadView.model_validate_json(existing["response_json"])
            else:
                response = self._confirm_draft(conn, session_id, idempotency_key, offer_id)

        # Always regenerate from the latest confirmed row. A retry can repair a
        # failed file write without replacing the lead or incrementing its revision.
        try:
            with self.db.tx():
                self._write_file(self.saved_view(session_id))
        except OSError as error:
            raise ApiError(503, "lead_file_unavailable",
                           "Заявка записана, но файл недоступен. Повторите сохранение.", retryable=True) from error
        return response

    def _confirm_draft(self, conn, session_id: str, idempotency_key: str, offer_id: str) -> LeadView:
        row, ctx = self._load_draft(session_id)
        draft = self._draft_view(session_id, row, ctx)
        if draft.contact_refused:
            raise ApiError(409, "contacts_refused", "Клиент отказался от контактов. Заявка не сохранена.")
        if not draft.contact.phone:
            raise ApiError(422, "phone_required",
                           "Чтобы сохранить заявку, укажите телефон для связи. "
                           "Без телефона консультация продолжится, а история сохранится.")
        if draft.offer_id != offer_id:
            raise ApiError(409, "stale_offer", "Контакты или резюме изменились. Проверьте актуальную заявку.")
        if not self._matches_saved(row, draft):
            self._ensure_row(conn, session_id)
            ts = now_iso()
            conn.execute(
                "UPDATE leads SET status='saved', contact_json=?, summary_json=?, "
                "callback_requested=?, revision=revision+1, idempotency_key=?, updated_at=?, saved_at=? "
                "WHERE session_id=?",
                (draft.contact.model_dump_json(), draft.summary.model_dump_json(), int(draft.callback_requested),
                 idempotency_key, ts, ts, session_id),
            )
        response = self.saved_view(session_id).model_copy(update={"offer_id": offer_id})
        conn.execute("INSERT INTO lead_save_requests VALUES (?,?,?,?)",
                     (session_id, idempotency_key, offer_id, response.model_dump_json()))
        return response

    # ------------------------------------------------------------ summary/render
    @staticmethod
    def fallback_summary(ctx: ClientContext, last_user: Optional[str]) -> LeadSummary:
        """Резюме только из сказанного клиентом и сохранённого контекста.
        Используется, если engine не прислал своё резюме. Ничего не выдумывает."""
        task = ctx.topic or last_user or "Клиент запросил обратную связь"
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
        temp_path = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
                temp_path = Path(temp.name)
                temp.write(self.render_markdown(view))
            temp_path.replace(path)
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
        return path

    def list_saved(self) -> list[LeadView]:
        rows = self.db.all("SELECT * FROM leads WHERE status='saved' ORDER BY saved_at DESC")
        return [self._saved_view(row) for row in rows]

    def by_id(self, lead_id: str) -> LeadView:
        row = self.db.one("SELECT * FROM leads WHERE id=? AND status='saved'", (lead_id,))
        if not row:
            raise ApiError(404, "lead_not_found", "Заявка не найдена")
        return self._saved_view(row)
