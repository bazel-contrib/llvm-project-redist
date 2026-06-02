#!/usr/bin/env python3
"""Unit tests for validate_patches.py."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.validate_patches import validate, validate_version, validate_version_txt


class ValidateVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.versions_dir = Path(self.tmpdir.name) / "versions"
        self.versions_dir.mkdir()
        self.version_dir = self.versions_dir / "20.0.0"
        self.version_dir.mkdir()
        self.patches_dir = self.version_dir / "patches"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _add_presubmit(self) -> None:
        (self.version_dir / "presubmit.yml").write_text("tasks: {}\n")

    def _add_version_txt(self, content: str | None = None) -> None:
        if content is None:
            content = self.version_dir.name
        (self.version_dir / "version.txt").write_text(content + "\n")

    def _add_patch(self, name: str) -> None:
        self.patches_dir.mkdir(exist_ok=True)
        (self.patches_dir / name).touch()

    def test_empty_dir_is_valid(self) -> None:
        self.assertEqual(validate_version(self.version_dir), [])

    def test_gitkeep_only_is_valid(self) -> None:
        (self.version_dir / ".gitkeep").touch()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_single_patch_starting_at_001(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("001_fix_build.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_sequential_patches(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("001_first.patch")
        self._add_patch("002_second.patch")
        self._add_patch("003_third.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_gap_in_sequence(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("001_first.patch")
        self._add_patch("003_third.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected patch 002", errors[0])

    def test_not_starting_at_001(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("002_second.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected patch 001", errors[0])

    def test_bad_naming_no_prefix(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("fix.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match", errors[0])

    def test_bad_naming_two_digit_prefix(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("01_fix.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match", errors[0])

    def test_bad_naming_no_underscore(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("001fix.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match", errors[0])

    def test_non_patch_files_in_patches_dir_ignored(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self.patches_dir.mkdir(exist_ok=True)
        (self.patches_dir / "README.md").touch()
        self._add_patch("001_fix.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_duplicate_numbers(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("001_alpha.patch")
        self._add_patch("001_bravo.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected patch 002", errors[0])

    def test_missing_presubmit_with_patches(self) -> None:
        self._add_version_txt()
        self._add_patch("001_fix.patch")
        errors = validate_version(self.version_dir)
        self.assertTrue(any("missing required presubmit.yml" in e for e in errors))

    def test_presubmit_and_version_txt_is_valid(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_no_patches_dir_is_valid(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_empty_patches_dir_is_valid(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self.patches_dir.mkdir()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_dash_separated_name_is_valid(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("001-fix-build.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_error_includes_patches_subpath(self) -> None:
        self._add_presubmit()
        self._add_version_txt()
        self._add_patch("fix.patch")
        errors = validate_version(self.version_dir)
        self.assertIn("patches/fix.patch", errors[0])


class ValidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.versions_dir = Path(self.tmpdir.name) / "versions"
        self.versions_dir.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_empty_versions_dir(self) -> None:
        self.assertEqual(validate(self.versions_dir), [])

    def test_nonexistent_dir(self) -> None:
        absent = Path(self.tmpdir.name) / "nope"
        self.assertEqual(validate(absent), [])

    def _make_valid_version(self, name: str) -> Path:
        v = self.versions_dir / name
        v.mkdir()
        (v / "presubmit.yml").write_text("tasks: {}\n")
        (v / "version.txt").write_text(f"{name}\n")
        return v

    def test_valid_multiple_versions(self) -> None:
        v1 = self._make_valid_version("20.0.0")
        (v1 / "patches").mkdir()
        (v1 / "patches" / "001_fix.patch").touch()

        v2 = self._make_valid_version("21.0.0")
        (v2 / "patches").mkdir()
        (v2 / "patches" / "001_a.patch").touch()
        (v2 / "patches" / "002_b.patch").touch()

        self.assertEqual(validate(self.versions_dir), [])

    def test_errors_across_versions(self) -> None:
        v1 = self._make_valid_version("20.0.0")
        (v1 / "patches").mkdir()
        (v1 / "patches" / "002_bad_start.patch").touch()

        v2 = self._make_valid_version("21.0.0")
        (v2 / "patches").mkdir()
        (v2 / "patches" / "bad.patch").touch()

        errors = validate(self.versions_dir)
        self.assertEqual(len(errors), 2)

    def test_ignores_files_in_versions_root(self) -> None:
        (self.versions_dir / ".gitkeep").touch()
        self.assertEqual(validate(self.versions_dir), [])

    def test_missing_presubmit_across_versions(self) -> None:
        v1 = self.versions_dir / "20.0.0"
        v1.mkdir()
        (v1 / "version.txt").write_text("20.0.0\n")
        (v1 / "patches").mkdir()
        (v1 / "patches" / "001_fix.patch").touch()

        v2 = self._make_valid_version("21.0.0")
        (v2 / "patches").mkdir()
        (v2 / "patches" / "001_a.patch").touch()

        errors = validate(self.versions_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("20.0.0", errors[0])
        self.assertIn("missing required presubmit.yml", errors[0])


class ValidateVersionTxtTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        # Create versions/{name}/ structure so _check_version_increment
        # can compute the repo root via version_dir.parent.parent.
        self.versions_dir = Path(self.tmpdir.name) / "versions"
        self.versions_dir.mkdir()
        self.version_dir = self.versions_dir / "20.0.0"
        self.version_dir.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    @patch("tools.validate_patches.subprocess.run")
    def test_base_version_matches_dir(self, mock_run: unittest.mock.MagicMock) -> None:
        mock_run.return_value = unittest.mock.Mock(returncode=1)
        (self.version_dir / "version.txt").write_text("20.0.0\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_bcr_version(self, mock_run: unittest.mock.MagicMock) -> None:
        mock_run.return_value = unittest.mock.Mock(returncode=1)
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.1\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_bcr_multi_digit(self, mock_run: unittest.mock.MagicMock) -> None:
        mock_run.return_value = unittest.mock.Mock(returncode=1)
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.12\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    def test_wrong_base_version(self) -> None:
        (self.version_dir / "version.txt").write_text("21.0.0\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("must be '20.0.0' or '20.0.0.bcr.N'", errors[0])

    def test_wrong_bcr_prefix(self) -> None:
        (self.version_dir / "version.txt").write_text("21.0.0.bcr.1\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)

    def test_arbitrary_suffix(self) -> None:
        (self.version_dir / "version.txt").write_text("20.0.0.preview\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)

    def test_empty_content(self) -> None:
        (self.version_dir / "version.txt").write_text("\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("empty", errors[0])

    def test_missing_file_non_empty_dir(self) -> None:
        (self.version_dir / "presubmit.yml").write_text("tasks: {}\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing required version.txt", errors[0])

    def test_missing_file_empty_dir(self) -> None:
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_whitespace_stripped(self, mock_run: unittest.mock.MagicMock) -> None:
        mock_run.return_value = unittest.mock.Mock(returncode=1)
        (self.version_dir / "version.txt").write_text("  20.0.0  \n")
        self.assertEqual(validate_version_txt(self.version_dir), [])


class CheckVersionIncrementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.versions_dir = Path(self.tmpdir.name) / "versions"
        self.versions_dir.mkdir()
        self.version_dir = self.versions_dir / "20.0.0"
        self.version_dir.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _git_show_returns(self, mock_run: unittest.mock.MagicMock, content: str) -> None:
        mock_run.return_value = unittest.mock.Mock(returncode=0, stdout=content + "\n")

    def _git_show_fails(self, mock_run: unittest.mock.MagicMock) -> None:
        mock_run.return_value = unittest.mock.Mock(returncode=1, stdout="")

    @patch("tools.validate_patches.subprocess.run")
    def test_new_file_skipped(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_fails(mock_run)
        (self.version_dir / "version.txt").write_text("20.0.0\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_unchanged_version_no_error(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0")
        (self.version_dir / "version.txt").write_text("20.0.0\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_base_to_bcr1(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0")
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.1\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_bcr1_to_bcr2(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0.bcr.1")
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.2\n")
        self.assertEqual(validate_version_txt(self.version_dir), [])

    @patch("tools.validate_patches.subprocess.run")
    def test_skip_by_two_errors(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0.bcr.1")
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.3\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("increment by exactly 1", errors[0])

    @patch("tools.validate_patches.subprocess.run")
    def test_base_to_bcr2_errors(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0")
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.2\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("increment by exactly 1", errors[0])

    @patch("tools.validate_patches.subprocess.run")
    def test_decrement_errors(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0.bcr.3")
        (self.version_dir / "version.txt").write_text("20.0.0.bcr.2\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("increment by exactly 1", errors[0])

    @patch("tools.validate_patches.subprocess.run")
    def test_bcr_to_base_errors(self, mock_run: unittest.mock.MagicMock) -> None:
        self._git_show_returns(mock_run, "20.0.0.bcr.1")
        (self.version_dir / "version.txt").write_text("20.0.0\n")
        errors = validate_version_txt(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("increment by exactly 1", errors[0])


if __name__ == "__main__":
    unittest.main()
