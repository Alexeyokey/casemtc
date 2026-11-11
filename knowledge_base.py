"""Read a local Markdown archive and search its sections without an external model."""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from math import log
from pathlib import Path, PurePosixPath
import re
from stat import S_ISLNK
from zipfile import BadZipFile, ZipFile, ZipInfo
from zlib import error as DecompressionError


MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 512
MAX_SECTIONS = 4_096
MAX_MARKDOWN_BYTES = 1024 * 1024
MAX_TOTAL_MARKDOWN_BYTES = 8 * 1024 * 1024
WORD = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
RESTRICTION = re.compile(r"ограничен|что нельзя|чего нельзя|не подходит|исключен|условия")
STOP_WORDS = frozenset(
    "а без будет бы был была были быть в во вообще вот все всего вы где да для до его ее если есть же за "
    "и из или им их к как какая какие какой какое когда кого кто куда ли можно мы на над "
    "надо нам нас не него нет ни но ну нужно о об один он она они от по под пока потом при про с "
    "сколько со сначала так там то уже у хотим хочу чем чего что чтобы это я".split()
)


class KnowledgeBaseError(ValueError):
    """The local archive could not be loaded as a knowledge base."""


@dataclass(frozen=True)
class Evidence:
    id: str
    path: str
    heading: str
    text: str


def _words(text: str) -> frozenset[str]:
    normalized = text.casefold().replace("ё", "е")
    return frozenset(
        word.replace(",", ".")
        for word in WORD.findall(normalized)
        if word not in STOP_WORDS and (len(word) > 1 or word.isdecimal())
    )


@dataclass(frozen=True)
class _Terms:
    words: frozenset[str]
    short_prefixes: frozenset[str]
    long_prefixes: frozenset[str]

    @classmethod
    def from_text(cls, text: str) -> "_Terms":
        words = _words(text)
        return cls(
            words,
            frozenset(
                word[:length]
                for word in words if word.isalpha()
                for length in (4, 5) if len(word) >= length
            ),
            frozenset(word[:6] for word in words if len(word) >= 6 and word.isalpha()),
        )

    def match(self, word: str) -> float:
        if word in self.words:
            return 1.0
        if len(word) >= 6 and word[:6] in self.long_prefixes:
            return 0.75
        if len(word) == 4 and word in self.short_prefixes:
            return 0.65
        if len(word) > 4 and word[:4] in self.words:
            return 0.65
        if len(word) >= 5 and word[:5] in self.short_prefixes:
            return 0.4
        return 0.0


@dataclass(frozen=True)
class _Section:
    evidence: Evidence
    title: _Terms
    ancestors: _Terms
    body: _Terms
    restriction: bool


def _section(path: str, headings: list[tuple[int, str]], line: int, text: str) -> _Section:
    titles = [title for _, title in headings] or [PurePosixPath(path).stem]
    evidence = Evidence(f"{path}#L{line}", path, " / ".join(titles), text)
    return _Section(
        evidence,
        _Terms.from_text(titles[-1]),
        _Terms.from_text(" ".join(titles[:-1])),
        _Terms.from_text(text),
        any(RESTRICTION.search(title.casefold().replace("ё", "е")) for title in titles),
    )


def _sections(path: str, markdown: str) -> Iterator[_Section]:
    headings: list[tuple[int, str]] = []
    body = []
    start = 1
    fence = ""
    for line_number, line in enumerate(markdown.splitlines(), 1):
        marker = FENCE.match(line)
        if marker:
            token, suffix = marker.groups()
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = ""
            body.append(line)
            continue
        heading = None if fence else HEADING.match(line)
        if not heading:
            body.append(line)
            continue

        text = "\n".join(body).strip("\n")
        if text.strip():
            yield _section(path, headings, start, text)
        level = len(heading[1])
        while headings and headings[-1][0] >= level:
            headings.pop()
        title = re.sub(r"[ \t]+#+$", "", heading[2]).strip()
        headings.append((level, title))
        body = []
        start = line_number

    text = "\n".join(body).strip("\n")
    if text.strip():
        yield _section(path, headings, start, text)


