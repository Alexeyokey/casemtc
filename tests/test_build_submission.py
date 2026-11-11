import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from scripts.build_submission import BuildError, RUNTIME_FILES, build_submission


class BuildSubmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in RUNTIME_FILES:
            content = "httpx==0.28.1\n" if name == "requirements.txt" else "pass\n"
            (self.root / name).write_text(content, encoding="utf-8")

    def test_archive_contains_only_allowed_files_at_root_and_is_reproducible(self):
        for name in (".env", "knowledge-base.zip", "questions.csv", "draft.py"):
            (self.root / name).write_text("fixture", encoding="utf-8")
        first = build_submission(self.root, self.root / "first.zip")
        for name in RUNTIME_FILES:
            os.utime(self.root / name, (1_600_000_000, 1_600_000_000))
        second = build_submission(self.root, self.root / "second.zip")

        self.assertEqual(first.read_bytes(), second.read_bytes())
        with ZipFile(first) as archive:
            self.assertEqual(tuple(archive.namelist()), RUNTIME_FILES)
            self.assertIsNone(archive.testzip())
            for name in RUNTIME_FILES:
                self.assertEqual(archive.read(name), (self.root / name).read_bytes())

    def test_build_rejects_symlink_inputs_and_keeps_previous_archive(self):
        output = build_submission(self.root, self.root / "submission.zip")
        previous = output.read_bytes()
        required = self.root / "knowledge_base.py"
        required.unlink()
        required.symlink_to(self.root / "consultation.py")
        before = set(self.root.iterdir())

        with self.assertRaisesRegex(BuildError, "knowledge_base.py"):
            build_submission(self.root, output)

        self.assertEqual(output.read_bytes(), previous)
        self.assertEqual(set(self.root.iterdir()), before)

    def test_build_rejects_unpinned_or_extra_requirements(self):
        for pins in ("httpx\n", "httpx==0.28.1\nother==1.0\n", "httpx==0.28.1\nhttpx==0.28.1\n"):
            with self.subTest(pins=pins):
                (self.root / "requirements.txt").write_text(pins, encoding="utf-8")
                output = self.root / "submission.zip"
                with self.assertRaisesRegex(BuildError, "requirements.txt"):
                    build_submission(self.root, output)
                self.assertFalse(output.exists())

    def test_build_rejects_invalid_python(self):
        (self.root / "solution.py").write_text("def test(\n", encoding="utf-8")
        with self.assertRaisesRegex(BuildError, "solution.py"):
            build_submission(self.root, self.root / "submission.zip")

    def test_archive_size_limit_does_not_replace_previous_output(self):
        output = build_submission(self.root, self.root / "submission.zip")
        previous = output.read_bytes()
        with patch("scripts.build_submission.MAX_ZIP_BYTES", 128):
            with self.assertRaisesRegex(BuildError, "Архив превышает лимит"):
                build_submission(self.root, output)
        self.assertEqual(output.read_bytes(), previous)
