"""Замер задержки на фиксированных сценариях (S4): p50/p95 и доля ошибок.

    # in-process, текущий engine из CONSULTATION_ENGINE (по умолчанию — заглушка)
    python scripts/bench_latency.py --runs 5 --out reports/latency.json
    # против запущенного сервера
    python scripts/bench_latency.py --url http://localhost:8000 --runs 5

Цифры заглушки НЕ являются результатом системы: в отчёте явно пишется engine.
Ориентир организаторов 5–7 с — не жёсткий timeout (QA09).
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCENARIOS: dict[str, list[str]] = {
    "simple_question": ["Нужно считать электроэнергию на 3 объектах, что подойдёт?"],
    "clarify_then_answer": ["Привет", "Нужен удалённый мониторинг оборудования на заводе"],
    "topic_change": ["Учёт электроэнергии на складе", "А что с телеметрией оборудования?",
                     "Вернёмся к учёту: какие отчёты будут?"],
    "to_lead_phone_only": ["Интересует учёт энергопотребления", "Свяжите со специалистом",
                           "+7 999 000-00-31"],
    "refusal": ["Расскажите про мониторинг", "Не хочу оставлять телефон", "Подходит ли для счётчиков?"],
}


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * len(s) + 0.5) - 1))
    return round(s[k], 1)


def summarize(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": pct(values, 50), "p95_ms": pct(values, 95),
            "max_ms": round(max(values), 1) if values else None,
            "mean_ms": round(statistics.fmean(values), 1) if values else None}


def git_sha() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def make_client(url: str | None):
    if url:
        import httpx
        return httpx.Client(base_url=url, timeout=120), None
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.settings import Settings
    tmp = tempfile.mkdtemp(prefix="bench_")
    app = create_app(Settings(db_path=f"{tmp}/bench.sqlite3", leads_dir=f"{tmp}/leads", debug_api=True))
    return TestClient(app), app


def run(url: str | None, runs: int) -> dict:
    client, _ = make_client(url)
    engine = client.get("/api/v1/health").json().get("engine")
    total, stage_ms, errors, turns = [], {}, [], 0
    for _ in range(runs):
        for name, msgs in SCENARIOS.items():
            s = client.post("/api/v1/sessions", json={}).json()
            h = {"X-Client-Token": s["client_token"]}
            for text in msgs:
                rid = str(uuid.uuid4())
                t0 = time.perf_counter()
                r = client.post(f"/api/v1/sessions/{s['session_id']}/messages", headers=h,
                                json={"request_id": rid, "text": text})
                dt = (time.perf_counter() - t0) * 1000
                turns += 1
                if r.status_code != 200:
                    errors.append({"scenario": name, "status": r.status_code,
                                   "code": r.json().get("error", {}).get("code")})
                    continue
                total.append(dt)
                tr = client.get(f"/api/v1/sessions/{s['session_id']}/turns/{rid}/debug", headers=h)
                if tr.status_code == 200:
                    for st in tr.json()["timings"]:
                        stage_ms.setdefault(st["name"], []).append(st["ms"])
    p95 = pct(total, 95)
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "engine": engine,
        "is_stub_engine": bool(engine and "stub_engine" in engine),
        "target": url or "in-process TestClient",
        "host": platform.platform(),
        "turns": turns,
        "errors": len(errors),
        "error_rate": round(len(errors) / turns, 4) if turns else None,
        "error_samples": errors[:10],
        "client_total": summarize(total),
        "stages": {k: summarize(v) for k, v in sorted(stage_ms.items())},
        "guideline_5_7s": None if p95 is None else
        ("p95 в пределах ориентира" if p95 <= 7000 else "p95 выше ориентира 7 с — нужна оптимизация"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out")
    a = ap.parse_args()
    rep = run(a.url, a.runs)
    text = json.dumps(rep, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if rep["is_stub_engine"]:
        print("\nВНИМАНИЕ: замер на синтетической заглушке — не публиковать как скорость системы.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
