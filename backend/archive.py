"""Архив всех сессий и экспорт для проверяющего (S2).

Архив — только для чтения людьми. Он никогда не подмешивается в контекст
новой сессии.

CLI:
    python -m backend.archive --db data/casemtc.sqlite3 --out exports/
создаёт exports/sessions.jsonl (по строке на сессию) и exports/sessions/<id>.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterator, Optional

from .db import Database, loads
from .leads import LeadService


def session_record(db: Database, session_id: str, *, include_traces: bool = False,
                   leads: Optional[LeadService] = None) -> dict:
    s = db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
    msgs = db.all("SELECT seq, role, content, request_id, in_context, created_at FROM messages "
                  "WHERE session_id=? ORDER BY seq", (session_id,))
    turns = db.all("SELECT request_id, status, error_code, attempts, created_at, finished_at FROM turns "
                   "WHERE session_id=? ORDER BY created_at", (session_id,))
    lead_row = db.one("SELECT * FROM leads WHERE session_id=?", (session_id,))
    rec = {
        "session_id": s["id"],
        "status": s["status"],
        "channel": s["channel"],
        "created_at": s["created_at"],
        "closed_at": s["closed_at"],
        "close_reason": s["close_reason"],
        "client_context": loads(s["client_context_json"]),
        "messages": [dict(m) | {"in_context": bool(m["in_context"])} for m in msgs],
        "turns": [dict(t) for t in turns],
        "lead": None,
    }
    if lead_row:
        rec["lead"] = {
            "lead_id": lead_row["id"], "status": lead_row["status"], "revision": lead_row["revision"],
            "contact": loads(lead_row["contact_json"]), "summary": loads(lead_row["summary_json"]),
            "saved_at": lead_row["saved_at"],
        }
    if include_traces:
        rec["traces"] = [dict(r) | {"data": loads(r["data_json"])} for r in db.all(
            "SELECT request_id, attempt, status, created_at, data_json FROM traces WHERE session_id=? "
            "ORDER BY created_at", (session_id,))]
        for t in rec["traces"]:
            t.pop("data_json", None)
    return rec


def list_sessions(db: Database, limit: int = 100, offset: int = 0) -> list[dict]:
    rows = db.all(
        "SELECT s.id, s.status, s.created_at, s.closed_at, "
        "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS messages, "
        "(SELECT status FROM leads l WHERE l.session_id=s.id) AS lead_status "
        "FROM sessions s ORDER BY s.created_at DESC LIMIT ? OFFSET ?", (limit, offset))
    return [dict(r) for r in rows]


def iter_all(db: Database, include_traces: bool = False) -> Iterator[dict]:
    for r in db.all("SELECT id FROM sessions ORDER BY created_at"):
        yield session_record(db, r["id"], include_traces=include_traces)


def render_session_markdown(rec: dict) -> str:
    out = [f"# Сессия {rec['session_id']}", "",
           f"- Статус: {rec['status']}", f"- Создана: {rec['created_at']}",
           f"- Закрыта: {rec['closed_at'] or '—'}",
           f"- Заявка: {rec['lead']['status'] if rec['lead'] else 'нет'}", "", "## Диалог", ""]
    for m in rec["messages"]:
        who = "Клиент" if m["role"] == "user" else "Консультант"
        flag = "" if m["in_context"] else " _(не вошло в контекст: ход не завершён/отменён)_"
        out.append(f"**{who}** ({m['created_at']}){flag}:\n\n{m['content']}\n")
    return "\n".join(out)


def export_all(db: Database, out_dir: str | Path, include_traces: bool = False) -> dict:
    out = Path(out_dir)
    (out / "sessions").mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out / "sessions.jsonl", "w", encoding="utf-8") as f:
        for rec in iter_all(db, include_traces=include_traces):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            (out / "sessions" / f"{rec['session_id']}.md").write_text(render_session_markdown(rec), encoding="utf-8")
            n += 1
    return {"sessions": n, "out_dir": str(out)}


def main() -> None:
    p = argparse.ArgumentParser(description="Экспорт архива всех сессий")
    p.add_argument("--db", default="data/casemtc.sqlite3")
    p.add_argument("--out", default="exports")
    p.add_argument("--traces", action="store_true", help="включить отладочные трассы")
    a = p.parse_args()
    print(json.dumps(export_all(Database(a.db), a.out, a.traces), ensure_ascii=False))


if __name__ == "__main__":
    main()