def _member_path(member: ZipInfo) -> str:
    original = member.orig_filename
    path = original[:-1] if member.is_dir() else original
    if (
        original != member.filename
        or len(path) > 240
        or any(character in path for character in ("\\", ":", "\x00"))
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or (member.create_system == 3 and S_ISLNK(member.external_attr >> 16))
    ):
        raise KnowledgeBaseError("Архив базы знаний содержит недопустимый путь или тип файла.")
    return path


class KnowledgeBase:
    def __init__(self, sections: tuple[_Section, ...]):
        self._sections = sections
        self._document_frequency: Counter[str] = Counter()
        for section in sections:
            terms = (section.title, section.ancestors, section.body)
            vocabulary = set().union(*(part.words | part.short_prefixes for part in terms))
            self._document_frequency.update(vocabulary)

    @classmethod
    def from_zip(cls, path: Path) -> "KnowledgeBase":
        sections = []
        seen_paths = set()
        total_size = 0
        try:
            if path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise KnowledgeBaseError("Архив базы знаний превышает допустимый размер.")
            with ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > MAX_MEMBERS:
                    raise KnowledgeBaseError("В архиве базы знаний слишком много файлов.")
                for member in sorted(members, key=lambda item: item.filename):
                    name = _member_path(member)
                    if member.is_dir() or PurePosixPath(name).suffix.casefold() != ".md":
                        continue
                    if name.casefold() in seen_paths:
                        raise KnowledgeBaseError("В архиве базы знаний повторяются имена документов.")
                    seen_paths.add(name.casefold())

                    read_limit = min(MAX_MARKDOWN_BYTES, MAX_TOTAL_MARKDOWN_BYTES - total_size)
                    if member.file_size > read_limit:
                        raise KnowledgeBaseError("Документы базы знаний превышают допустимый размер.")
                    with archive.open(member) as stream:
                        data = stream.read(read_limit + 1)
                    if len(data) > read_limit:
                        raise KnowledgeBaseError("Документы базы знаний превышают допустимый размер.")
                    total_size += len(data)
                    markdown = data.decode("utf-8-sig")
                    if "\x00" in markdown:
                        raise KnowledgeBaseError("В документах базы знаний есть недопустимые символы.")
                    for section in _sections(name, markdown):
                        if len(sections) == MAX_SECTIONS:
                            raise KnowledgeBaseError("В базе знаний слишком много разделов.")
                        sections.append(section)
        except (BadZipFile, DecompressionError, EOFError, OSError, RuntimeError, UnicodeError) as exc:
            raise KnowledgeBaseError("Не удалось прочитать архив базы знаний в UTF-8.") from exc
        if not sections:
            raise KnowledgeBaseError("В архиве базы знаний нет содержательных Markdown-документов.")
        return cls(tuple(sections))

    def search(self, query: str, *, limit: int = 5) -> tuple[Evidence, ...]:
        words = sorted(_words(query))
        if not words or limit <= 0:
            return ()

        weights = {
            word: 1.0 if word[0].isdigit() else 1 + log((1 + len(self._sections)) / (1 + max(
                self._document_frequency[word],
                self._document_frequency[word[:5]],
            )))
            for word in words
        }
        ranked = []
        for section in self._sections:
            direct = 0.0
            context = 0.0
            for word, weight in weights.items():
                direct += weight * (2.0 * section.title.match(word) + section.body.match(word))
                context += weight * section.ancestors.match(word)
            score = direct + 0.8 * context
            if score:
                ranked.append((score, direct, section))
        ranked.sort(key=lambda entry: (-entry[0], entry[2].evidence.id))
        selected = []
        overflow = []
        source_counts = Counter()
        for _, _, section in ranked:
            path = section.evidence.path
            if source_counts[path] == 2:
                overflow.append(section)
                continue
            selected.append(section)
            source_counts[path] += 1
            if len(selected) == limit:
                break
        selected.extend(overflow[:limit - len(selected)])

        # Prefer the leading source's limitation over a duplicate from that source.
        if len(selected) > 1:
            main_path = selected[0].evidence.path
            main_slots = [index for index, section in enumerate(selected) if section.evidence.path == main_path]
            if len(main_slots) > 1 and not any(selected[index].restriction for index in main_slots):
                for _, direct, section in ranked:
                    if direct and section.restriction and section.evidence.path == main_path:
                        selected[main_slots[-1]] = section
                        break
        return tuple(section.evidence for section in selected)
