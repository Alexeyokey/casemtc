"""Build the runtime-index ZIP submitted to the MTS evaluation platform."""

import argparse
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


RUNTIME_FILES = (
    "solution.py",
    "consultation.py",
    "knowledge_base.py",
    "organizer_api.py",
    "rag_consultation.py",
    "requirements.txt",
)
RUNTIME_REQUIREMENTS = frozenset({"httpx==0.28.1"})
MAX_ZIP_BYTES = 128 * 1024 * 1024


class BuildError(Exception):
    """The project cannot be packaged for the evaluation platform."""


def read_runtime_file(project_dir: Path, name: str) -> bytes:
    path = project_dir / name
    if path.is_symlink() or not path.is_file():
        raise BuildError(f"В корне проекта нужен обычный файл {name}.")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise BuildError(f"Файл {name} слишком большой для сабмита.")

    content = path.read_bytes()
    if name == "requirements.txt":
        lines = content.decode("utf-8").splitlines()
        pins = [line.split("#", 1)[0].strip() for line in lines]
        pins = [pin for pin in pins if pin]
        if set(pins) != RUNTIME_REQUIREMENTS or len(pins) != len(RUNTIME_REQUIREMENTS):
            raise BuildError("В requirements.txt ожидается закреплённая зависимость httpx==0.28.1.")
    else:
        try:
            compile(content, name, "exec")
        except (SyntaxError, ValueError) as error:
            raise BuildError(f"Не удалось скомпилировать файл {name}.") from error
    return content


def build_submission(project_dir: Path, output: Path) -> Path:
    """Publish an archive only after verifying its size, members and CRCs."""
    if output.suffix.lower() != ".zip":
        raise BuildError("Путь результата должен оканчиваться на .zip.")

    temporary = None
    try:
        with NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            with ZipFile(target, "w") as archive:
                for name in RUNTIME_FILES:
                    member = ZipInfo(name)
                    member.create_system = 3
                    member.external_attr = 0o100644 << 16
                    archive.writestr(member, read_runtime_file(project_dir, name), ZIP_DEFLATED)

        if temporary.stat().st_size > MAX_ZIP_BYTES:
            raise BuildError("Архив превышает лимит 128 МБ.")
        with ZipFile(temporary) as archive:
            if tuple(archive.namelist()) != RUNTIME_FILES or archive.testzip() is not None:
                raise BuildError("Не удалось проверить содержимое архива.")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Путь к выходному ZIP-архиву.")
    args = parser.parse_args()
    try:
        output = build_submission(Path(__file__).resolve().parents[1], args.output)
    except (BuildError, OSError, UnicodeError) as error:
        parser.exit(1, f"Не удалось собрать архив. {error}\n")
    print(f"Создан архив {output} ({output.stat().st_size} байт).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
