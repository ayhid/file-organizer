"""fo/report.py: self-contained HTML report with per-row details."""
from __future__ import annotations

import json
import re
import string
import unittest

from hypothesis import given, settings, strategies as st

from support import EXAMPLES, WorkspaceCase, load

report = load("report")


def payload(page: str) -> dict:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
    assert m, "data block missing"
    return json.loads(m.group(1))


class Report(WorkspaceCase, unittest.TestCase):
    def build(self):
        self.ws.file("Facture 2025-03-14.pdf", "inv")
        self.ws.file("a.txt", "dup")
        self.ws.file("a (1).txt", "dup")
        self.ws.file("1234.png", "needs review")
        self.ws.pipeline()

    def page(self, *args, today="2026-10-11"):
        self.ws.ok("report", "--today", today, *args)
        return (self.ws.state.parent / "reports" / "report.html").read_text(encoding="utf-8")

    def test_writes_dated_and_latest_copy(self):
        self.build()
        self.page()
        rep = self.ws.state.parent / "reports"
        self.assertEqual((rep / "report-2026-10-11.html").read_text(), (rep / "report.html").read_text())

    def test_every_pending_row_is_in_the_page_with_details(self):
        self.build()
        d = payload(self.page())
        pending = {r["src"] for r in self.ws.q("SELECT src FROM plan WHERE status='pending'")}
        self.assertEqual({r["src"] for r in d["rows"]}, pending)
        by = {r["src"]: r for r in d["rows"]}
        self.assertEqual(by["1234.png"]["st"], "review")
        self.assertEqual(by["a (1).txt"]["st"], "delete")
        dup = by["a (1).txt"]["dup"]
        self.assertEqual(sorted(m["role"] for m in dup["members"]), ["keep", "remove"])
        self.assertTrue(any(m["self"] and m["role"] == "remove" for m in dup["members"]))
        self.assertEqual(d["kpi"]["review"], sum(1 for r in d["rows"] if r["st"] == "review"))

    def test_page_is_offline_and_has_no_em_dash(self):
        self.build()
        page = self.page()
        self.assertNotIn("—", page)
        self.assertNotRegex(page, r'<(script|link)[^>]+(src|href)="https?://')

    def test_hostile_names_cannot_break_out_of_the_data_block(self):
        self.build()
        d = payload(self.page())
        evil = "</script><script>alert(1)</script>\u2028<!-- a \u2014 b"
        d["rows"][0]["src"] = d["rows"][0]["name"] = evil
        page = report.render(d, "")
        self.assertEqual(page.count("</script>"), 2)  # data block + app script
        self.assertEqual(payload(page)["rows"][0]["src"], evil)
        self.assertNotIn("\u2014", page)

    def test_notes_are_escaped_and_rendered(self):
        self.build()
        notes = self.ws.tmp / "notes.md"
        notes.write_text("# Revue\nJ'ai décidé 3 lignes.\n\n- <b>x</b> rangé\n- y", encoding="utf-8")
        page = self.page("--notes", str(notes))
        self.assertIn("<h3>Revue</h3>", page)
        self.assertIn("<li>&lt;b&gt;x&lt;/b&gt; rangé</li>", page)

    def test_path_alias_rewrites_displayed_paths(self):
        self.build()
        d = payload(self.page("--path-alias", f"{self.ws.tmp}=/Users/me", "--repo-dir", "/Users/me/repo"))
        self.assertEqual(d["source"], "/Users/me/src")
        self.assertTrue(all(c.startswith("cd /Users/me/repo && ") for _, c in d["commands"]))
        self.assertIn("--apply", d["commands"][1][1])
        self.assertTrue(all(r["abs"].startswith("/Users/me/dst/") for r in d["rows"]))

    def test_weekly_baseline_survives_same_day_reruns(self):
        self.build()
        self.page(today="2026-10-04")
        self.ws.file("nouveau.pdf", "new")
        self.ws.pipeline()
        first = payload(self.page(today="2026-10-11"))
        self.assertEqual([r["src"] for r in first["rows"] if r["new"]], ["nouveau.pdf"])
        # the review edits a row after the first report of the day, then the routine runs again
        pid = self.ws.q("SELECT id FROM plan WHERE src='1234.png'")[0]["id"]
        self.ws.exec("UPDATE plan SET needs_review=0, locked=1, new_name='scan.png', updated_at='9999' WHERE id=?",
                     pid)
        second = payload(self.page(today="2026-10-11"))
        self.assertEqual([r["src"] for r in second["rows"] if r["new"]], ["nouveau.pdf"])
        self.assertEqual({r["src"]: r["st"] for r in second["rows"]}["1234.png"], "decided")
        nxt = payload(self.page(today="2026-10-18"))
        self.assertFalse(any(r["new"] for r in nxt["rows"]))

    def test_bad_alias_is_rejected(self):
        self.build()
        code, out = self.ws.run("report", "--path-alias", "nope")
        self.assertNotEqual(code, 0)
        self.assertIn("FROM=TO", out)


class Units(unittest.TestCase):
    @settings(max_examples=EXAMPLES, deadline=None)
    @given(st.text(string.ascii_letters + "/_", min_size=1, max_size=20),
           st.text(string.ascii_letters + "/_", max_size=20))
    def test_alias_only_rewrites_whole_prefixes(self, base, rest):
        a = "/" + base.strip("/") if base.strip("/") else "/x"
        out = report.alias(a + "/" + rest, [(a, "/T")])
        self.assertEqual(out, "/T/" + rest)
        self.assertEqual(report.alias(a + "zz/f", [(a, "/T")])[:3] != "/T/", True)

    def test_reason_translation_keeps_unknown_parts(self):
        self.assertEqual(report.translate_reason("no date found; custom"), "aucune date trouvée ; custom")
        self.assertEqual(report.translate_reason(None), "")


if __name__ == "__main__":
    unittest.main()
