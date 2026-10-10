"""End-to-end test on a synthetic folder. Run: python3 -m unittest discover -s tests -v

Uses only fake names. Never touches your real state, rules, fonts or Trash: everything lives in a temp dir.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "fo"))
import common  # noqa: E402

OTF = b"OTTO"            # OpenType (CFF) header
TTF = b"\x00\x01\x00\x00"  # TrueType header


def font(header: bytes, body: str) -> bytes:
    return header + body.encode()


def write(path: Path, data: bytes | str = b"x", mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if mtime:
        os.utime(path, (mtime, mtime))


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fo-test-"))
        src, cls.src = cls.tmp / "Downloads", cls.tmp / "Downloads"
        cls.dst = cls.tmp / "Organized"
        cls.state = cls.tmp / "state" / "state.db"
        cls.fonts = cls.tmp / "Library" / "Fonts"
        cls.trash = cls.tmp / "Trash"
        cls.local = cls.tmp / "rules.local.toml"
        cls.local.write_text(f"""
[units]
collections = ["Library"]

[fonts]
install_dir = "{cls.fonts}"
trash_dir = "{cls.trash}"
trash_method = "dir"

[[clients]]
pattern = 'globex'
slug = "globex"
""")
        big = os.urandom(300_000)

        # a code project: identical files inside must NOT count as duplicates
        write(src / "my-app" / "package.json", '{"name":"x"}')
        write(src / "my-app" / "src" / "a.ts", "same")
        write(src / "my-app" / "src" / "b.ts", "same")
        write(src / "my-app" / ".git" / "HEAD", "ref")
        # a project nested in a container folder
        write(src / "Library" / "starter-kit" / "package.json", "{}")
        write(src / "Library" / "starter-kit" / "index.js", "x")
        write(src / "Library" / "loose-note.md", "note")
        # a font family folder (with a licence file that goes to the Trash with it)
        for w in ("Regular", "Bold", "Light"):
            write(src / "KO Sans" / f"KOSans-{w}.otf", font(OTF, w))
        write(src / "KO Sans" / "OFL.txt", "licence")
        # loose fonts
        write(src / "Loose Sans.ttf", font(TTF, "loose"))
        write(src / "Web Sans.woff2", b"wOF2web")                 # web font: moved, not installed
        write(src / "Broken.otf", b"not a font at all")           # invalid: nothing installed or trashed
        write(src / "Clash.otf", font(OTF, "mine"))               # a different Clash.otf is installed
        write(src / "Same.otf", font(OTF, "same"))                # identical font already installed
        write(cls.fonts / "Clash.otf", font(OTF, "theirs"))
        write(cls.fonts / "Same.otf", font(OTF, "same"))
        # an app bundle
        write(src / "Tool.app" / "Contents" / "Info.plist", "plist")
        # standalone duplicates: keeper should be the one inside a folder, without copy suffix
        write(src / "Documents" / "report.pdf", big, mtime=1_700_000_000)
        write(src / "report (1).pdf", big, mtime=1_700_000_100)
        write(src / "report copy.pdf", big, mtime=1_600_000_000)
        # duplicate folders
        for d in ("Pack", "Pack 2"):
            write(src / d / "one.png", "img1")
            write(src / d / "two.png", "img2")
        # zip already extracted
        write(src / "Script Bundle" / "a.js", "script-a")
        write(src / "Script Bundle" / "b.js", "script-b")
        with zipfile.ZipFile(src / "Script Bundle.zip", "w") as z:
            z.writestr("Script Bundle/a.js", "script-a")
            z.writestr("Script Bundle/b.js", "script-b")
        # zip NOT extracted
        with zipfile.ZipFile(src / "Other Bundle.zip", "w") as z:
            z.writestr("c.txt", "c")
        # naming / dating
        write(src / "Facture Globex 2025-03-14.pdf", "pdf")
        write(src / "WhatsApp Image 2025-12-04 at 18.20.59 (1).jpeg", "jpg")
        write(src / "1b60be85-4358-420f-af0f-acc07d2d731f.png", "png")
        write(src / "attestation sans date.pdf", "no date inside")
        write(src / "Globex specs.md", "spec")
        write(src / "Exercice maths 28_10_2025.pdf", "exercise")
        write(src / "ملاحظة عربية.txt", "arabic")
        write(src / ".ssh" / "id_ed25519", "secret")
        write(src / ".DS_Store", "junk")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fo(self, script, *args, check=True):
        cmd = [sys.executable, str(REPO / "fo" / f"{script}.py"), *args,
               "--source", str(self.src), "--target", str(self.dst), "--state", str(self.state),
               "--local-rules", str(self.local)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if check and p.returncode != 0:
            self.fail(f"{script} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        return p

    def db(self):
        con = sqlite3.connect(self.state)
        con.row_factory = sqlite3.Row
        return con

    def plan_for(self, src):
        return self.db().execute("SELECT * FROM plan WHERE src=?", (src,)).fetchone()

    def test_pipeline(self):
        for s in ("inventory", "units", "hash", "dupes", "profile", "classify", "rename"):
            self.fo(s)
        db = self.db()

        # inventory: .DS_Store ignored
        self.assertIsNone(db.execute("SELECT 1 FROM files WHERE name='.DS_Store'").fetchone())

        # units
        units = {r["root"]: r["kind"] for r in db.execute("SELECT root, kind FROM units")}
        self.assertIn(units.get("my-app"), ("node", "git"))
        self.assertEqual(units.get("Library/starter-kit"), "node")
        self.assertEqual(units.get("KO Sans"), "font")
        self.assertEqual(units.get("Tool.app"), "app")
        self.assertNotIn("Library", units)
        loose = db.execute("SELECT unit_id FROM files WHERE path='Library/loose-note.md'").fetchone()
        self.assertIsNone(loose["unit_id"])

        # duplicates: nothing inside a unit, keeper is Documents/report.pdf
        kinds = sorted(g["kind"] for g in db.execute("SELECT * FROM dup_groups"))
        self.assertEqual(kinds, ["file", "unit", "zip"], kinds)
        keep = db.execute("""SELECT f.path FROM dup_members m JOIN files f ON f.id=m.item_id
                             WHERE m.item_type='file' AND m.role='keep'""").fetchone()
        self.assertEqual(keep["path"], "Documents/report.pdf")
        self.assertEqual(self.plan_for("report (1).pdf")["action"], "delete")
        self.assertEqual(self.plan_for("Script Bundle.zip")["action"], "delete")
        self.assertEqual(self.plan_for("Other Bundle.zip")["action"], "move")
        removed_unit = db.execute("""SELECT u.root FROM dup_members m JOIN units u ON u.id=m.item_id
                                     WHERE m.item_type='unit' AND m.role='remove'""").fetchone()
        self.assertEqual(removed_unit["root"], "Pack 2")

        # classification + names
        p = self.plan_for("Facture Globex 2025-03-14.pdf")
        self.assertEqual((p["target_dir"], p["new_name"]), ("admin/invoices/2025", "facture-globex_2025-03.pdf"))
        p = self.plan_for("WhatsApp Image 2025-12-04 at 18.20.59 (1).jpeg")
        self.assertEqual((p["target_dir"], p["new_name"]), ("media/photos/2025", "whatsapp-image_2025-12-04.jpg"))
        p = self.plan_for("1b60be85-4358-420f-af0f-acc07d2d731f.png")
        self.assertEqual(p["needs_review"], 1)
        p = self.plan_for("attestation sans date.pdf")
        self.assertEqual(p["needs_review"], 1)
        self.assertIn("no date", p["reason"])
        p = self.plan_for("Globex specs.md")
        self.assertEqual((p["target_dir"], p["new_name"]), ("work/clients/globex", "globex-specs.md"))
        p = self.plan_for("Exercice maths 28_10_2025.pdf")
        self.assertEqual((p["target_dir"], p["new_name"]), ("documents", "exercice-maths_2025-10-28.pdf"))
        p = self.plan_for("my-app")
        self.assertEqual(p["target_dir"], "dev/projects")
        p = self.plan_for(".ssh")
        self.assertEqual((p["target_dir"], p["needs_review"]), ("_sensitive", 1))
        p = self.plan_for("ملاحظة عربية.txt")
        self.assertEqual(p["new_name"], "ملاحظة-عربية.txt")

        # fonts: installable -> install (original name kept), web font -> moved
        for name in ("KO Sans", "Loose Sans.ttf", "Broken.otf", "Clash.otf", "Same.otf"):
            p = self.plan_for(name)
            self.assertEqual((p["action"], p["new_name"]), ("install", name), name)
        p = self.plan_for("Web Sans.woff2")
        self.assertEqual((p["action"], p["target_dir"], p["new_name"]),
                         ("move", "design/fonts/_loose", "web-sans.woff2"))

        # review export/import locks an edited row
        self.fo("review", "export")
        csv_path = Path(self.state).parent / "reports" / "plan.csv"
        text = csv_path.read_text(encoding="utf-8").replace("globex-specs.md", "globex-specifications.md")
        csv_path.write_text(text, encoding="utf-8")
        self.fo("review", "import")
        p = self.plan_for("Globex specs.md")
        self.assertEqual((p["new_name"], p["locked"]), ("globex-specifications.md", 1))
        self.fo("classify")
        self.fo("rename")
        self.assertEqual(self.plan_for("Globex specs.md")["new_name"], "globex-specifications.md")

        # dry run moves nothing
        out = self.fo("apply").stdout
        self.assertIn("install", out)
        self.assertTrue((self.src / "report (1).pdf").exists())
        self.assertFalse((self.fonts / "Loose Sans.ttf").exists())

        # apply
        out = self.fo("apply", "--apply").stdout
        self.assertTrue((self.dst / "admin/invoices/2025/facture-globex_2025-03.pdf").exists(), out)
        self.assertTrue((self.dst / "_to_delete" / "report (1).pdf").exists())
        self.assertTrue((self.src / ".ssh").exists(), "review rows must not move")
        self.assertFalse((self.src / "my-app").exists())
        # fonts installed and verified, originals in the Trash
        for f in ("KOSans-Regular.otf", "KOSans-Bold.otf", "KOSans-Light.otf", "Loose Sans.ttf"):
            self.assertTrue((self.fonts / f).exists(), f)
        self.assertFalse((self.fonts / "OFL.txt").exists())
        self.assertTrue((self.trash / "KO Sans" / "OFL.txt").exists())
        self.assertTrue((self.trash / "Loose Sans.ttf").exists())
        self.assertFalse((self.src / "KO Sans").exists())
        self.assertTrue((self.dst / "design/fonts/_loose/web-sans.woff2").exists())
        # same font already installed: counts as success
        self.assertTrue((self.trash / "Same.otf").exists())
        # invalid font and name clash: nothing installed, nothing trashed
        self.assertTrue((self.src / "Broken.otf").exists())
        self.assertFalse((self.fonts / "Broken.otf").exists())
        self.assertTrue((self.src / "Clash.otf").exists())
        self.assertEqual((self.fonts / "Clash.otf").read_bytes(), font(OTF, "theirs"))
        self.assertEqual(self.plan_for("Broken.otf")["status"], "error")

        # undo restores everything, uninstalls only what the run installed
        self.fo("undo", "--last", "--yes")
        self.assertTrue((self.src / "report (1).pdf").exists())
        self.assertTrue((self.src / "my-app" / "src" / "a.ts").exists())
        self.assertFalse((self.dst / "admin/invoices/2025/facture-globex_2025-03.pdf").exists())
        self.assertTrue((self.src / "KO Sans" / "OFL.txt").exists())
        self.assertTrue((self.src / "Loose Sans.ttf").exists())
        self.assertFalse((self.fonts / "KOSans-Bold.otf").exists())
        self.assertFalse((self.fonts / "Loose Sans.ttf").exists())
        self.assertTrue((self.fonts / "Same.otf").exists(), "pre-existing font must stay installed")
        self.assertTrue((self.src / "Same.otf").exists())


class Helpers(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(common.find_date("invoice_20250403_1819")[:2], ("2025-04-03", "day"))
        self.assertEqual(common.find_date("2026-02 INFOS SITE")[:2], ("2026-02", "month"))
        self.assertEqual(common.find_date("ChatGPT Image Mar 11, 2026, 09_20_38 PM")[:2], ("2026-03-11", "day"))
        self.assertEqual(common.find_date("releve 14 février 2024")[:2], ("2024-02-14", "day"))
        self.assertEqual(common.find_date("relevé mars 2024")[:2], ("2024-03", "month"))
        self.assertEqual(common.find_date("scan 03/04/2025")[:2], ("2025-04-03", "day"))
        self.assertEqual(common.find_date("Document_numerise_23052025")[:2], ("2025-05-23", "day"))
        self.assertEqual(common.find_date("exercice_28_10_2025")[:2], ("2025-10-28", "day"))
        self.assertEqual(common.find_date("ChatGPT_Image_Feb_16__2026__08_15")[:2], ("2026-02-16", "day"))
        self.assertIsNone(common.find_date("pieces-jointes-60187392"))
        self.assertIsNone(common.find_date("2025X_269473"))
        self.assertIsNone(common.find_date("id 5236524644"))

    def test_slug(self):
        self.assertEqual(common.slugify("Cahier des charges V3 (Élise) & co"), "cahier-des-charges-v3-elise-and-co")
        self.assertEqual(common.strip_copy_suffix("photo (2)"), "photo")
        self.assertEqual(common.strip_copy_suffix("Palestine 2"), "Palestine")
        self.assertEqual(common.strip_copy_suffix("report copy"), "report")

    def test_merge(self):
        merged = common.merge_config({"a": [1], "t": {"x": 1, "y": 2}}, {"a": [0], "t": {"y": 3}})
        self.assertEqual(merged, {"a": [0, 1], "t": {"x": 1, "y": 3}})


if __name__ == "__main__":
    unittest.main()
