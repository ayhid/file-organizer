"""fonts, apply, undo: unit tests + round-trip properties (nothing lost, nothing overwritten, undo restores)."""
from __future__ import annotations

import collections
import os
import string
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import support  # noqa: E402
from support import OTF, TTF, Workspace, WorkspaceCase, font_bytes, load, snapshot, write  # noqa: I001
import fonts  # noqa: E402

FAST = settings(max_examples=support.EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --------------------------------------------------------------------------- fonts module

class Fonts(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.inst = self.d / "Fonts"
        self.cfg = {"install_dir": self.inst, "trash_dir": self.d / "Trash", "trash_method": "dir",
                    "installable": {".ttf", ".otf", ".ttc", ".otc", ".dfont"}}

    def test_magic(self):
        for header in (OTF, TTF, b"true", b"ttcf", b"typ1"):
            self.assertTrue(fonts.looks_like_font(str(write(self.d / "f.otf", header + b"x"))))
        self.assertFalse(fonts.looks_like_font(str(write(self.d / "g.otf", b"<html>"))))
        self.assertFalse(fonts.looks_like_font(str(write(self.d / "e.ttf", b""))))
        self.assertTrue(fonts.looks_like_font(str(write(self.d / "s.dfont", b"\x00\x00\x01\x00"))))
        self.assertFalse(fonts.looks_like_font(str(write(self.d / "z.dfont", b""))))

    def test_fonts_in(self):
        write(self.d / "pack/a.otf", OTF)
        write(self.d / "pack/sub/b.TTF", TTF)
        write(self.d / "pack/web.woff2", b"w")
        write(self.d / "pack/.hidden.otf", OTF)
        write(self.d / "pack/__MACOSX/._a.otf", b"junk")
        write(self.d / "pack/.git/x.otf", OTF)
        found = [os.path.relpath(p, self.d / "pack") for p in fonts.fonts_in(str(self.d / "pack"), self.cfg["installable"])]
        self.assertEqual(found, ["a.otf", "sub/b.TTF"])
        self.assertEqual(fonts.fonts_in(str(self.d / "pack/web.woff2"), self.cfg["installable"]), [])

    def test_install_paths(self):
        f = write(self.d / "A.otf", font_bytes(OTF, "a"))
        dst, already = fonts.install(str(f), self.inst)
        self.assertEqual((Path(dst).read_bytes(), already), (f.read_bytes(), False))
        self.assertEqual(fonts.install(str(f), self.inst)[1], True)  # idempotent
        other = write(self.d / "x/A.otf", font_bytes(OTF, "different"))
        with self.assertRaises(fonts.FontError):
            fonts.install(str(other), self.inst)
        self.assertEqual(Path(dst).read_bytes(), f.read_bytes(), "never overwrite a different font")
        with self.assertRaises(fonts.FontError):
            fonts.install(str(write(self.d / "bad.otf", b"nope")), self.inst)
        self.assertFalse((self.inst / "bad.otf").exists())
        self.assertFalse(any(p.name.endswith(".fo-tmp") for p in self.inst.iterdir()))

    def test_install_verification_failure_cleans_up(self):
        f = write(self.d / "V.otf", font_bytes(OTF, "v"))
        real = fonts.digest
        with mock.patch.object(fonts, "digest", side_effect=lambda p: "bad" if p.startswith(str(self.inst)) else real(p)):
            with self.assertRaises(fonts.FontError):
                fonts.install(str(f), self.inst)
        self.assertFalse((self.inst / "V.otf").exists())

    def test_uninstall_guard(self):
        p = write(self.inst / "U.otf", font_bytes(OTF, "u"))
        fonts.uninstall(str(p), "not-its-hash")
        self.assertTrue(p.exists())
        fonts.uninstall(str(p), fonts.digest(str(p)))
        self.assertFalse(p.exists())
        fonts.uninstall(str(p))  # missing: no error

    def test_trash_names_never_collide(self):
        a = write(self.d / "one/F.otf", b"1")
        b = write(self.d / "two/F.otf", b"2")
        c = write(self.d / "pack/x", b"3")
        ta, tb = fonts.trash(str(a), self.cfg), fonts.trash(str(b), self.cfg)
        self.assertNotEqual(ta, tb)
        self.assertEqual((Path(ta).read_bytes(), Path(tb).read_bytes()), (b"1", b"2"))
        tc = fonts.trash(str(self.d / "pack"), self.cfg)
        self.assertEqual((Path(tc) / "x").read_bytes(), b"3")
        self.assertFalse(c.exists())

    def test_trash_fallbacks(self):
        f = write(self.d / "T.otf", b"t")

        def fail(*a, **k):
            raise PermissionError("Operation not permitted")
        cfg = {**self.cfg, "trash_method": "auto"}
        with mock.patch.object(fonts, "move_path", fail), mock.patch.object(fonts.sys, "platform", "linux"):
            with self.assertRaises(OSError):
                fonts.trash(str(f), cfg)
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(fonts, "move_path", fail), mock.patch.object(fonts.sys, "platform", "darwin"), \
                mock.patch.object(fonts.subprocess, "run", return_value=ok) as run:
            self.assertEqual(fonts.trash(str(f), cfg), "finder:" + str(f))
            self.assertIn("Finder", run.call_args[0][0][2])
        with mock.patch.object(fonts.sys, "platform", "darwin"), \
                mock.patch.object(fonts.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "no")):
            with self.assertRaises(OSError):
                fonts.trash(str(f), {**self.cfg, "trash_method": "finder"})

    def test_settings_defaults(self):
        s = fonts.settings({})
        self.assertTrue(str(s["install_dir"]).endswith("Library/Fonts"))
        self.assertIn(".otf", s["installable"])
        self.assertEqual(fonts.settings({"fonts": {"installable": ["TTF"]}})["installable"], {".ttf"})


# --------------------------------------------------------------------------- apply / undo

class Apply(WorkspaceCase, unittest.TestCase):
    def build(self):
        ws = self.ws
        ws.file("Facture 2025-03-14.pdf", "inv")
        ws.file("a.txt", "dup")
        ws.file("a (1).txt", "dup")
        ws.file("1234.png", "meaningless -> review")
        ws.file("proj/package.json", "{}")
        ws.file("Fam/A.otf", font_bytes(OTF, "fa"))
        ws.file("Fam/B.otf", font_bytes(OTF, "fb"))
        ws.file("Fam/OFL.txt", "licence")
        ws.file("Loose.ttf", font_bytes(TTF, "loose"))
        ws.pipeline("rename")

    def test_dry_run_changes_nothing(self):
        self.build()
        before = snapshot(self.ws.src)
        out = self.ws.ok("apply")
        self.assertIn("DRY RUN", out)
        self.assertIn("held for review", out)
        self.assertIn("install", out)
        self.assertEqual(snapshot(self.ws.src), before)
        self.assertEqual(snapshot(self.ws.dst), {})
        self.assertEqual(self.ws.q("SELECT COUNT(*) FROM ops")[0][0], 0)

    def test_apply_and_undo(self):
        self.build()
        ws = self.ws
        before = snapshot(ws.src)
        out = ws.ok("apply", "--apply")
        self.assertTrue((ws.dst / "admin/invoices/2025/facture_2025-03.pdf").exists(), out)
        self.assertTrue((ws.dst / "_to_delete/a (1).txt").exists())
        self.assertTrue((ws.src / "1234.png").exists(), "review rows are held")
        self.assertTrue((ws.fonts / "A.otf").exists() and (ws.fonts / "Loose.ttf").exists())
        self.assertTrue((ws.trash / "Fam/OFL.txt").exists())
        self.assertEqual(ws.q("SELECT COUNT(*) FROM plan WHERE status='done'")[0][0], 6)
        self.assertEqual(ws.q("SELECT COUNT(*) FROM files WHERE missing=1")[0][0], 4)
        self.assertIn("0 operations", ws.ok("apply"), "a second apply finds nothing left to do")
        ws.ok("undo", "--last", "--yes")
        self.assertEqual(snapshot(ws.src), before)
        self.assertEqual(snapshot(ws.fonts), {})
        self.assertEqual(snapshot(ws.trash), {})
        self.assertEqual({k for k in snapshot(ws.dst)}, set())
        self.assertEqual(ws.q("SELECT COUNT(*) FROM plan WHERE status='pending'")[0][0],
                         ws.q("SELECT COUNT(*) FROM plan")[0][0])
        self.assertEqual(ws.q("SELECT COUNT(*) FROM files WHERE missing=1")[0][0], 0)

    def test_filters(self):
        self.build()
        ws = self.ws
        ws.ok("apply", "--apply", "--only", "delete")
        self.assertEqual({r[0] for r in ws.q("SELECT action FROM plan WHERE status='done'")}, {"delete"})
        ws.ok("apply", "--apply", "--limit", "1")
        self.assertEqual(ws.q("SELECT COUNT(*) FROM plan WHERE status='done'")[0][0], 2)
        ws.ok("apply", "--apply", "--include-review")
        self.assertFalse((ws.src / "1234.png").exists())

    def test_target_exists_is_an_error_not_an_overwrite(self):
        self.build()
        ws = self.ws
        p = ws.plan("a.txt")
        write(ws.dst / p["target_dir"] / p["new_name"], "someone else's file")
        out = ws.ok("apply", "--apply")
        self.assertIn("target exists", out)
        self.assertEqual((ws.dst / p["target_dir"] / p["new_name"]).read_text(), "someone else's file")
        self.assertTrue((ws.src / "a.txt").exists())
        self.assertEqual(ws.plan("a.txt")["status"], "error")

    def test_source_missing(self):
        self.build()
        (self.ws.src / "a.txt").unlink()
        self.ws.ok("apply", "--apply")
        p = self.ws.plan("a.txt")
        self.assertEqual(p["status"], "error")
        self.assertIn("source missing", p["reason"])

    def test_font_rollback_and_conflict(self):
        ws = self.ws
        ws.file("Pack/Good.otf", font_bytes(OTF, "good"))
        ws.file("Pack/Bad.otf", b"corrupted")
        ws.file("Clash.otf", font_bytes(OTF, "mine"))
        write(ws.fonts / "Clash.otf", font_bytes(OTF, "theirs"))
        ws.pipeline("rename")
        out = ws.ok("apply", "--apply")
        self.assertIn("nothing trashed", out)
        self.assertFalse((ws.fonts / "Good.otf").exists(), "partial installs are rolled back")
        self.assertTrue((ws.src / "Pack/Good.otf").exists())
        self.assertTrue((ws.src / "Clash.otf").exists())
        self.assertEqual((ws.fonts / "Clash.otf").read_bytes(), font_bytes(OTF, "theirs"))
        self.assertEqual(snapshot(ws.trash), {})

    def test_trash_failure_keeps_fonts_and_undo_removes_them(self):
        ws = self.ws
        ws.file("Loose.ttf", font_bytes(TTF, "l"))
        ws.pipeline("rename")
        ap = load("apply")

        def fail(*a, **k):
            raise PermissionError("Trash not writable")
        with mock.patch.object(ap.fonts, "trash", fail):
            out = ws.ok("apply", "--apply")
        self.assertIn("could not be trashed", out)
        self.assertTrue((ws.fonts / "Loose.ttf").exists() and (ws.src / "Loose.ttf").exists())
        ws.ok("undo", "--last", "--yes")
        self.assertFalse((ws.fonts / "Loose.ttf").exists())

    def test_preexisting_font_survives_undo(self):
        ws = self.ws
        ws.file("Same.otf", font_bytes(OTF, "same"))
        write(ws.fonts / "Same.otf", font_bytes(OTF, "same"))
        ws.pipeline("rename")
        ws.ok("apply", "--apply")
        self.assertTrue((ws.trash / "Same.otf").exists())
        ws.ok("undo", "--last", "--yes")
        self.assertTrue((ws.fonts / "Same.otf").exists() and (ws.src / "Same.otf").exists())

    def test_cross_volume_copy_then_undo(self):
        self.build()
        ws = self.ws
        ap = load("apply")
        with mock.patch.object(ap, "move_path", return_value=("copied", "copied but source not removed")) as mv:
            def fake(src, dst):
                import shutil
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
                return "copied", "copied but source not removed"
            mv.side_effect = fake
            out = ws.ok("apply", "--apply", "--only", "move")
        self.assertIn("could not be removed", out)
        self.assertTrue((ws.src / "Facture 2025-03-14.pdf").exists())
        ws.ok("undo", "--last", "--yes")
        self.assertEqual([k for k in snapshot(ws.dst)], [])

    def test_prune_empty(self):
        ws = self.ws
        ws.file("Box/sub/only.md", "x")
        ws.set_rules('[units]\ncollections = ["Box", "Box/*"]\n')
        ws.pipeline("rename")
        ws.ok("apply", "--apply", "--prune-empty")
        self.assertFalse((ws.src / "Box").exists())


class Undo(WorkspaceCase, unittest.TestCase):
    def test_list_dry_run_and_finder_items(self):
        ws = self.ws
        ws.file("a.md", "a")
        ws.pipeline("rename")
        self.assertEqual(ws.ok("undo").strip(), "")
        ws.ok("apply", "--apply")
        self.assertIn("live=1", ws.ok("undo", "--list"))
        self.assertIn("DRY RUN: 1 operations", ws.ok("undo", "--last"))
        self.assertFalse((ws.src / "a.md").exists())
        ws.exec("UPDATE ops SET op='trash', dst='finder:/x'")
        out = ws.ok("undo", "--last", "--yes")
        self.assertIn("Put Back", out)
        self.assertIn("0 operations reversed, 1 failed", out)

    def test_undo_twice_is_harmless(self):
        ws = self.ws
        ws.file("a.md", "a")
        ws.pipeline("rename")
        ws.ok("apply", "--apply")
        ws.ok("undo", "--last", "--yes")
        out = ws.ok("undo", "--last", "--yes")
        self.assertIn("0 operations reversed, 0 failed", out)
        self.assertEqual(snapshot(ws.src), {"a.md": b"a"})


# --------------------------------------------------------------------------- round-trip properties

CONTENTS = [b"alpha", b"beta", b"gamma" * 30000, font_bytes(OTF, "f1"), font_bytes(TTF, "f2"), b"broken-font"]
NAMES = ["Facture 2024-02-03.pdf", "notes.md", "photo.jpg", "x.otf", "y.ttf", "data.csv", "123.png",
         "Rapport (1).pdf", "été à Paris.txt", "package.json", "w.woff2", "z.unknown"]
DIRS = ["", "Docs/", "Proj/", "Fam/", "Docs/Mail/", "Deep/a/b/"]


class RoundTrip(unittest.TestCase):
    @FAST
    @given(st.lists(st.tuples(st.sampled_from(DIRS), st.sampled_from(NAMES), st.sampled_from(CONTENTS)),
                    min_size=1, max_size=14),
           st.lists(st.sampled_from(NAMES), max_size=2),
           st.booleans())
    def test_apply_then_undo_restores_everything(self, entries, preinstalled, include_review):
        ws = Workspace('[units]\ncollections = ["Docs", "Docs/*"]\n')
        try:
            for i, (folder, name, data) in enumerate(entries):
                rel = f"{folder}{i}-{name}" if i % 3 else f"{folder}{name}"
                if not (ws.src / rel).exists():
                    ws.file(rel, data)
            for name in preinstalled:  # pre-existing fonts and an occupied target
                write(ws.fonts / name, b"pre-existing " + name.encode())
            write(ws.dst / "documents/notes/notes.md", b"already organised")
            src0, fonts0, dst0 = snapshot(ws.src), snapshot(ws.fonts), snapshot(ws.dst)

            ws.pipeline("rename")
            ws.ok("apply", "--apply", *(["--include-review"] if include_review else []))

            # nothing lost: every original content still exists somewhere
            src1, dst1, trash1, fonts1 = snapshot(ws.src), snapshot(ws.dst), snapshot(ws.trash), snapshot(ws.fonts)
            pool = collections.Counter(list(src1.values()) + list(dst1.values()) + list(trash1.values()))
            for data, n in collections.Counter(src0.values()).items():
                self.assertGreaterEqual(pool[data], n, "a file disappeared")
            # nothing overwritten: pre-existing target and font files are untouched
            for rel, data in dst0.items():
                self.assertEqual(dst1[rel], data)
            for rel, data in fonts0.items():
                self.assertEqual(fonts1[rel], data)
            # every installed font is byte-identical to a trashed original
            for rel, data in fonts1.items():
                if rel not in fonts0:
                    self.assertIn(data, trash1.values())

            ws.ok("undo", "--last", "--yes")
            self.assertEqual(snapshot(ws.src), src0)
            self.assertEqual(snapshot(ws.fonts), fonts0)
            self.assertEqual(snapshot(ws.dst), dst0)
            self.assertEqual(snapshot(ws.trash), {})
        finally:
            ws.cleanup()

    @FAST
    @given(st.lists(st.tuples(st.sampled_from(DIRS), st.sampled_from(NAMES), st.sampled_from(CONTENTS)),
                    min_size=1, max_size=10))
    def test_pipeline_is_idempotent(self, entries):
        ws = Workspace('[units]\ncollections = ["Docs", "Docs/*"]\n')
        try:
            for i, (folder, name, data) in enumerate(entries):
                ws.file(f"{folder}{i}-{name}", data)
            ws.pipeline("rename")
            first = [tuple(r) for r in ws.q("SELECT item_type, src, action, target_dir, new_name, needs_review "
                                            "FROM plan ORDER BY src")]
            ws.pipeline("rename")
            again = [tuple(r) for r in ws.q("SELECT item_type, src, action, target_dir, new_name, needs_review "
                                            "FROM plan ORDER BY src")]
            self.assertEqual(first, again)
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
