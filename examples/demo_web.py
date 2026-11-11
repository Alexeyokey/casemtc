"""Local browser adapter for the synthetic consultation demo."""

import argparse
import asyncio
from contextlib import closing
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs
from uuid import uuid4
from wsgiref.simple_server import make_server

from consultation import Turn
from examples.demo_consultation import consult as demo_consult


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    request_id TEXT,
    action TEXT,
    summary TEXT,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    UNIQUE (session_id, request_id)
);
CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS leads (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    phone TEXT NOT NULL,
    summary TEXT NOT NULL
);
"""


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _connect(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _fields(encoded):
    try:
        parsed = parse_qs(encoded, keep_blank_values=True, max_num_fields=8, errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise RequestError("400 Bad Request", "Некорректные данные формы.") from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise RequestError("400 Bad Request", "Поля формы должны быть уникальны.")
    return {key: values[0] for key, values in parsed.items()}


def _post_fields(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 <= length <= 32_768:
            raise ValueError("form length")
        return _fields(environ["wsgi.input"].read(length).decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RequestError("400 Bad Request", "Некорректные данные формы.") from exc


def _require_session(db, session_id):
    if not session_id or not db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone():
        raise RequestError("404 Not Found", "Диалог не найден.")


def _messages(db, session_id):
    return db.execute(
        "SELECT id, role, text, action, summary, evidence_ids "
        "FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()


def _lead(db, session_id):
    return db.execute("SELECT phone, summary FROM leads WHERE session_id = ?", (session_id,)).fetchone()


def _chat(db, session_id, fields, consult):
    message = fields.get("message", "").strip()
    request_id = fields.get("request_id") or None
    if not message or len(message) > 4_000 or (request_id and len(request_id) > 64):
        raise RequestError("400 Bad Request", "Введите сообщение длиной от 1 до 4000 символов.")
    if request_id:
        previous = db.execute(
            "SELECT text FROM messages WHERE session_id = ? AND request_id = ?",
            (session_id, request_id),
        ).fetchone()
        if previous:
            if previous["text"] != message:
                raise RequestError("409 Conflict", "Этот запрос уже использован для другого сообщения.")
            return

    history = tuple(Turn(row["role"], row["text"]) for row in _messages(db, session_id))
    reply = consult((*history, Turn("user", message)))
    db.execute(
        "INSERT INTO messages (session_id, role, text, request_id) VALUES (?, 'user', ?, ?)",
        (session_id, message, request_id),
    )
    db.execute(
        "INSERT INTO messages (session_id, role, text, action, summary, evidence_ids) "
        "VALUES (?, 'assistant', ?, ?, ?, ?)",
        (session_id, reply.text, reply.action, reply.summary, json.dumps(reply.evidence_ids)),
    )


def _save_lead(db, session_id, fields):
    entered = fields.get("phone", "").strip()
    phone = re.sub(r"[ ().-]", "", entered)
    if len(entered) > 40 or not re.fullmatch(r"\+?[0-9]{10,15}", phone):
        raise RequestError("400 Bad Request", "Введите тестовый телефон из 10–15 цифр.")
    existing = _lead(db, session_id)
    if existing:
        if existing["phone"] != phone:
            raise RequestError("409 Conflict", "В этом диалоге заявка уже сохранена.")
        return

    latest = db.execute(
        "SELECT id, action, summary FROM messages WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if not latest or latest["action"] != "offer_contact" or not latest["summary"]:
        raise RequestError("409 Conflict", "Предложение сохранить заявку ещё не готово.")
    if fields.get("offer_id") != str(latest["id"]):
        raise RequestError("409 Conflict", "Резюме изменилось. Обновите диалог и проверьте новую версию.")
    db.execute(
        "INSERT INTO leads (session_id, phone, summary) VALUES (?, ?, ?)",
        (session_id, phone, latest["summary"]),
    )


def _page(content, synthetic):
    banner = (
        "Локальное демо на синтетических данных. Используйте тестовые контакты."
        if synthetic else "Локальная проверка поиска по базе МТС. Используйте тестовые контакты."
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>EnergyTool — локальное демо</title>
<style>
* {{ box-sizing: border-box }}
body {{ margin: 0; color: #202631; background: #f3f5f8; font: 1rem/1.55 system-ui, sans-serif }}
main {{ max-width: 46rem; margin: auto; padding: 1.5rem 1rem 2rem }}
h1 {{ margin: 0; line-height: 1.2 }}
section, article {{ background: white; border-radius: .65rem; padding: 1rem; margin: .75rem 0 }}
article.user {{ background: #e4ebf8; margin-left: 2rem }}
p {{ margin: .4rem 0; overflow-wrap: anywhere; white-space: pre-wrap }}
form {{ margin: .75rem 0 }}
label, textarea, input {{ display: block; width: 100%; margin: .3rem 0 }}
input, textarea, button {{ font: inherit; border: 1px solid #5a6782; border-radius: .4rem; padding: .45rem .65rem }}
button {{ color: white; background: #1b4286; cursor: pointer }}
summary {{ cursor: pointer }}
</style></head><body><main>
<h1>Консультант EnergyTool</h1>
<p>{banner}</p>
{content}</main></body></html>"""


