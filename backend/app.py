"""HTTP-адаптер ядра консультации (S2–S4). Запуск:

    uvicorn backend.app:app --host 0.0.0.0 --port 8000

Один процесс/воркер: блокировки ходов внутри процесса (см. docs/backend.md).
"""
from __future__ import annotations

import asyncio
import hmac
import os
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.contracts import CONTRACT_VERSION
from core.engine import DEFAULT_ENGINE, ConsultationEngine, load_engine

from . import archive
from .api_models import (
    ContactsUpdate,
    ErrorResponse,
    LeadSaveRequest,
    LeadView,
    SessionCreated,
    SessionCreateRequest,
    SessionView,
    TraceView,
    TurnRequest,
    TurnResponse,
)
from .db import Database
from .errors import ApiError
from .leads import LeadService
from .redaction import install_log_redaction
from .sessions import ChatService
from .settings import Settings

log = logging.getLogger("casemtc")
DEMO_DIR = Path(__file__).with_name("demo")
# Сборка web/ (React). Если её нет (локальный запуск без npm), "/" отдаёт демо-чат.
def _web_dist() -> Path:
    return Path(os.getenv("CASEMTC_WEB_DIST") or Path(__file__).resolve().parent.parent / "web" / "dist")
ERRORS = {409: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
          502: {"model": ErrorResponse}}


