"""classify, rename, review: unit + property tests."""
from __future__ import annotations

import csv
import os
import re
import string
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import support  # noqa: E402
from support import Workspace, WorkspaceCase, load  # noqa: I001  (puts fo/ on sys.path)
import common  # noqa: E402

FAST = settings(max_examples=support.EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def rules(*rule_list, clients=(), meaningless=()):
    cl = load("classify")
    return cl.Rules({"classify": {"rules": [dict(r) for r in rule_list]},
                     "clients": [{"pattern": p, "slug": s} for p, s in clients],
                     "review": {"meaningless_names": list(meaningless)}})


def item(name, type_="file", path=None, family="document", kind="", ext=None, mtime=1_700_000_000, **kw):
    return {"type": type_, "id": 1, "name": name, "path": path or name, "family": family, "kind": kind,
            "ext": ext if ext is not None else common.ext_of(name), "mtime": mtime, "size": 1, **kw}


class NoPdf:
    def get(self, f):
        raise AssertionError("PDF must not be read here")


class FixedPdf:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def get(self, f):
        self.calls += 1
        return self.result


# --------------------------------------------------------------------------- classify: rule matching

class Matching(unittest.TestCase):
    def test_conditions(self):
        r = rules({"name": "u", "item": "unit", "kind": ["font"], "target": "fonts"},
                  {"name": "fam", "family": ["image"], "target": "img"},
                  {"name": "ext", "ext": [".HEIC"], "target": "heic"},
                  {"name": "re", "name_re": "releve", "target": "fin"},
                  {"name": "path", "path": ["Mail/*"], "target": "mail"},
                  {"name": "fallback", "target": "_review"})
        m = lambda it: r.match(it)[0]["name"]  # noqa: E731
        self.assertEqual(m(item("KO Sans", "unit", kind="font", family="font")), "u")
        self.assertEqual(m(item("KO Sans.otf", family="font")), "fallback")  # item=unit excludes files
        self.assertEqual(m(item("a.png", family="image")), "fam")
        self.assertEqual(m(item("a.heic", family="other")), "ext")
        self.assertEqual(m(item("Relevé mars.pdf")), "re")  # accents folded
        self.assertEqual(m(item("x.pdf", path="Mail/123/x.pdf")), "path")
        self.assertEqual(m(item("x.pdf")), "fallback")

    def test_first_match_wins(self):
        r = rules({"name": "first", "family": ["document"], "target": "a"},
                  {"name": "second", "family": ["document"], "target": "b"})
        self.assertEqual(r.match(item("x.pdf"))[0]["name"], "first")

    def test_client(self):
        r = rules({"name": "client", "client": True, "target": "work/{client}"},
                  {"name": "other", "target": "x"}, clients=[("acme|acmé", "acme")])
        rule, client = r.match(item("Specs ACME.md", path="Specs ACME.md"))
        self.assertEqual((rule["name"], client), ("client", "acme"))
        rule, client = r.match(item("Specs.md", path="Clients/Acmé/Specs.md"))
        self.assertEqual(client, "acme")
        self.assertEqual(r.match(item("other.md"))[0]["name"], "other")

    def test_no_rule(self):
        self.assertEqual(rules().match(item("x"))[0], None)

    def test_meaningless(self):
        r = rules(meaningless=[r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", r"^\d+$"])
        self.assertTrue(r.is_meaningless("1b60be85-4358-420f-af0f-acc07d2d731f"))
        self.assertTrue(r.is_meaningless("02"))
        self.assertFalse(r.is_meaningless("facture-acme"))


# --------------------------------------------------------------------------- classify: decisions

class Decide(unittest.TestCase):
    def setUp(self):
        self.cl = load("classify")

    def decide(self, it, rule_list, removed=None, pdf=None, keep_name_dates=True, **kw):
        return self.cl._decide(it, removed, rules(*rule_list, **kw), pdf or NoPdf(), 1995, keep_name_dates)

    def test_duplicate(self):
        row = self.decide(item("a (1).pdf", path="Docs/a (1).pdf"), [], removed="identical content")
        self.assertEqual((row["action"], row["target_dir"], row["rule"]), ("delete", "_to_delete/Docs", "duplicate"))
        row = self.decide(item("a (1).pdf"), [], removed="x")
        self.assertEqual(row["target_dir"], "_to_delete")

    def test_dated_from_name(self):
        row = self.decide(item("Facture 2025-03-14.pdf"),
                          [{"name": "inv", "target": "inv/{year}", "dated": True, "precision": "month"}])
        self.assertEqual((row["target_dir"], row["date_found"], row["date_source"], row["needs_review"]),
                         ("inv/2025", "2025-03-14", "name", 0))

    def test_dated_from_pdf(self):
        pdf = FixedPdf(("2024-11-02", "day", "pdf-text"))
        row = self.decide(item("Facture Acme.pdf"), [{"name": "inv", "target": "inv/{year}", "dated": True}], pdf=pdf)
        self.assertEqual((row["target_dir"], row["date_found"], row["date_source"]), ("inv/2024", "2024-11-02", "pdf-text"))
        self.assertEqual(pdf.calls, 1)

    def test_dated_without_date(self):
        row = self.decide(item("Facture.docx"), [{"name": "inv", "target": "inv/{year}", "dated": True}])
        self.assertEqual(row["needs_review"], 1)
        self.assertIn("no date found", row["reason"])
        self.assertEqual(row["target_dir"], "inv/2023")  # year falls back to mtime

    def test_name_date_kept_on_undated_rule(self):
        row = self.decide(item("scan 28_10_2025.pdf"), [{"name": "docs", "target": "documents"}])
        self.assertEqual((row["dated"], row["precision"], row["date_found"]), (1, "day", "2025-10-28"))
        row = self.decide(item("scan 28_10_2025.pdf"), [{"name": "docs", "target": "documents"}],
                          keep_name_dates=False)
        self.assertEqual(row["dated"], 0)

    def test_review_flags(self):
        row = self.decide(item("1b60be85-4358-420f-af0f-acc07d2d731f.png", family="image"),
                          [{"name": "img", "target": "img"}], meaningless=[r"^[0-9a-f]{8}-"])
        self.assertEqual((row["needs_review"], row["reason"]), (1, "meaningless name"))
        row = self.decide(item("x.bin"), [{"name": "mixed", "target": "_review", "review": True, "reason": "why"}])
        self.assertEqual((row["needs_review"], row["reason"]), (1, "why"))
        row = self.decide(item("x.bin"), [])
        self.assertEqual((row["rule"], row["target_dir"], row["needs_review"]), ("none", "_review", 1))

    def test_placeholders_and_clean_target(self):
        row = self.decide(item("Proj", "unit", kind="audio-project", family="audio-project"),
                          [{"name": "m", "target": "media//{kind}/"}])
        self.assertEqual(row["target_dir"], "media/audio-project")

    def test_install(self):
        r = [{"name": "fonts", "family": ["font"], "action": "install", "target": "design/fonts"}]
        row = self.decide(item("A.otf", family="font", installable=True), r)
        self.assertEqual((row["action"], row["target_dir"]), ("install", self.cl.INSTALL_TARGET))
        row = self.decide(item("A.woff2", family="font", installable=False), r)
        self.assertEqual((row["action"], row["target_dir"]), ("move", "design/fonts"))
        self.assertIn("no installable font", row["reason"])
        # a date in a font file name never turns it into a dated rename
        row = self.decide(item("Font 2024-01-02.otf", family="font", installable=True), r)
        self.assertEqual((row["action"], row["dated"]), ("install", 0))

    @given(st.text(max_size=30).map(lambda s: s.replace("/", "").replace("\x00", "")),
           st.sampled_from(["document", "image", "font", "other", "none"]),
           st.booleans(), st.booleans())
    def test_decide_invariants(self, name, family, dup, installable):
        cfg = common.load_config(common.DEFAULT_CONFIG, "/nonexistent")
        r = self.cl.Rules(cfg)
        it = item(name or "x", family=family, installable=installable)
        row = self.cl._decide(it, "dup" if dup else None, r, FixedPdf(None), 1995)
        self.assertIn(row["action"], ("move", "delete", "install"))
        td = row["target_dir"]
        self.assertFalse(td.startswith("/") or ".." in td.split("/") or "//" in td, td)
        self.assertEqual(row["action"] == "delete", dup)
        if row["date_found"]:
            self.assertEqual(row["dated"], 1)


class PdfDates(WorkspaceCase, unittest.TestCase):
    def pdf(self):
        cl = load("classify")
        ctx = SimpleNamespace(db=common.connect(self.ws.state), source=self.ws.src,
                              cfg=common.load_config(common.DEFAULT_CONFIG, "/nonexistent"))
        p = cl.PdfDates(ctx)
        p.pdftotext, p.pdfinfo = "/usr/bin/pdftotext", "/usr/bin/pdfinfo"
        return p, ctx.db

    def fake_run(self, text="", info=""):
        def run(cmd, **kw):
            out = text if "pdftotext" in cmd[0] else info
            return subprocess.CompletedProcess(cmd, 0, stdout=out.encode())
        return run

    def test_keyword_beats_first_date(self):
        p, db = self.pdf()
        text = "Ref 2020-01-01 contrat\nDate de facture : 14/03/2025\nTotal"
        with mock.patch("subprocess.run", self.fake_run(text)):
            self.assertEqual(p._extract("x.pdf"), ("2025-03-14", "day", "pdf-text"))
        db.close()

    def test_falls_back_to_first_date_then_metadata(self):
        p, db = self.pdf()
        with mock.patch("subprocess.run", self.fake_run("printed 02/01/2024")):
            self.assertEqual(p._extract("x.pdf"), ("2024-01-02", "day", "pdf-text"))
        with mock.patch("subprocess.run", self.fake_run("no date", "CreationDate:   2023-05-06T10:00:00+02")):
            self.assertEqual(p._extract("x.pdf"), ("2023-05-06", "day", "pdf-meta"))
        with mock.patch("subprocess.run", self.fake_run("no date", "Producer: x")):
            self.assertIsNone(p._extract("x.pdf"))
        db.close()

    def test_timeouts_are_survived(self):
        p, db = self.pdf()

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 20)
        with mock.patch("subprocess.run", slow):
            self.assertIsNone(p._extract("x.pdf"))
        db.close()

    def test_cache_by_size_and_mtime(self):
        self.ws.file("a.pdf", "x")
        self.ws.ok("inventory")
        p, db = self.pdf()
        f = dict(db.execute("SELECT * FROM files").fetchone())
        with mock.patch.object(p, "_extract", return_value=("2024-01-02", "day", "pdf-text")) as ex:
            self.assertEqual(p.get(f)[0], "2024-01-02")
            p.get(f)
            self.assertEqual(ex.call_count, 1)
            p.get({**f, "mtime": f["mtime"] + 1})
            self.assertEqual(ex.call_count, 2)
        db.close()


class ClassifyStep(WorkspaceCase, unittest.TestCase):
    def test_locked_and_done_rows_untouched(self):
        ws = self.ws
        ws.file("a.pdf", "a")
        ws.file("b.pdf", "b")
        ws.pipeline("rename")
        ws.exec("UPDATE plan SET target_dir='mine', locked=1 WHERE src='a.pdf'")
        ws.exec("UPDATE plan SET target_dir='done-dir', status='done' WHERE src='b.pdf'")
        ws.ok("classify")
        self.assertEqual(ws.plan("a.pdf")["target_dir"], "mine")
        self.assertEqual(ws.plan("b.pdf")["target_dir"], "done-dir")

    def test_budget_resume(self):
        ws = self.ws
        for i in range(5):
            ws.file(f"f{i}.txt", str(i))
        ws.pipeline("dupes")
        mod = load("classify")
        calls = {"n": 0}

        class Tiny:
            def __init__(self, s):
                pass

            def exceeded(self):
                calls["n"] += 1
                return calls["n"] > 2
        with mock.patch.object(mod, "Budget", Tiny):
            self.assertEqual(ws.run("classify", "--budget", "1")[0], 3)
        self.assertEqual(len(ws.q("SELECT * FROM plan")), 2)
        ws.ok("classify")
        self.assertEqual(len(ws.q("SELECT * FROM plan")), 5)

    @FAST
    @given(st.lists(st.tuples(st.sampled_from(["", "Docs/", "Proj/", "Fonts/"]),
                              st.sampled_from(["Facture 2024-01-05.pdf", "a.jpg", "x.otf", "notes.md",
                                               "package.json", "data.csv", "1234.png", "z.unknownext"]),
                              st.sampled_from([b"A", b"B", b"C"])), min_size=1, max_size=10))
    def test_every_item_planned_once(self, entries):
        ws = Workspace('[units]\ncollections = ["Docs"]\n')
        try:
            for i, (folder, name, data) in enumerate(entries):
                ws.file(f"{folder}{i}-{name}", data + name.encode())
            ws.pipeline("rename")
            standalone = {r[0] for r in ws.q("SELECT path FROM files WHERE unit_id IS NULL")}
            units = {r[0] for r in ws.q("SELECT root FROM units")}
            plan = [r["src"] for r in ws.q("SELECT src FROM plan")]
            self.assertEqual(sorted(plan), sorted(standalone | units))
            removes = {r[0] for r in ws.q("""SELECT f.path FROM dup_members m JOIN files f ON f.id=m.item_id
                                             WHERE m.item_type='file' AND m.role='remove'""")}
            deletes = {r[0] for r in ws.q("SELECT src FROM plan WHERE action='delete' AND item_type='file'")}
            self.assertEqual(removes, deletes)
            for r in ws.q("SELECT target_dir, new_name FROM plan"):
                self.assertTrue(r["new_name"] and "/" not in r["new_name"])
                self.assertFalse(r["target_dir"].startswith("/") or ".." in r["target_dir"].split("/"))
        finally:
            ws.cleanup()


# --------------------------------------------------------------------------- rename

def row(src, action="move", item_type="file", dated=0, precision=None, date_found=None, date_source=None):
    return {"src": src, "action": action, "item_type": item_type, "dated": dated, "precision": precision,
            "date_found": date_found, "date_source": date_source}


CFG = {"rename": {"max_length": 40, "strip_patterns": [r"\s+at\s+\d{1,2}[.:]\d{2}([.:]\d{2})?"],
                  "ext_aliases": {".jpeg": ".jpg"}}}


class BuildName(unittest.TestCase):
    def setUp(self):
        self.rn = load("rename")

    def name(self, r):
        return self.rn.build_name(r, CFG, 1995)

    def test_examples(self):
        self.assertEqual(self.name(row("Facture Globex 2025-03-14.pdf", dated=1, precision="month",
                                       date_found="2025-03-14", date_source="name")), "facture-globex_2025-03.pdf")
        self.assertEqual(self.name(row("WhatsApp Image 2025-12-04 at 18.20.59 (1).jpeg", dated=1, precision="day",
                                       date_found="2025-12-04", date_source="name")), "whatsapp-image_2025-12-04.jpg")
        self.assertEqual(self.name(row("Facture Acme.pdf", dated=1, precision="day", date_found="2024-11",
                                       date_source="pdf-text")), "facture-acme_2024-11.pdf")
        self.assertEqual(self.name(row("Docs/KO Sans (1)", item_type="unit")), "ko-sans")
        self.assertEqual(self.name(row("x/a (1).pdf", action="delete")), "a (1).pdf")
        self.assertEqual(self.name(row("Font Bold.otf", action="install")), "Font Bold.otf")
        self.assertEqual(self.name(row("!!!.txt")), "untitled.txt")
        self.assertEqual(self.name(row("a" * 100 + " b.txt")), "a" * 40 + ".txt")
        self.assertEqual(self.name(row("word " * 20 + ".txt")), "-".join(["word"] * 8) + ".txt")

    @given(st.text(alphabet=string.ascii_letters + string.digits + " ._-()éàü", min_size=1, max_size=40),
           st.sampled_from([".pdf", ".JPEG", ".txt", ""]),
           st.one_of(st.none(), st.dates().map(lambda d: d.isoformat())),
           st.sampled_from(["day", "month", "year"]))
    def test_file_name_shape(self, stem, ext, date, precision):
        src = f"dir/{stem}{ext}"
        r = row(src, dated=1 if date else 0, precision=precision, date_found=date,
                date_source="pdf-text" if date else None)
        out = self.name(r)
        self.assertNotIn("/", out)
        want_ext = common.ext_of(f"{stem}{ext}")  # what the file really has ("..JPEG" is a dotfile: none)
        want_ext = CFG["rename"]["ext_aliases"].get(want_ext, want_ext)
        self.assertTrue(out.endswith(want_ext), (src, out))
        base = out[: len(out) - len(want_ext)] if want_ext else out
        if date:
            suffix = common.format_date(date, "day", precision)
            self.assertTrue(base.endswith("_" + suffix), (src, out))
            base = base[: -len(suffix) - 1]
        self.assertTrue(base == "untitled" or re.fullmatch(r"[^\W_]+(-[^\W_]+)*", base), (src, out))
        self.assertLessEqual(len(base), 40)

    @given(st.text(alphabet=string.ascii_lowercase + "-", min_size=1, max_size=10),
           st.sets(st.text(alphabet=string.ascii_lowercase + "-.2", min_size=1, max_size=12), max_size=8),
           st.booleans())
    def test_unique(self, name, taken, is_dir):
        out = self.rn._unique(name + ("" if is_dir else ".txt"), {t.lower() for t in taken}, is_dir)
        self.assertNotIn(out.lower(), {t.lower() for t in taken})
        if (name + ("" if is_dir else ".txt")).lower() not in taken:
            self.assertEqual(out, name + ("" if is_dir else ".txt"))


class RenameStep(WorkspaceCase, unittest.TestCase):
    def test_collisions_disk_and_locked(self):
        ws = self.ws
        ws.file("Notes.md", "1")
        ws.file("notes (1).md", "2")
        ws.file("NOTES copy.md", "3")
        ws.file("todo.md", "4")
        (ws.dst / "documents/notes").mkdir(parents=True)
        (ws.dst / "documents/notes/todo.md").write_text("already there")
        ws.pipeline("classify")
        ws.exec("UPDATE plan SET new_name='notes.md', locked=1 WHERE src='NOTES copy.md'")
        ws.ok("rename")
        names = {r["src"]: r["new_name"] for r in ws.q("SELECT src, new_name FROM plan")}
        self.assertEqual(names["NOTES copy.md"], "notes.md")
        self.assertEqual(sorted([names["Notes.md"], names["notes (1).md"]]), ["notes-2.md", "notes-3.md"])
        self.assertEqual(names["todo.md"], "todo-2.md")

    def test_names_unique_per_target(self):
        ws = self.ws
        for i in range(6):
            ws.file(f"d{i}/report.pdf" if i % 2 else f"report ({i}).pdf", f"content {i}")
        ws.set_rules('[units]\ncollections = ["d1", "d3", "d5"]\n')
        ws.pipeline("rename")
        seen = set()
        for r in ws.q("SELECT target_dir, new_name FROM plan WHERE action='move'"):
            key = (r["target_dir"], r["new_name"].lower())
            self.assertNotIn(key, seen)
            seen.add(key)


# --------------------------------------------------------------------------- review

class Review(WorkspaceCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ws.file("a.md", "a")
        self.ws.file("b.md", "b")
        self.ws.file("1234.png", "c")
        self.ws.pipeline("rename")
        self.csv = self.ws.state.parent / "reports" / "plan.csv"

    def rows(self):
        with open(self.csv, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def save(self, rows):
        with open(self.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def test_summary_and_filters(self):
        out = self.ws.ok("review", "summary")
        self.assertIn("3 pending items", out)
        self.assertIn("needs review: 1", out)
        self.ws.ok("review", "export", "--only", "review")
        self.assertEqual([r["src"] for r in self.rows()], ["1234.png"])

    def test_roundtrip_without_edits_changes_nothing(self):
        self.ws.ok("review", "export")
        self.assertIn("0 rows updated", self.ws.ok("review", "import"))
        self.assertEqual(self.ws.q("SELECT SUM(locked) FROM plan")[0][0], 0)

    def test_edit_locks_row(self):
        self.ws.ok("review", "export")
        rows = self.rows()
        for r in rows:
            if r["src"] == "1234.png":
                r.update(new_name="cat.png", target_dir="media/pets/", needs_review="0")
        self.save(rows)
        self.ws.ok("review", "import")
        p = self.ws.plan("1234.png")
        self.assertEqual((p["new_name"], p["target_dir"], p["needs_review"], p["locked"]),
                         ("cat.png", "media/pets", 0, 1))

    def test_invalid_rows_rejected(self):
        self.ws.ok("review", "export")
        rows = self.rows()
        rows[0]["action"] = "explode"
        rows[1]["target_dir"] = "/etc" if rows[1]["src"] == "b.md" else "../../etc"
        rows[2]["new_name"] = "a/b.txt"
        self.save(rows)
        code, out = self.ws.run("review", "import")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("line "), 3)
        self.assertEqual(self.ws.q("SELECT SUM(locked) FROM plan")[0][0], 0)


if __name__ == "__main__":
    unittest.main()
