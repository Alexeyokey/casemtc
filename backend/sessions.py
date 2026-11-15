"""Сессии, ходы диалога, архив и трассы (S2 #8, S4 #20).

Инварианты:
* в engine уходит история ТОЛЬКО текущей сессии и только завершённых ходов;
* новая/закрытая сессия не получает ничего из архива;
* (session_id, request_id) уникален: повтор возвращает сохранённый ответ.
  Failed/cancelled ход можно повторить без дубля, пока не начат новый ход;
* в сессии одновременно выполняется не больше одного хода.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from typing import Callable, Optional

from core.contracts import (
    ClientContext,
    ConsultationRequest,
    ConsultationResult,
    Message,
    Role,
    StageTiming,
)
from core.engine import ConsultationEngine

from .api_models import MessageView, SessionCreated, SessionView, TraceView, TurnResponse
from .db import Database, dumps, loads, now_iso
from .errors import ApiError
from .leads import LeadService
from .redaction import redact, redact_text

log = logging.getLogger("casemtc")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ChatService:
    def __init__(self, db: Database, engine: ConsultationEngine, leads: LeadService,
                 engine_name: str = "unknown", collect_debug: bool = True):
        self.db = db
        self.engine = engine
        self.leads = leads
        self.engine_name = engine_name
        self.collect_debug = collect_debug
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ================================================================ sessions
    def recover_after_restart(self) -> int:
        """Ходы, оставшиеся 'pending' после падения процесса, помечаются failed
        (их можно повторить тем же request_id до нового хода). Архив не теряется."""
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE turns SET status='failed', error_code='interrupted', "
                "error_message='process restarted during turn', finished_at=? WHERE status='pending'",
                (now_iso(),),
            )
            return cur.rowcount

    def create_session(self, channel: str = "web") -> SessionCreated:
        sid = "s_" + uuid.uuid4().hex
        token = secrets.token_urlsafe(24)
        ts = now_iso()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO sessions(id, client_token_hash, status, channel, client_context_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (sid, _hash_token(token), "active", channel, ClientContext().model_dump_json(), ts, ts),
            )
        log.info("session created %s", sid)
        return SessionCreated(session_id=sid, client_token=token, created_at=ts)

    def authorize(self, session_id: str, token: Optional[str]):
        row = self.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        # Чужая сессия неотличима от несуществующей.
        if not row or not token or not hmac.compare_digest(row["client_token_hash"], _hash_token(token)):
            raise ApiError(404, "session_not_found", "Диалог не найден. Начните новый диалог.")
        return row

    def close_session(self, session_id: str, reason: str = "user_closed") -> None:
        closed_at = now_iso()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE sessions SET status='closed', closed_at=COALESCE(closed_at, ?), "
                "close_reason=COALESCE(close_reason, ?), updated_at=? WHERE id=?",
                (closed_at, reason, closed_at, session_id),
            )
            conn.execute(
                "UPDATE turns SET status='cancelled', error_code='session_closed', finished_at=? "
                "WHERE session_id=? AND status='pending'",
                (closed_at, session_id),
            )

    def messages(self, session_id: str) -> list[MessageView]:
        rows = self.db.all("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session_id,))
        return [MessageView(id=r["id"], seq=r["seq"], role=r["role"], content=r["content"],
                            request_id=r["request_id"], in_context=bool(r["in_context"]),
                            created_at=r["created_at"]) for r in rows]

    def session_view(self, session_id: str) -> SessionView:
        r = self.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        lead = self.leads.view(session_id)
        return SessionView(session_id=r["id"], status=r["status"], created_at=r["created_at"],
                           closed_at=r["closed_at"], messages=self.messages(session_id),
                           lead=lead if lead.status != "none" or lead.contact_refused else None)

    # ================================================================ turns
    def _session_lock(self, sid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.Lock())

    def _history(self, conn, session_id: str, exclude_request: str) -> list[Message]:
        # Group replies with their user messages even for data from older builds
        # which could complete a retry after a newer request.
        rows = conn.execute(
            "SELECT m.role, m.content FROM messages AS m "
            "JOIN turns AS t ON t.session_id=m.session_id AND t.request_id=m.request_id "
            "LEFT JOIN traces AS tr ON tr.session_id=t.session_id AND tr.request_id=t.request_id "
            "AND tr.attempt=t.attempts AND tr.status='completed' "
            "WHERE m.session_id=? AND m.in_context=1 AND m.request_id<>? AND t.status='completed' "
            "ORDER BY t.finished_at, tr.rowid, m.seq",
            (session_id, exclude_request),
        ).fetchall()
        return [Message(role=Role(r["role"]), content=r["content"]) for r in rows]

    def handle_turn(
        self, session_id: str, token: Optional[str], request_id: str, text: str,
        *, on_admitted: Optional[Callable[[], None]] = None,
    ) -> TurnResponse:
        t_start = time.perf_counter()
        lock = None
        try:
            with self.db.tx() as conn:
                session = self.authorize(session_id, token)
                if session["status"] != "active":
                    raise ApiError(409, "session_closed", "Этот диалог завершён. Начните новый диалог.",
                                   request_id=request_id)
                existing = conn.execute(
                    "SELECT * FROM turns WHERE session_id=? AND request_id=?", (session_id, request_id)
                ).fetchone()
                if existing:
                    if existing["user_text"] != text:
                        raise ApiError(409, "request_id_conflict",
                                       "request_id уже использован для другой реплики", request_id=request_id)
                    if existing["status"] == "completed":
                        response = TurnResponse.model_validate(loads(existing["response_json"]))
                        response.replayed = True
                        response.lead = self.leads.view(session_id) if response.lead else None
                        if on_admitted is not None:
                            on_admitted()
                        return response

                candidate = self._session_lock(session_id)
                if not candidate.acquire(blocking=False):
                    raise self._turn_in_progress(request_id)
                lock = candidate
                attempt, user_msg_id = self._start_attempt(conn, session_id, request_id, text, existing)
                started_ctx = ClientContext.model_validate(loads(session["client_context_json"]))
                history = self._history(conn, session_id, request_id)
            if on_admitted is not None:
                on_admitted()
            return self._run_turn(
                session_id, request_id, text, attempt, user_msg_id, started_ctx, history, t_start
            )
        finally:
            if lock is not None:
                lock.release()

    @staticmethod
    def _turn_in_progress(request_id: str) -> ApiError:
        return ApiError(409, "turn_in_progress", "Дождитесь ответа на предыдущее сообщение",
                        request_id=request_id, retryable=True)

    @staticmethod
    def _turn_superseded(request_id: str) -> ApiError:
        return ApiError(409, "turn_superseded",
                        "После этого запроса начался новый ход. Отправьте текст как новое сообщение.",
                        request_id=request_id)

    def _start_attempt(self, conn, session_id, request_id, text, existing) -> tuple[int, int]:
        pending = conn.execute(
            "SELECT 1 FROM turns WHERE session_id=? AND status='pending'", (session_id,)
        ).fetchone()
        if pending:
            raise self._turn_in_progress(request_id)
        if existing:
            latest_user = conn.execute(
                "SELECT id, request_id FROM messages WHERE session_id=? AND role='user' ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not latest_user or latest_user["request_id"] != request_id:
                raise self._turn_superseded(request_id)
            conn.execute(
                "UPDATE turns SET status='pending', attempts=attempts+1, error_code=NULL, "
                "error_message=NULL, finished_at=NULL WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            )
            return existing["attempts"] + 1, latest_user["id"]
        conn.execute(
            "INSERT INTO turns(session_id, request_id, status, user_text, created_at) VALUES (?,?,?,?,?)",
            (session_id, request_id, "pending", text, now_iso()),
        )
        return 1, self._insert_message(conn, session_id, Role.user, text, request_id, in_context=0)

    def _run_turn(self, session_id, request_id, text, attempt, user_msg_id, started_ctx, history, t_start):
        stages = [StageTiming(name="backend_prepare", ms=(time.perf_counter() - t_start) * 1000)]
        t = time.perf_counter()
        try:
            req = ConsultationRequest(session_id=session_id, request_id=request_id, history=history,
                                      message=text, client_context=started_ctx.model_copy(deep=True),
                                      debug=self.collect_debug)
            result = self.engine.consult(req)
            if not isinstance(result, ConsultationResult):
                result = ConsultationResult.model_validate(result)
            if result.session_id != session_id or result.request_id != request_id:
                raise ValueError("engine returned result for another session/request")
        except Exception as exc:  # noqa: BLE001 — любая ошибка этапа должна попасть в trace
            engine_ms = (time.perf_counter() - t) * 1000
            stages.append(StageTiming(name="engine", ms=engine_ms, status="error", error=type(exc).__name__))
            stages.append(StageTiming(name="total", ms=(time.perf_counter() - t_start) * 1000, status="error"))
            log.exception("engine failed session=%s request=%s", session_id, request_id)
            self._record_failure(session_id, request_id, attempt, stages, code="engine_error",
                                 stage="engine", error=exc)
            raise ApiError(502, "engine_error",
                           "Не удалось подготовить ответ. Повторите отправку — сообщение не потеряется.",
                           request_id=request_id, retryable=True) from exc
        stages.append(StageTiming(name="engine", ms=(time.perf_counter() - t) * 1000))

        t = time.perf_counter()
        try:
            return self._persist_result(session_id, request_id, attempt, user_msg_id, result,
                                        started_ctx, stages, t, t_start)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой хранилища: ход откатан, его можно повторить
            log.exception("persist failed session=%s request=%s", session_id, request_id)
            self._record_failure(session_id, request_id, attempt, stages, code="storage_error",
                                 stage="backend_persist", error=exc, result=result)
            raise ApiError(500, "storage_error", "Не удалось сохранить ответ. Повторите отправку.",
                           request_id=request_id, retryable=True) from exc

    def _unfinished_attempt(self, conn, session_id, request_id, attempt):
        turn = conn.execute(
            "SELECT status, attempts, error_code FROM turns WHERE session_id=? AND request_id=?",
            (session_id, request_id),
        ).fetchone()
        if not turn or turn["attempts"] != attempt or turn["status"] not in ("pending", "cancelled"):
            raise self._turn_superseded(request_id)
        return turn

    @staticmethod
    def _cancelled_error(request_id: str, reason: Optional[str]) -> ApiError:
        if reason == "session_closed":
            return ApiError(409, "session_closed", "Этот диалог завершён. Начните новый диалог.",
                            request_id=request_id)
        return ApiError(409, "turn_cancelled", "Запрос был отменён", request_id=request_id)

    def _record_failure(self, session_id, request_id, attempt, stages, *, code, stage, error, result=None):
        message = redact_text(str(error))[:500]
        with self.db.tx() as conn:
            turn = self._unfinished_attempt(conn, session_id, request_id, attempt)
            cancelled = turn["status"] == "cancelled"
            if not cancelled:
                conn.execute(
                    "UPDATE turns SET status='failed', error_code=?, error_message=?, finished_at=? "
                    "WHERE session_id=? AND request_id=?",
                    (code, message, now_iso(), session_id, request_id),
                )
            self._store_trace(
                conn, session_id, request_id, attempt, "cancelled" if cancelled else "failed", stages,
                result, error={"stage": stage, "type": type(error).__name__, "message": message},
            )
        if cancelled:
            raise self._cancelled_error(request_id, turn["error_code"]) from None

    def _persist_result(self, session_id, request_id, attempt, user_msg_id, result, started_ctx, stages, t, t_start):
        with self.db.tx() as conn:
            turn = self._unfinished_attempt(conn, session_id, request_id, attempt)
            cancelled = turn["status"] == "cancelled"
            prev = conn.execute("SELECT id FROM messages WHERE session_id=? AND request_id=? AND role='assistant'",
                                (session_id, request_id)).fetchone()
            if prev:
                conn.execute("UPDATE messages SET content=?, in_context=? WHERE id=?",
                             (result.answer, not cancelled, prev["id"]))
                asst_id = prev["id"]
            else:
                asst_id = self._insert_message(conn, session_id, Role.assistant, result.answer, request_id,
                                               in_context=not cancelled)
            if not cancelled:
                conn.execute("UPDATE messages SET in_context=1 WHERE id=?", (user_msg_id,))
                new_ctx = self.leads.sync_from_engine(
                    conn, session_id, result.client_context, result.lead_draft, started_ctx=started_ctx
                )
                conn.execute("UPDATE sessions SET client_context_json=?, updated_at=? WHERE id=?",
                             (new_ctx.model_dump_json(), now_iso(), session_id))
            stages.append(StageTiming(name="backend_persist", ms=(time.perf_counter() - t) * 1000))
            stages.append(StageTiming(name="total", ms=(time.perf_counter() - t_start) * 1000))
            self._store_trace(
                conn, session_id, request_id, attempt, "cancelled" if cancelled else "completed", stages, result
            )
            if not cancelled:
                lead = self.leads.view(session_id)
                response = TurnResponse(
                    session_id=session_id, request_id=request_id, user_message_id=user_msg_id,
                    assistant_message_id=asst_id, answer=result.answer, action=result.action,
                    clarifying_question=result.clarifying_question,
                    lead=lead if lead.status != "none" else None,
                    debug_available=True,
                )
                conn.execute("UPDATE turns SET status='completed', response_json=?, finished_at=? "
                             "WHERE session_id=? AND request_id=?",
                             (response.model_dump_json(), now_iso(), session_id, request_id))
        if cancelled:
            raise self._cancelled_error(request_id, turn["error_code"])
        return response

    def cancel_turn(self, session_id: str, request_id: str) -> str:
        with self.db.tx() as conn:
            row = conn.execute("SELECT status FROM turns WHERE session_id=? AND request_id=?",
                               (session_id, request_id)).fetchone()
            if not row:
                raise ApiError(404, "turn_not_found", "Ход не найден", request_id=request_id)
            if row["status"] == "pending":
                conn.execute("UPDATE turns SET status='cancelled', finished_at=? "
                             "WHERE session_id=? AND request_id=?", (now_iso(), session_id, request_id))
                return "cancelled"
            return row["status"]

    def _insert_message(self, conn, session_id, role: Role, content, request_id, in_context: int) -> int:
        seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?",
                           (session_id,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO messages(session_id, seq, role, content, request_id, in_context, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, seq, role.value, content, request_id, in_context, now_iso()))
        return cur.lastrowid

    # ================================================================ traces
    def _store_trace(self, conn, session_id, request_id, attempt, status, stages, result: Optional[ConsultationResult],
                     error: Optional[dict] = None) -> None:
        timings = list(stages)
        if result:
            timings = [StageTiming(name=f"engine.{s.name}", ms=s.ms, status=s.status, error=s.error)
                       for s in result.timings] + timings
        data = {
            "engine": self.engine_name,
            "timings": [s.model_dump() for s in timings],
            "retrieved": [e.model_dump() for e in result.retrieved] if result else [],
            "used_evidence_ids": list(result.used_evidence_ids) if result else [],
            "action": result.action.value if result else None,
            "error": error,
        }
        conn.execute(
            "INSERT OR REPLACE INTO traces(trace_id, session_id, request_id, attempt, status, data_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("t_" + uuid.uuid4().hex[:20], session_id, request_id, attempt, status, dumps(redact(data)), now_iso()))

    def trace(self, session_id: str, request_id: str) -> TraceView:
        row = self.db.one("SELECT * FROM traces WHERE session_id=? AND request_id=? ORDER BY attempt DESC LIMIT 1",
                          (session_id, request_id))
        if not row:
            raise ApiError(404, "trace_not_found", "Для этого ответа нет отладочной трассы", request_id=request_id)
        d = loads(row["data_json"])
        retrieved = d["retrieved"]
        by_id = {e["evidence_id"]: e for e in retrieved}
        used_ids = d["used_evidence_ids"]
        return TraceView(
            trace_id=row["trace_id"], session_id=session_id, request_id=request_id, attempt=row["attempt"],
            status=row["status"], engine=d["engine"], created_at=row["created_at"], timings=d["timings"],
            retrieved=retrieved, used=[by_id[i] for i in used_ids if i in by_id],
            used_missing_ids=[i for i in used_ids if i not in by_id], action=d.get("action"), error=d.get("error"),
        )

    def traces_for_session(self, session_id: str) -> list[dict]:
        rows = self.db.all("SELECT request_id, attempt, status, data_json, created_at FROM traces "
                           "WHERE session_id=? ORDER BY created_at", (session_id,))
        return [{"request_id": r["request_id"], "attempt": r["attempt"], "status": r["status"],
                 "created_at": r["created_at"], **loads(r["data_json"])} for r in rows]