def _render(db, session_id, synthetic):
    new = '<form method="post" action="/sessions"><button>Новый диалог</button></form>'
    if not session_id:
        return _page(new, synthetic)
    rows = _messages(db, session_id)
    lead = _lead(db, session_id)
    hidden = f'<input type="hidden" name="session_id" value="{escape(session_id)}">'
    conversation = []
    for row in rows:
        author = "Вы" if row["role"] == "user" else "Консультант"
        anchor = ' id="latest-reply"' if row["id"] == rows[-1]["id"] else ""
        sources = json.loads(row["evidence_ids"])
        details = (
            f'<details><summary>Источники ответа</summary>{escape(", ".join(sources))}</details>'
            if sources else ""
        )
        conversation.append(
            f'<article class="{row["role"]}"{anchor}><strong>{author}</strong>'
            f'<p>{escape(row["text"])}</p>{details}</article>'
        )
    contact = ""
    if lead:
        contact = f'<section id="saved-lead"><strong>Заявка сохранена</strong><p>{escape(lead["summary"])}</p></section>'
    elif rows and rows[-1]["action"] == "offer_contact" and rows[-1]["summary"]:
        contact = f"""<section><strong>Резюме для заявки</strong><p>{escape(rows[-1]["summary"])}</p>
<form method="post" action="/lead">{hidden}
<input type="hidden" name="offer_id" value="{rows[-1]['id']}">
<label for="phone">Тестовый телефон</label>
<input id="phone" type="tel" name="phone" maxlength="40" placeholder="+7 000 000-00-00" required>
<button>Сохранить заявку</button></form><p>Можно продолжить диалог без телефона.</p></section>"""
    chat = f"""<form method="post" action="/chat">{hidden}
<input type="hidden" name="request_id" value="{uuid4().hex}">
<label for="message">Ваше сообщение</label>
<textarea id="message" name="message" rows="2" maxlength="4000" required></textarea>
<button>Отправить</button></form>"""
    return _page(new + "".join(conversation) + contact + chat, synthetic)


def create_app(db_path, *, consult=demo_consult):
    """Create a WSGI app using a persistent SQLite file."""
    synthetic = consult is demo_consult
    with closing(_connect(db_path)) as db:
        db.executescript(SCHEMA)

    def app(environ, start_response):
        route = (environ["REQUEST_METHOD"], environ["PATH_INFO"])
        if route == ("GET", "/health"):
            return _health(db_path, start_response)
        try:
            with closing(_connect(db_path)) as db, db:
                if route == ("GET", "/"):
                    session_id = _fields(environ.get("QUERY_STRING", "")).get("session_id")
                    if session_id is not None:
                        _require_session(db, session_id)
                    page = _render(db, session_id, synthetic)
                elif route in {("POST", "/sessions"), ("POST", "/chat"), ("POST", "/lead")}:
                    fields = _post_fields(environ)
                    db.execute("BEGIN IMMEDIATE")
                    if route == ("POST", "/sessions"):
                        session_id = uuid4().hex
                        db.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
                    else:
                        session_id = fields.get("session_id")
                        _require_session(db, session_id)
                        if route == ("POST", "/chat"):
                            _chat(db, session_id, fields, consult)
                        else:
                            _save_lead(db, session_id, fields)
                    page = None
                else:
                    raise RequestError("404 Not Found", "Страница не найдена.")
            if page is None:
                fragment = {"/chat": "#latest-reply", "/lead": "#saved-lead"}.get(route[1], "")
                start_response("303 See Other", [("Location", f"/?session_id={session_id}{fragment}")])
                return [b""]
            status = "200 OK"
        except RequestError as exc:
            status = exc.status
            page = _page(f'<p role="alert">{escape(str(exc))}</p><a href="/">Открыть чат</a>', synthetic)
        start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
        return [page.encode("utf-8")]

    return app


def _health(db_path, start_response):
    database = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(database, uri=True)) as db:
            db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
        status, body = "200 OK", b'{"status":"ok"}'
    except sqlite3.Error:
        status, body = "503 Service Unavailable", b'{"status":"unavailable"}'
    start_response(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
    return [body]


def _api_consultation(archive):
    from knowledge_base import KnowledgeBase
    from organizer_api import APIConfig, OrganizerAPIError, OrganizerClient
    from rag_consultation import RagConsultant, configured_top_k

    config = APIConfig.from_env()
    top_k = configured_top_k()
    knowledge = KnowledgeBase.from_zip(archive)

    async def answer(history):
        async with OrganizerClient(config) as client:
            return await RagConsultant(knowledge, client.chat, top_k=top_k).consult(history)

    def consult(history):
        try:
            return asyncio.run(answer(history))
        except OrganizerAPIError:
            raise RequestError("503 Service Unavailable", "Сервис консультации временно недоступен.") from None

    return consult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Path to a local SQLite database")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес, на котором слушает веб-сервер")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--knowledge-base", type=Path, default=os.environ.get("ENERGYTOOL_KB_PATH") or None,
        help="Архив базы МТС для режима с API организаторов",
    )
    args = parser.parse_args()
    consult = demo_consult
    if args.knowledge_base:
        try:
            consult = _api_consultation(args.knowledge_base)
        except ValueError as error:
            parser.error(str(error))
    with make_server(args.host, args.port, create_app(args.db, consult=consult)) as server:
        print(f"EnergyTool слушает http://{args.host}:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
