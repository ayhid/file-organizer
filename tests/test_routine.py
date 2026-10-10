"""routine.sh (periodic plan) and macos/install-fonts-job.sh."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import OTF, REPO, font_bytes, snapshot, write


class Routine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fo-routine-"))
        self.src = self.tmp / "Downloads"
        self.home = self.src / ".file-organizer"
        write(self.home / "rules.local.toml",
              f'[fonts]\ninstall_dir = "{self.tmp / "Fonts"}"\ntrash_dir = "{self.tmp / "Trash"}"\n')
        write(self.src / "Facture 2025-03-14.pdf", "inv")
        write(self.src / "a.txt", "dup")
        write(self.src / "a (1).txt", "dup")
        write(self.src / "1234.png", "needs review")
        write(self.src / "Fam/A.otf", font_bytes(OTF, "a"))
        write(self.src / "Fam/B.otf", font_bytes(OTF, "b"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_routine(self, **env):
        e = {**os.environ, "FO_SOURCE": str(self.src), "FO_TARGET": str(self.tmp / "Organized"),
             "PYTHON": sys.executable, **{k: str(v) for k, v in env.items()}}
        return subprocess.run(["bash", str(REPO / "routine.sh")], capture_output=True, text=True, env=e)

    def test_full_run_plans_and_moves_nothing(self):
        before = {k: v for k, v in snapshot(self.src).items() if not k.startswith(".file-organizer")}
        p = self.run_routine()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip().endswith("ROUTINE: DONE"))
        after = {k: v for k, v in snapshot(self.src).items() if not k.startswith(".file-organizer")}
        self.assertEqual(after, before, "the routine never moves anything")
        self.assertFalse((self.tmp / "Organized").exists())
        for f in ("state.db", "reports/review.csv", "reports/plan.csv", "reports/profile.md", "reports/dupes.csv"):
            self.assertTrue((self.home / f).exists(), f)
        con = sqlite3.connect(self.home / "state.db")
        try:
            paths = [r[0] for r in con.execute("SELECT path FROM files")]
            self.assertFalse(any(p.startswith(".file-organizer") for p in paths), "own state folder is ignored")
            actions = dict(con.execute("SELECT src, action FROM plan"))
            self.assertEqual(actions["Fam"], "install")
            self.assertEqual(actions["a (1).txt"], "delete")
        finally:
            con.close()
        review = (self.home / "reports/review.csv").read_text()
        self.assertIn("1234.png", review)
        self.assertNotIn("a.txt", review)

    def test_time_budget_resumes(self):
        p = self.run_routine(MAX_SECONDS=1)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip().endswith("ROUTINE: RESUME"))
        p = self.run_routine()
        self.assertEqual(p.returncode, 0)

    def test_second_run_is_stable(self):
        self.run_routine()
        first = (self.home / "reports/plan.csv").read_text()
        p = self.run_routine()
        self.assertEqual(p.returncode, 0)
        self.assertEqual((self.home / "reports/plan.csv").read_text(), first)

    def test_errors_surface(self):
        (self.home / "rules.local.toml").write_text("this is [ not toml")
        p = self.run_routine()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ROUTINE: ERROR in inventory", p.stdout)


class Installer(unittest.TestCase):
    script = REPO / "macos" / "install-fonts-job.sh"

    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(self.script)]).returncode, 0)

    @unittest.skipIf(sys.platform == "darwin", "refusal is only observable off macOS")
    def test_refuses_outside_macos(self):
        p = subprocess.run(["bash", str(self.script)], capture_output=True, text=True,
                           env={**os.environ, "HOME": tempfile.mkdtemp()})
        self.assertEqual(p.returncode, 1)
        self.assertIn("for macOS", p.stdout)


if __name__ == "__main__":
    unittest.main()