def create_app(settings: Optional[Settings] = None, engine: Optional[ConsultationEngine] = None) -> FastAPI:
    settings = settings or Settings()
    install_log_redaction()
    db = Database(settings.db_path)
    engine_name = settings.engine_spec or DEFAULT_ENGINE
    if engine is None:
        engine = load_engine(engine_name)
    else:
        engine_name = type(engine).__module__ + ":" + type(engine).__name__
    leads = LeadService(db, settings.leads_dir)
    chat = ChatService(db, engine, leads, engine_name=engine_name, collect_debug=settings.debug_api)
    recovered = chat.recover_after_restart()
    if recovered:
        log.warning("marked %d interrupted turns as failed", recovered)

    app = FastAPI(title="МТС Energytool — backend консультанта", version=CONTRACT_VERSION)
    app.state.db, app.state.chat, app.state.leads, app.state.settings = db, chat, leads, settings
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["Content-Type", "X-Client-Token", "X-Archive-Token"])

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "validation_error", "message": "Некорректные поля: " + ", ".join(fields),
            "request_id": None, "retryable": False}})

    def _session(sid: str, token: Optional[str]):
        return chat.authorize(sid, token)

    def _archive_allowed(token: Optional[str]) -> bool:
        return bool(token and settings.archive_token and
                    hmac.compare_digest(token.encode(), settings.archive_token.encode()))

    def _archive_guard(token: Optional[str]) -> None:
        if not settings.archive_token:
            raise ApiError(403, "archive_forbidden",
                           "Архив по HTTP выключен: задайте ARCHIVE_TOKEN или используйте "
                           "python -m backend.archive")
        if not _archive_allowed(token):
            raise ApiError(403, "archive_forbidden", "Неверный X-Archive-Token")

    # ------------------------------------------------------------- health
    web_dist = _web_dist()
    web_index = web_dist / "index.html"
    if (web_dist / "assets").is_dir():
        # Файлы с хешем в имени можно кешировать долго; index.html — никогда.
        app.mount("/assets", _ImmutableStatic(directory=web_dist / "assets"), name="web-assets")

    @app.get("/", include_in_schema=False)
    def web_page():
        page = web_index if web_index.is_file() else DEMO_DIR / "index.html"
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/demo", include_in_schema=False)
    def demo_page():
        return FileResponse(DEMO_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/demo.js", include_in_schema=False)
    def demo_script():
        return FileResponse(DEMO_DIR / "chat.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/health", include_in_schema=False)
    @app.get("/api/v1/health")
    def health():
        db.one("SELECT 1")
        return {"status": "ok", "contract_version": CONTRACT_VERSION, "engine": engine_name,
                "build_sha": settings.build_sha}

    # ------------------------------------------------------------- sessions
    @app.post("/api/v1/sessions", response_model=SessionCreated, status_code=201, responses=ERRORS)
    def create_session(body: SessionCreateRequest = Body(default_factory=SessionCreateRequest),
                       x_client_token: Optional[str] = Header(default=None)):
        if body.close_session_id:
            _session(body.close_session_id, x_client_token)
            chat.close_session(body.close_session_id, "new_dialog")
        return chat.create_session()

    @app.get("/api/v1/sessions/{session_id}", response_model=SessionView, responses=ERRORS)
    def get_session(session_id: str, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        return chat.session_view(session_id)

    @app.post("/api/v1/sessions/{session_id}/close", responses=ERRORS)
    def close_session(session_id: str, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        chat.close_session(session_id)
        return {"session_id": session_id, "status": "closed"}

    # ------------------------------------------------------------- turns
    @app.post("/api/v1/sessions/{session_id}/messages", response_model=TurnResponse, responses=ERRORS)
    def send_message(session_id: str, body: TurnRequest, x_client_token: Optional[str] = Header(default=None)):
        resp = chat.handle_turn(session_id, x_client_token, body.request_id, body.text)
        resp.debug_available = settings.debug_api
        return resp

    @app.post("/api/v1/sessions/{session_id}/messages/stream", responses=ERRORS)
    def send_message_stream(session_id: str, body: TurnRequest,
                            x_client_token: Optional[str] = Header(default=None)):
        """SSE: accepted after admission, then final/error. Admission errors send only error."""
        chat.authorize(session_id, x_client_token)

        async def gen():
            loop = asyncio.get_running_loop()
            events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

            def publish(event: str, data: dict) -> None:
                try:
                    loop.call_soon_threadsafe(events.put_nowait, (event, data))
                except RuntimeError:
                    # The loop may already be closed during server shutdown.
                    pass

            def run_turn() -> None:
                try:
                    response = chat.handle_turn(
                        session_id, x_client_token, body.request_id, body.text,
                        on_admitted=lambda: publish("accepted", {"request_id": body.request_id}),
                    )
                    response.debug_available = settings.debug_api
                    publish("final", response.model_dump(mode="json"))
                except ApiError as exc:
                    publish("error", exc.body()["error"] | {"status": exc.status})
                except Exception:
                    log.exception("stream failed session=%s request=%s", session_id, body.request_id)
                    exc = ApiError(500, "internal_error", "Не удалось обработать запрос.",
                                   request_id=body.request_id)
                    publish("error", exc.body()["error"] | {"status": exc.status})

            # Keep the admitted turn running after a lost connection so the
            # client can recover its result with the same request_id.
            loop.run_in_executor(None, run_turn)
            while True:
                event, data = await events.get()
                yield _sse(event, data)
                if event != "accepted":
                    break

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/v1/sessions/{session_id}/turns/{request_id}/cancel", responses=ERRORS)
    def cancel_turn(session_id: str, request_id: str, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        return {"request_id": request_id, "status": chat.cancel_turn(session_id, request_id)}

    @app.get("/api/v1/sessions/{session_id}/turns/{request_id}/debug", response_model=TraceView,
             responses=ERRORS)
    def debug_trace(session_id: str, request_id: str, response: Response,
                    x_client_token: Optional[str] = Header(default=None),
                    x_archive_token: Optional[str] = Header(default=None)):
        if not settings.debug_api:
            raise ApiError(403, "debug_disabled", "Debug выключен (CASEMTC_DEBUG_API=0)")
        _session(session_id, x_client_token)
        response.headers["Cache-Control"] = "no-store"
        trace = chat.trace(session_id, request_id)
        if _archive_allowed(x_archive_token):
            return trace
        return trace.model_copy(update={
            "retrieved": [item.model_copy(update={"text": ""}) for item in trace.retrieved],
            "used": [item.model_copy(update={"text": ""}) for item in trace.used],
            "timings": [item.model_copy(update={"error": None}) for item in trace.timings],
            "error": {key: trace.error[key] for key in ("stage", "type") if key in trace.error}
            if trace.error else None,
        })

    # ------------------------------------------------------------- leads
    @app.get("/api/v1/sessions/{session_id}/lead", response_model=LeadView, responses=ERRORS)
    def get_lead(session_id: str, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        return leads.view(session_id)

    @app.put("/api/v1/sessions/{session_id}/lead/contacts", response_model=LeadView, responses=ERRORS)
    def put_contacts(session_id: str, body: ContactsUpdate, x_client_token: Optional[str] = Header(default=None)):
        s = _session(session_id, x_client_token)
        if s["status"] != "active" and not leads._row(session_id):
            raise ApiError(409, "session_closed", "Диалог завершён")
        return leads.update_contacts(session_id, body)

    @app.post("/api/v1/sessions/{session_id}/lead/refuse", response_model=LeadView, responses=ERRORS)
    def refuse_contacts(session_id: str, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        return leads.refuse(session_id)

    @app.post("/api/v1/sessions/{session_id}/lead/save", response_model=LeadView, responses=ERRORS)
    def save_lead(session_id: str, body: LeadSaveRequest, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        return leads.save(session_id, body.idempotency_key, body.offer_id)

    @app.get("/api/v1/sessions/{session_id}/lead.md", response_class=PlainTextResponse, responses=ERRORS)
    def lead_markdown(session_id: str, x_client_token: Optional[str] = Header(default=None)):
        _session(session_id, x_client_token)
        return PlainTextResponse(leads.render_markdown(leads.saved_view(session_id)),
                                 media_type="text/markdown; charset=utf-8")

    # ------------------------------------------------------------- archive (проверяющий/менеджер)
    @app.get("/api/v1/archive/sessions")
    def archive_list(limit: int = Query(100, le=1000), offset: int = 0,
                     x_archive_token: Optional[str] = Header(default=None)):
        _archive_guard(x_archive_token)
        return {"sessions": archive.list_sessions(db, limit, offset)}

    @app.get("/api/v1/archive/sessions/{session_id}")
    def archive_session(session_id: str, traces: bool = False,
                        x_archive_token: Optional[str] = Header(default=None)):
        _archive_guard(x_archive_token)
        if not db.one("SELECT 1 FROM sessions WHERE id=?", (session_id,)):
            raise ApiError(404, "session_not_found", "Сессия не найдена")
        return archive.session_record(db, session_id, include_traces=traces)

    @app.get("/api/v1/archive/export.jsonl")
    def archive_export(x_archive_token: Optional[str] = Header(default=None)):
        _archive_guard(x_archive_token)
        return StreamingResponse((json.dumps(r, ensure_ascii=False) + "\n" for r in archive.iter_all(db)),
                                 media_type="application/x-ndjson")

    @app.get("/api/v1/archive/leads", response_model=list[LeadView])
    def archive_leads(x_archive_token: Optional[str] = Header(default=None)):
        _archive_guard(x_archive_token)
        return leads.list_saved()

    @app.get("/api/v1/archive/leads/{lead_id}.md", response_class=PlainTextResponse)
    def archive_lead_md(lead_id: str, x_archive_token: Optional[str] = Header(default=None)):
        _archive_guard(x_archive_token)
        return PlainTextResponse(leads.render_markdown(leads.by_id(lead_id)),
                                 media_type="text/markdown; charset=utf-8")

    return app


class _ImmutableStatic(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def __getattr__(name: str):
    # Ленивая инициализация `backend.app:app` для uvicorn, чтобы импорт модуля
    # в тестах не создавал data/casemtc.sqlite3.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
