import stat
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from knowledge_base import KnowledgeBase, KnowledgeBaseError


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def archive(self, name, *members):
        path = self.directory / name
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as zip_file:
            for member, data in members:
                zip_file.writestr(member, data)
        return path

    def test_keeps_heading_path_and_table_conditions_in_one_evidence(self):
        table = (
            "  ----------------- -----------------------------\n"
            "  Прибор           Условие\n"
            "  ----------------- -----------------------------\n"
            "  Тип А            Проверка совместимости нужна\n"
            "                   для старых приборов\n\n"
            "  Тип Б            Поддержка после обновления\n"
            "  ----------------- -----------------------------"
        )
        metering = (
            "docs/Модели.MD",
            f"\ufeff# Учёт\n\n## Совместимость приборов\n\n{table}\n\nПроверка обязательна.",
        )
        other = ("faq.md", "# Общие сведения\n\nПриборы хранятся на складе.")
        ignored = ("ignored.txt", b"\xff")
        first = KnowledgeBase.from_zip(self.archive("first.zip", metering, other, ignored))
        second = KnowledgeBase.from_zip(self.archive("second.zip", ignored, other, metering))

        results = first.search("Совместимость приборов", limit=1)
        self.assertEqual(results, second.search("Совместимость приборов", limit=1))
        evidence, = results
        self.assertEqual(evidence.id, "docs/Модели.MD#L3")
        self.assertEqual(evidence.path, "docs/Модели.MD")
        self.assertEqual(evidence.heading, "Учёт / Совместимость приборов")
        self.assertEqual(evidence.text, f"{table}\n\nПроверка обязательна.")

    def test_returns_topic_and_relevant_restriction_with_inflected_query(self):
        path = self.archive(
            "coverage.zip",
            (
                "meters.md",
                "# Платформа учёта\n\n"
                "## Счётчики арендаторов\n\nПлатформа поддерживает учёт по арендаторам.\n\n"
                "## Счётчики по арендаторам\n\nДоступна группировка счётчиков.\n\n"
                "## Ограничения\n\nСовместимость счётчиков нужно подтвердить отдельно.\n",
            ),
            ("weather.md", "# Погода\n\nНаблюдения по осадкам."),
        )
        kb = KnowledgeBase.from_zip(path)
        results = kb.search("учет счетчиками арендаторами", limit=2)

        self.assertEqual([result.path for result in results], ["meters.md", "meters.md"])
        self.assertIn("арендаторов", results[0].heading)
        self.assertTrue(results[1].heading.endswith("Ограничения"))
        self.assertEqual(kb.search("экзопланета цирконий"), ())
        self.assertEqual(kb.search("А что и как?"), ())
        self.assertEqual(kb.search("учёт", limit=0), ())

    def test_does_not_treat_markdown_inside_code_fence_as_heading(self):
        path = self.archive(
            "fence.zip",
            ("doc.md", "# Раздел\n\n```markdown\n## Псевдозаголовок\n```\n"),
        )
        evidence, = KnowledgeBase.from_zip(path).search("псевдозаголовок")
        self.assertEqual(evidence.heading, "Раздел")
        self.assertIn("## Псевдозаголовок", evidence.text)

    def test_keeps_a_second_relevant_source_when_one_document_dominates(self):
        photos = "# Счётчики и фотографии\n\n" + "\n\n".join(
            f"## Фотографии счётчиков {index}\n\nРаспознавание фотографий счётчиков."
            for index in range(1, 5)
        )
        path = self.archive(
            "multiple-sources.zip",
            ("photos.md", photos),
            ("metering.md", "# Облачный учёт\n\nСчётчики арендаторов можно подключать после проверки."),
        )
        results = KnowledgeBase.from_zip(path).search("Счётчики арендаторов и фотографии", limit=3)
        self.assertEqual({result.path for result in results}, {"photos.md", "metering.md"})

    def test_rejects_unsafe_paths_symlinks_and_duplicates(self):
        symlink = ZipInfo("link.md")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for name, members in (
            ("parent", (("../nested.md", "text"),)),
            ("absolute", (("/nested.md", "text"),)),
            ("windows", ((r"C:\nested.md", "text"),)),
            ("symlink", ((symlink, "target.md"),)),
            ("duplicates", (("doc.md", "one"), ("DOC.md", "two"))),
        ):
            with self.subTest(name=name), self.assertRaises(KnowledgeBaseError):
                KnowledgeBase.from_zip(self.archive(f"{name}.zip", *members))

    def test_rejects_missing_broken_empty_and_invalid_utf8_archives(self):
        broken = self.directory / "broken.zip"
        broken.write_bytes(b"not a zip")
        empty = self.archive("empty.zip", ("text.txt", "ignored"), ("headings.md", "# Heading"))
        invalid = self.archive("invalid.zip", ("doc.md", b"# Heading\n\xff"))
        for path in (self.directory / "missing.zip", broken, empty, invalid):
            with self.subTest(path=path.name), self.assertRaises(KnowledgeBaseError) as caught:
                KnowledgeBase.from_zip(path)
            self.assertNotIn(str(self.directory), str(caught.exception))

    def test_enforces_archive_member_and_content_budgets(self):
        path = self.archive(
            "limits.zip", ("one.md", "# One\n12345"), ("two.md", "# Two\n67890")
        )
        for setting, limit in (
            ("MAX_ARCHIVE_BYTES", 10),
            ("MAX_MEMBERS", 1),
            ("MAX_MARKDOWN_BYTES", 6),
            ("MAX_TOTAL_MARKDOWN_BYTES", 15),
            ("MAX_SECTIONS", 1),
        ):
            with self.subTest(setting=setting), patch(f"knowledge_base.{setting}", limit):
                with self.assertRaises(KnowledgeBaseError):
                    KnowledgeBase.from_zip(path)
