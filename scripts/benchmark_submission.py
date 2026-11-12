"""Measure a fresh EnergyTool submission against a synthetic local HTTP proxy."""

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from zipfile import ZipFile

from build_submission import RUNTIME_FILES, BuildError, build_submission


BUDGET_SECONDS = 20 * 60
SYNTHETIC_ANSWER = "Начните с проверки совместимости счётчиков."
QUESTIONS = (
    ("001", "Как подключить счётчики арендаторов?"),
    ("002", "Пользователь: У арендаторов есть счётчики.\n"
     "Ассистент: Какой у вас объект?\nПользователь: Бизнес-центр. С чего начать?"),
    ("003", "Рецепт плова с шафраном"),
)


class BenchmarkError(Exception):
    """The local synthetic run could not be verified."""


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path not in ("/cold/v1/chat/completions", "/warm/v1/chat/completions", "/demo/v1/chat/completions"):
            self.send_error(404)
            return
        with self.server.calls_lock:
            self.server.calls[self.path.split("/")[1]] += 1
        try:
            size = int(self.headers["Content-Length"])
            if not 0 < size <= 1_000_000:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(size))
            payload = json.loads(request["messages"][-1]["content"])
            source = payload["evidence"][0]["id"]
        except (KeyError, IndexError, TypeError, ValueError):
            self.send_error(400)
            return

        result = {
            "text": SYNTHETIC_ANSWER, "action": "answer", "summary": None,
            "evidence_ids": [source],
        }
        response = json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(result)},
        }]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def write_fixtures(data_dir: Path) -> None:
    data_dir.mkdir()
    with (data_dir / "questions.csv").open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows((("id", "question"), *QUESTIONS))
    with ZipFile(data_dir / "knowledge-base.zip", "w") as archive:
        archive.writestr("metering.md", "# Счётчики арендаторов\n\n"
                         "Для бизнес-центра сначала проверьте совместимость счётчиков.\n")


def run_worker(command: list[str], work_dir: Path, data_dir: Path, url: str,
               *, package: Path | None = None) -> subprocess.CompletedProcess[str]:
    # Pass only fixture settings, including a dummy value required by the API contract.
    env = {
        "PYTHONUTF8": "1",
        "DATA_DIR": str(data_dir),
        "LLM_BASE_URL": url,
        "LLM_API_KEY": "synthetic-local-only",
        "LLM_MODEL": "synthetic-local-only",
        "API_MAX_RETRIES": "0",
    }
    if package is not None:
        env["PYTHONPATH"] = str(package)
    result = subprocess.run([sys.executable, "-B", *command], cwd=work_dir, env=env,
                            capture_output=True, text=True, timeout=BUDGET_SECONDS)
    if result.returncode:
        raise BenchmarkError(f"Локальный процесс завершился с кодом {result.returncode}. {result.stderr.strip()}")
    return result


def validate_csv(output: Path, expected_ids: list[str]) -> list[str]:
    with output.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
    if reader.fieldnames != ["id", "answer"] or [row["id"] for row in rows] != expected_ids:
        raise BenchmarkError("В выходном CSV не совпадают схема или ID вопросов.")
    answers = [row["answer"] for row in rows]
    if any(not answer or not answer.strip() for answer in answers):
        raise BenchmarkError("В выходном CSV есть пустой ответ.")
    return answers


def check_demo(root: Path, entrypoint: Path, questions: Path, knowledge: Path, url: str) -> dict:
    data_dir, work_dir = root / "demo-input", root / "demo-work"
    data_dir.mkdir()
    work_dir.mkdir()
    shutil.copyfile(questions, data_dir / "questions.csv")
    shutil.copyfile(knowledge, data_dir / "knowledge-base.zip")
    with (data_dir / "questions.csv").open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != ["id", "question"]:
            raise BenchmarkError("В демо-вопросах ожидаются столбцы id,question.")
        ids = [row["id"] for row in reader]
    if not ids or len(ids) != len(set(ids)) or any(not item for item in ids):
        raise BenchmarkError("В демо-вопросах нужны непустые уникальные ID.")
    started = time.perf_counter()
    run_worker([str(entrypoint)], work_dir, data_dir, url)
    validate_csv(work_dir / "submission.csv", ids)
    return {"questions": len(ids), "answers": len(ids), "seconds": round(time.perf_counter() - started, 6)}


def percentile(samples: list[float], quantile: float) -> float:
    return sorted(samples)[math.ceil(len(samples) * quantile) - 1]


async def measure_warm(rounds: int) -> dict:
    # This worker imports the freshly extracted package through PYTHONPATH.
    from consultation import parse_csv_history
    from knowledge_base import KnowledgeBase
    from organizer_api import APIConfig, OrganizerClient
    from rag_consultation import NO_EVIDENCE, RagConsultant

    started = time.perf_counter()
    knowledge = KnowledgeBase.from_zip(Path(os.environ["DATA_DIR"]) / "knowledge-base.zip")
    index_duration = time.perf_counter() - started
    samples = []
    async with OrganizerClient(APIConfig.from_env()) as client:
        consultant = RagConsultant(knowledge, client.chat)
        await consultant.consult(parse_csv_history(QUESTIONS[0][1]))
        for _ in range(rounds):
            for key, question in QUESTIONS:
                started = time.perf_counter()
                reply = await consultant.consult(parse_csv_history(question))
                samples.append(time.perf_counter() - started)
                expected = NO_EVIDENCE if key == "003" else SYNTHETIC_ANSWER
                if reply.text != expected:
                    raise BenchmarkError(f"Прогретый обработчик дал неожиданный ответ для {key}.")

    return {
        "index_build_seconds": round(index_duration, 6),
        "rounds": rounds,
        "samples": len(samples),
        "end_to_end_p50_seconds": round(percentile(samples, 0.50), 6),
        "end_to_end_p95_seconds": round(percentile(samples, 0.95), 6),
    }


