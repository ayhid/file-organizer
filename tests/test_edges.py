"""Edge cases on safety-critical branches (rollback, races, misconfiguration)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import OTF, WorkspaceCase, font_bytes, load, make_zip, snapshot, write  # noqa: I001
import fonts  # noqa: E402


class InstallRollback(WorkspaceCase, unittest.TestCase):
    def test_fonts_installed_before_a_failure_are_removed(self):
        ws = self.ws
        ws.file("Pack/A_Good.otf", font_bytes(OTF, "a"))
        ws.file("Pack/B_Good.otf", font_bytes(OTF, "b"))
        ws.file("Pack/Z_Bad.otf", b"corrupted")
        write(ws.fonts / "B_Good.otf", font_bytes(OTF, "b"))  # already installed: must survive rollback
        ws.pipeline("rename")
        self.assertEqual(ws.plan("Pack")["action"], "install")
        out = ws.ok("apply", "--apply")
        self.assertIn("install failed, nothing trashed", out)
        self.assertFalse((ws.fonts / "A_Good.otf").exists(), "installed by this row: rolled back")
        self.assertTrue((ws.fonts / "B_Good.otf").exists(), "pre-existing: kept")
        self.assertTrue((ws.src / "Pack/Z_Bad.otf").exists())
        statuses = sorted(r[0] for r in ws.q("SELECT status FROM ops WHERE op='install'"))
        self.assertEqual(statuses, ["error", "undone", "undone"])

    def test_folder_changed_since_plan(self):
        ws = self.ws
        ws.file("Pack/A.otf", font_bytes(OTF, "a"))
        ws.file("Pack/B.otf", font_bytes(OTF, "b"))
        ws.pipeline("rename")
        for f in ("A.otf", "B.otf"):
            (ws.src / "Pack" / f).unlink()
        ws.file("Pack/readme.txt", "r")
        out = ws.ok("apply", "--apply")
        self.assertIn("no installable font found", out)
        self.assertTrue((ws.src / "Pack/readme.txt").exists())

    def test_trash_folder_collision(self):
        d = Path(tempfile.mkdtemp())
        try:
            cfg = {"trash_dir": d / "Trash", "trash_method": "dir"}
            write(d / "Trash/Pack/old", b"old")
            write(d / "Pack/new", b"new")
            where = fonts.trash(str(d / "Pack"), cfg)
            self.assertNotEqual(Path(where).name, "Pack")
            self.assertEqual((Path(where) / "new").read_bytes(), b"new")
            self.assertEqual((d / "Trash/Pack/old").read_bytes(), b"old")
        finally:
            shutil.rmtree(d)


class DupesEdges(WorkspaceCase, unittest.TestCase):
    def test_zip_in_its_own_folder_is_kept(self):
        ws = self.ws
        ws.set_rules('[units]\ncollections = ["Docs"]\n')
        ws.file("Docs/a.txt", "A")
        make_zip(ws.src / "Docs/Docs.zip", {"a.txt": "A"})
        ws.pipeline("dupes")
        self.assertEqual(ws.q("SELECT COUNT(*) FROM dup_groups WHERE kind='zip'")[0][0], 0)

    def test_unproven_unit_duplicates_are_not_claimed(self):
        ws = self.ws
        ws.file("P1/a.txt", "same")
        ws.file("P2/a.txt", "same")
        ws.ok("inventory")
        ws.ok("units")
        h = load("hash")
        with mock.patch.object(h, "hash_file", side_effect=PermissionError("denied")):
            ws.ok("hash")
        ws.ok("dupes")
        self.assertEqual(ws.q("SELECT COUNT(*) FROM dup_groups WHERE kind='unit'")[0][0], 0)


class InventoryEdges(WorkspaceCase, unittest.TestCase):
    def test_file_vanishing_during_scan(self):
        ws = self.ws
        ws.file("a.txt", "a")
        ws.file("b.txt", "b")
        inv = load("inventory")
        real = os.lstat

        def flaky(p, *a, **k):
            if str(p).endswith("b.txt"):
                raise FileNotFoundError(2, "gone", str(p))
            return real(p, *a, **k)
        with mock.patch.object(inv.os, "lstat", flaky):
            out = ws.ok("inventory")
        self.assertIn("unreadable 1", out)
        self.assertEqual([r[0] for r in ws.q("SELECT path FROM files")], ["a.txt"])

    def test_ignore_paths_on_files(self):
        ws = self.ws
        ws.set_rules('[inventory]\nignore_paths = ["*.part"]\n')
        ws.file("movie.mp4.part", "partial download")
        ws.file("ok.txt", "ok")
        ws.ok("inventory")
        self.assertEqual([r[0] for r in ws.q("SELECT path FROM files")], ["ok.txt"])

    def test_many_files_batched(self):
        ws = self.ws
        for i in range(1203):
            ws.file(f"d/{i}.txt", str(i))
        self.assertIn("1203 files", ws.ok("inventory"))


class ApplyEdges(WorkspaceCase, unittest.TestCase):
    def test_missing_target_config(self):
        ws = self.ws
        ws.file("a.md", "a")
        ws.pipeline("rename")
        ap = load("apply")
        with mock.patch.object(ap.sys, "argv", ["apply.py", "--state", str(ws.state), "--local-rules", str(ws.local),
                                                "--source", str(ws.src), "--config", str(self._no_target_config())]):
            with self.assertRaises(SystemExit) as e:
                ap.main()
        self.assertIn("source and target must be set", str(e.exception.code))
        import common
        common.close_all()

    def _no_target_config(self):
        p = self.ws.tmp / "rules.toml"
        p.write_text("[paths]\n", encoding="utf-8")
        return p

    def test_target_inside_source_warns(self):
        ws = self.ws
        ws.file("a.md", "a")
        ws.pipeline("rename")
        code, out = ws.run("apply", "--target", str(ws.src / "Organized"))
        self.assertIn("target is inside the source", out)

    def test_undo_when_original_place_is_taken(self):
        ws = self.ws
        ws.file("a.md", "a")
        ws.pipeline("rename")
        ws.ok("apply", "--apply")
        ws.file("a.md", "someone recreated it")
        out = ws.ok("undo", "--last", "--yes")
        self.assertIn("1 failed", out)
        self.assertEqual((ws.src / "a.md").read_text(), "someone recreated it")
        self.assertEqual(len([k for k in snapshot(ws.dst) if k.endswith("a.md")]), 1)


class ReviewEdges(WorkspaceCase, unittest.TestCase):
    def test_import_ignores_applied_rows(self):
        ws = self.ws
        ws.file("a.md", "a")
        ws.pipeline("rename")
        ws.ok("review", "export")
        ws.ok("apply", "--apply")
        csv_path = ws.state.parent / "reports/plan.csv"
        csv_path.write_text(csv_path.read_text().replace("a.md,", "renamed.md,", 1).replace(",a.md,", ",zz.md,"))
        self.assertIn("0 rows updated", ws.ok("review", "import"))


if __name__ == "__main__":
    unittest.main()
