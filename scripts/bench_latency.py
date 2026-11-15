"""Замер задержки на сценариях консультации и сохранения заявки (S4/S5).

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
import math
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

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
    k = max(0, math.ceil(p / 100 * len(s)) - 1)
    return round(s[k], 1)


def summarize(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": pct(values, 50), "p95_ms": pct(values, 95),
            "max_ms": round(max(values), 1) if values else None,
            "mean_ms": round(statistics.fmean(values), 1) if values else None}


def git_sha() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


@contextmanager
def make_client(url: str | None):
    if url:
        with httpx.Client(base_url=url, timeout=120) as client:
            yield client
        return
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.settings import Settings

    with tempfile.TemporaryDirectory(prefix="bench_") as tmp:
        app = create_app(Settings(db_path=f"{tmp}/bench.sqlite3", leads_dir=f"{tmp}/leads", debug_api=True))
        try:
            with TestClient(app) as client:
                yield client
        finally:
            app.state.db.close()


def target_build(health: dict, supplied_sha: str | None) -> tuple[str | None, str]:
    reported_sha = health.get("build_sha")
    if isinstance(reported_sha, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", reported_sha):
        if supplied_sha and reported_sha.lower() != supplied_sha.lower():
            raise ValueError("SHA цели в --target-build-sha и в /api/v1/health различаются")
        return reported_sha.lower(), "health"
    return (supplied_sha.lower(), "provided") if supplied_sha else (None, "unknown")


def json_object(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def run(url: str | None, runs: int, supplied_sha: str | None = None) -> dict:
    total, lead_save, stage_ms, errors = [], [], {}, []
    operations = turns = traces = 0

    def request(client: httpx.Client, scenario: str, step: str, method: str, path: str, status: int, **kwargs):
        nonlocal operations
        operations += 1
        t0 = time.perf_counter()
        try:
            response = client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            errors.append({"scenario": scenario, "step": step, "status": None, "code": type(exc).__name__})
            return None, None
        elapsed = (time.perf_counter() - t0) * 1000
        body = json_object(response)
        if response.status_code == status and body:
            return body, elapsed
        error = body.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        errors.append({"scenario": scenario, "step": step, "status": response.status_code,
                       "code": code or "unexpected_response"})
        return None, None

    def record_trace(client: httpx.Client, path: str, headers: dict):
        nonlocal traces
        try:
            response = client.get(path, headers=headers)
        except httpx.RequestError:
            return
        if response.status_code == 200 and (timings := json_object(response).get("timings")):
            traces += 1
            for stage in timings:
                stage_ms.setdefault(stage["name"], []).append(stage["ms"])

    def run_scenario(client: httpx.Client, name: str, messages: list[str]):
        nonlocal turns
        session, _ = request(client, name, "session", "POST", "/api/v1/sessions", 201, json={})
        if session is None:
            return
        if not session.get("session_id") or not session.get("client_token"):
            errors.append({"scenario": name, "step": "session", "status": 201, "code": "invalid_session"})
            return
        path = f"/api/v1/sessions/{session['session_id']}"
        headers = {"X-Client-Token": session["client_token"]}
        for message in messages:
            rid = str(uuid.uuid4())
            turns += 1
            reply, elapsed = request(client, name, "turn", "POST", f"{path}/messages", 200,
                                     headers=headers, json={"request_id": rid, "text": message})
            if reply is None:
                return
            if reply.get("status") != "completed":
                errors.append({"scenario": name, "step": "turn", "status": 200, "code": "invalid_reply"})
                return
            total.append(elapsed)
            if reply.get("debug_available"):
                record_trace(client, f"{path}/turns/{rid}/debug", headers)

        if name != "to_lead_phone_only":
            return
        preview, _ = request(client, name, "lead_preview", "GET", f"{path}/lead", 200, headers=headers)
        if preview is None:
            return
        if preview.get("status") != "ready" or not preview.get("offer_id"):
            errors.append({"scenario": name, "step": "lead_preview", "status": 200, "code": "lead_not_ready"})
            return
        saved, elapsed = request(client, name, "lead_save", "POST", f"{path}/lead/save", 200,
                                 headers=headers, json={"idempotency_key": str(uuid.uuid4()),
                                                        "offer_id": preview["offer_id"]})
        if saved is None:
            return
        if saved.get("status") == "saved" and saved.get("delivery") == "stored_only":
            lead_save.append(elapsed)
        else:
            errors.append({"scenario": name, "step": "lead_save", "status": 200, "code": "lead_not_saved"})

    with make_client(url) as client:
        response = client.get("/api/v1/health")
        response.raise_for_status()
        health = json_object(response)
        engine = health.get("engine")
        if not isinstance(engine, str) or not engine:
            raise ValueError("/api/v1/health не вернул имя engine")
        build_sha, build_source = target_build(health, supplied_sha)
        for _ in range(runs):
            for name, messages in SCENARIOS.items():
                run_scenario(client, name, messages)
    p95 = pct(total, 95)
    is_stub = "stub_engine" in engine
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark_git_sha": git_sha(),
        "target_build_sha": build_sha,
        "target_build_sha_source": build_source,
        "engine": engine,
        "is_stub_engine": is_stub,
        "target": str(httpx.URL(url).copy_with(username=None, password=None, query=None, fragment=None))
        if url else "in-process TestClient",
        "host": platform.platform(),
        "turns": turns,
        "operations": operations,
        "errors": len(errors),
        "error_rate": round(len(errors) / operations, 4) if operations else None,
        "error_samples": errors[:10],
        "client_total": summarize(total),
        "lead_save": summarize(lead_save),
        "debug_traces": traces,
        "stages": {k: summarize(v) for k, v in sorted(stage_ms.items())},
        "guideline_5_7s": None if p95 is None or is_stub else
        ("p95 в пределах ориентира" if p95 <= 7000 else "p95 выше ориентира 7 с — нужна оптимизация"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--target-build-sha", help="SHA сборки цели, если сервер не сообщает его через health")
    a = ap.parse_args()
    if a.runs < 1:
        ap.error("--runs должен быть положительным")
    if a.target_build_sha and not re.fullmatch(r"[0-9a-fA-F]{7,64}", a.target_build_sha):
        ap.error("--target-build-sha должен содержать Git SHA")
    try:
        rep = run(a.url, a.runs, a.target_build_sha)
    except httpx.HTTPStatusError as exc:
        ap.error(f"/api/v1/health вернул HTTP {exc.response.status_code}")
    except httpx.RequestError as exc:
        ap.error(f"не удалось обратиться к /api/v1/health ({type(exc).__name__})")
    except ValueError as exc:
        ap.error(str(exc))
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