def source_revision(project: Path, archive: ZipFile) -> tuple[str, bool]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    for name in RUNTIME_FILES:
        committed = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=project, capture_output=True)
        if committed.returncode or committed.stdout != archive.read(name):
            return commit, False
    return commit, True


def benchmark(project: Path, artifact: Path | None, rounds: int, *,
              demo_inputs: tuple[Path, Path] | None = None) -> dict:
    with TemporaryDirectory(prefix="energytool-benchmark-") as directory:
        root = Path(directory)
        archive_path = build_submission(project, artifact or root / "submission.zip")
        with archive_path.open("rb") as artifact_file:
            digest = hashlib.file_digest(artifact_file, "sha256").hexdigest()
        package, data_dir, work_dir = root / "package", root / "input", root / "work"
        work_dir.mkdir()
        write_fixtures(data_dir)
        with ZipFile(archive_path) as archive:
            commit, matches_commit = source_revision(project, archive)
            archive.extractall(package)

        with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
            server.calls = Counter()
            server.calls_lock = Lock()
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"http://127.0.0.1:{server.server_port}"
                started = time.perf_counter()
                run_worker([str(package / "solution.py")], work_dir, data_dir, f"{host}/cold/v1")
                cold_duration = time.perf_counter() - started
                answers = validate_csv(work_dir / "submission.csv", [key for key, _ in QUESTIONS])
                if answers[:2] != [SYNTHETIC_ANSWER] * 2 or answers[2] == SYNTHETIC_ANSWER:
                    raise BenchmarkError("Выходной CSV не совпадает с ожидаемым поведением фикстур.")
                warm = run_worker([str(Path(__file__).resolve()), "--worker", str(rounds)],
                                  work_dir, data_dir, f"{host}/warm/v1", package=package)
                metrics = json.loads(warm.stdout)
                demo = (check_demo(root, package / "solution.py", *demo_inputs, f"{host}/demo/v1")
                        if demo_inputs else None)
            finally:
                server.shutdown()
                thread.join()

        if server.calls["cold"] != 2 or server.calls["warm"] != 1 + 2 * rounds:
            raise BenchmarkError("Число обращений к локальному прокси не совпало с фикстурами.")
        report = {
            "mode": "synthetic_local_http_proxy",
            "platform_score": None,
            "profile": "runtime",
            "commit_sha": commit,
            "artifact_sha256": digest,
            "artifact_bytes": archive_path.stat().st_size,
            "artifact_matches_commit": matches_commit,
            "budget_seconds": BUDGET_SECONDS,
            "cold": {
                "seconds": round(cold_duration, 6),
                "questions": len(QUESTIONS),
                "chat_requests": server.calls["cold"],
                "within_local_budget": cold_duration <= BUDGET_SECONDS,
            },
            "index_build_seconds": metrics.pop("index_build_seconds"),
            "warm": {**metrics, "chat_requests": server.calls["warm"] - 1, "warmup_chat_requests": 1},
        }
        if demo is not None:
            report["organizer_demo_local"] = {**demo, "chat_requests": server.calls["demo"]}
        return report


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        print(json.dumps(asyncio.run(measure_warm(int(sys.argv[2])))))
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, help="Сохранить проверенный ZIP по этому пути.")
    parser.add_argument("--output", type=Path, help="Сохранить JSON-отчёт по этому пути.")
    parser.add_argument("--demo-questions", type=Path, help="Локальная копия выданного demo_questions.csv.")
    parser.add_argument("--demo-knowledge", type=Path, help="Локальная копия выданного knowledge-base.zip.")
    parser.add_argument("--rounds", type=int, default=10, help="Число прогретых проходов по трём вопросам.")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("Число проходов должно быть положительным.")
    if args.artifact and args.output and args.artifact.resolve() == args.output.resolve():
        parser.error("Для архива и отчёта нужны разные пути.")
    if bool(args.demo_questions) != bool(args.demo_knowledge):
        parser.error("Для демо-прогона укажите оба файла: --demo-questions и --demo-knowledge.")
    demo_inputs = (args.demo_questions, args.demo_knowledge) if args.demo_questions else None
    if demo_inputs:
        inputs = {path.resolve() for path in demo_inputs}
        outputs = {path.resolve() for path in (args.artifact, args.output) if path}
        if inputs & outputs:
            parser.error("Архив и отчёт должны записываться отдельно от входных файлов.")
    try:
        report = benchmark(Path(__file__).resolve().parents[1], args.artifact, args.rounds, demo_inputs=demo_inputs)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (BuildError, BenchmarkError, OSError, subprocess.SubprocessError, ValueError) as error:
        parser.exit(1, f"Не удалось выполнить локальный замер. {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
