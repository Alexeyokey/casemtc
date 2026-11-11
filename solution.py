"""Read platform questions and write a complete submission CSV."""

import argparse
import asyncio
import csv
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile


MAX_ANSWER_LENGTH = 10_000
AnswerQuestion = Callable[[str], Awaitable[str]]


class SubmissionError(Exception):
    """A run error that can be printed without exposing the input or response."""


def read_questions(path: Path) -> list[tuple[str, str]]:
    rows = []
    seen = set()
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            header = reader.fieldnames
            if not header or not {"id", "question"}.issubset(header):
                raise SubmissionError("В questions.csv нужны колонки id и question.")
            if len(header) != len(set(header)):
                raise SubmissionError("В questions.csv повторяются имена колонок.")

            for row in reader:
                line = reader.line_num
                if None in row or any(value is None for value in row.values()):
                    raise SubmissionError(f"Неверное число полей в записи, строка {line}.")
                question_id = row["id"]
                question = row["question"]
                if not question_id.strip() or not question.strip():
                    raise SubmissionError(f"Пустой id или вопрос в записи, строка {line}.")
                if question_id in seen:
                    raise SubmissionError(f"Повторяющийся id в записи, строка {line}.")
                seen.add(question_id)
                rows.append((question_id, question))
    except (OSError, UnicodeError, csv.Error) as error:
        raise SubmissionError("Не удалось прочитать questions.csv как CSV в UTF-8.") from error
    return rows


async def run_submission(
    data_dir: Path,
    answer_question: AnswerQuestion,
    *,
    output_path: Path = Path("submission.csv"),
) -> int:
    """Call the handler once per question with the original cell contents."""
    rows = read_questions(data_dir / "questions.csv")
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=output_path.parent, delete=False,
        ) as target:
            temporary_path = Path(target.name)
            writer = csv.writer(target)
            writer.writerow(("id", "answer"))
            for index, (question_id, question) in enumerate(rows, start=1):
                try:
                    answer = await answer_question(question)
                except Exception as error:
                    raise SubmissionError(
                        f"Обработчик завершился ошибкой на вопросе №{index} "
                        f"({type(error).__name__})."
                    ) from error

                if not isinstance(answer, str) or not answer.strip():
                    raise SubmissionError(f"Пустой или нетекстовый ответ на вопрос №{index}.")
                if len(answer) > MAX_ANSWER_LENGTH:
                    raise SubmissionError(f"Ответ на вопрос №{index} длиннее 10 000 символов.")
                writer.writerow((question_id, answer))

        os.replace(temporary_path, output_path)
    except (OSError, UnicodeError, csv.Error) as error:
        raise SubmissionError("Не удалось записать submission.csv в UTF-8.") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return len(rows)


async def demo_answer(_question: str) -> str:
    return "Синтетический ответ для проверки формата CSV."


async def run_platform_submission(data_dir: Path) -> int:
    from consultation import parse_csv_history
    from knowledge_base import KnowledgeBase, KnowledgeBaseError
    from organizer_api import APIConfig, ConfigurationError, OrganizerClient
    from rag_consultation import RagConsultant, configured_top_k

    started_at = time.monotonic()
    try:
        top_k = configured_top_k()
    except ValueError as error:
        raise SubmissionError(str(error)) from None
    try:
        config = APIConfig.from_env()
    except ConfigurationError as error:
        raise SubmissionError(f"Проверьте конфигурацию API организаторов. {error}.") from None
    try:
        knowledge = KnowledgeBase.from_zip(data_dir / "knowledge-base.zip")
    except KnowledgeBaseError as error:
        raise SubmissionError(str(error)) from None

    async with OrganizerClient(config, deadline=started_at + config.run_budget_s) as client:
        consultant = RagConsultant(knowledge, client.chat, top_k=top_k)

        async def answer_question(question: str) -> str:
            return (await consultant.consult(parse_csv_history(question))).text

        return await run_submission(data_dir, answer_question)


def main(
    argv: Sequence[str] | None = None, *, answer_question: AnswerQuestion | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Собрать submission.csv из questions.csv.")
    parser.add_argument("--demo", action="store_true", help="Использовать синтетические ответы.")
    args = parser.parse_args(argv)
    handler = demo_answer if args.demo else answer_question
    data_dir = Path(os.environ.get("DATA_DIR") or "data")
    run = run_submission(data_dir, handler) if handler is not None else run_platform_submission(data_dir)
    try:
        count = asyncio.run(run)
    except SubmissionError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Записан submission.csv. Ответов: {count}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
