"""inventory, units, hash, dupes, profile: unit + property tests (in-process, isolated workspace)."""
from __future__ import annotations

import collections
import os
import string
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import support  # noqa: E402
from support import Workspace, WorkspaceCase, load, make_zip, write

FAST = settings(max_examples=support.EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --------------------------------------------------------------------------- inventory

class Inventory(WorkspaceCase, unittest.TestCase):
    def test_records_and_ignores(self):
        ws = self.ws
        ws.file("a.PDF", "x")
        ws.file("dir/b.jpg", "y")
        ws.file(".DS_Store", "z")
        ws.file("dir/._b.jpg", "resource fork")
        ws.file("_to_delete/old.txt", "o")
        ws.file("Makefile", "m")
        out = ws.ok("inventory")
        rows = {r["path"]: r for r in ws.q("SELECT * FROM files")}
        self.assertEqual(set(rows), {"a.PDF", "dir/b.jpg", "Makefile"}, out)
        self.assertEqual((rows["a.PDF"]["ext"], rows["a.PDF"]["family"]), (".pdf", "document"))
        self.assertEqual((rows["Makefile"]["ext"], rows["Makefile"]["family"]), ("", "none"))
        self.assertEqual(rows["dir/b.jpg"]["size"], 1)

    def test_incremental(self):
        ws = self.ws
        a = ws.file("a.txt", "1")
        ws.file("b.txt", "2")
        ws.ok("inventory")
        self.assertIn("new 0, changed 0, gone 0", ws.ok("inventory"))
        a.write_text("changed!")
        os.utime(a, (1, 1))
        (ws.src / "b.txt").unlink()
        out = ws.ok("inventory")
        self.assertIn("changed 1", out)
        self.assertIn("gone 1", out)
        self.assertEqual(ws.q("SELECT missing FROM files WHERE path='b.txt'")[0][0], 1)
        ws.file("b.txt", "back")
        ws.ok("inventory")
        self.assertEqual(ws.q("SELECT missing FROM files WHERE path='b.txt'")[0][0], 0)

    def test_symlinks_skipped(self):
        ws = self.ws
        ws.file("real/f.txt", "f")
        os.symlink(ws.src / "real", ws.src / "linkdir")
        os.symlink(ws.src / "real/f.txt", ws.src / "linkfile.txt")
        ws.ok("inventory")
        self.assertEqual([r[0] for r in ws.q("SELECT path FROM files")], ["real/f.txt"])

    def test_nfd_names_stay_openable(self):
        ws = self.ws
        nfd = unicodedata.normalize("NFD", "été.txt")
        write(ws.src / nfd, "accent")
        ws.ok("inventory")
        (path,) = [r[0] for r in ws.q("SELECT path FROM files")]
        self.assertTrue((ws.src / path).exists(), "stored path must reopen the real file")

    def test_unreadable_folder_logged(self):
        ws = self.ws
        ws.file("ok.txt")
        real_walk = os.walk

        def walk(top, onerror=None, followlinks=False):
            onerror(PermissionError(13, "Permission denied", str(ws.src / "locked")))
            yield from real_walk(top, onerror=onerror, followlinks=followlinks)
        inv = load("inventory")
        with mock.patch.object(inv.os, "walk", walk):
            out = ws.ok("inventory")
        self.assertIn("unreadable 1", out)
        self.assertEqual(len(ws.q("SELECT * FROM scan_errors")), 1)

    def test_bad_source(self):
        self.ws.src.rmdir()
        code, out = self.ws.run("inventory")
        self.assertNotEqual(code, 0)
        self.assertIn("not found", out)


# --------------------------------------------------------------------------- units

class KeepOuter(unittest.TestCase):
    paths = st.lists(st.lists(st.sampled_from("abc"), min_size=1, max_size=4).map("/".join), max_size=12)

    @given(paths)
    def test_antichain_and_cover(self, roots):
        units = load("units")
        out = {}
        for r in roots:
            units._keep_outer(out, r, "k")
        keys = list(out)
        for a in keys:
            for b in keys:
                if a != b:
                    self.assertFalse(b.startswith(a + "/"), (a, b))
        for r in roots:
            self.assertTrue(any(r == k or r.startswith(k + "/") for k in keys), r)


class Units(WorkspaceCase, unittest.TestCase):
    local_rules = '[units]\ncollections = ["Box", "Mail", "Mail/*"]\n'

    def units(self):
        return {r["root"]: r["kind"] for r in self.ws.q("SELECT root, kind FROM units")}

    def unit_of(self, path):
        r = self.ws.q("SELECT u.root FROM files f LEFT JOIN units u ON u.id=f.unit_id WHERE f.path=?", path)
        return r[0][0]

    def test_markers_bundles_collections(self):
        ws = self.ws
        ws.file("package.json", "{}")                       # marker at source root: no unit
        ws.file("proj/package.json", "{}")
        ws.file("proj/sub/inner/package.json", "{}")        # nested marker absorbed
        ws.file("proj/sub/inner/x.js", "x")
        ws.file("Box/repo/.git/HEAD", "ref")                # .git -> parent
        ws.file("Box/repo/main.py", "p")
        ws.file("Box/ios/App.xcodeproj/project.pbxproj", "p")
        ws.file("Box/Tool.app/Contents/Info.plist", "i")    # bundle -> itself
        ws.file("Box/loose.txt", "l")                       # loose in a collection: standalone
        ws.file("Mail/123/att.pdf", "m")                    # Mail/* collections: standalone
        ws.file("Fonts Pack/a.otf", "1")
        ws.file("Fonts Pack/b.otf", "2")
        ws.file("Fonts Pack/readme.txt", "r")
        ws.file("Mixed/a.jpg", "1")
        ws.file("Mixed/b.pdf", "2")
        ws.file("Wrapper/deep/package.json", "{}")          # non-collection wrapper wins, kind from inner
        ws.ok("inventory")
        ws.ok("units")
        u = self.units()
        self.assertEqual(u["proj"], "node")
        self.assertNotIn("proj/sub/inner", u)
        self.assertEqual(u["Box/repo"], "git")
        self.assertEqual(u["Box/ios"], "xcode")
        self.assertEqual(u["Box/Tool.app"], "app")
        self.assertEqual(u["Fonts Pack"], "font")
        self.assertEqual(u["Mixed"], "folder")
        self.assertEqual(u["Wrapper"], "node")
        self.assertNotIn("Box", u)
        self.assertNotIn("Mail", u)
        self.assertIsNone(self.unit_of("package.json"))
        self.assertIsNone(self.unit_of("Box/loose.txt"))
        self.assertIsNone(self.unit_of("Mail/123/att.pdf"))
        self.assertEqual(self.unit_of("proj/sub/inner/x.js"), "proj")

    def test_stable_ids_and_cleanup(self):
        ws = self.ws
        ws.file("A/a.txt", "a")
        ws.file("B/b.txt", "b")
        ws.ok("inventory")
        ws.ok("units")
        ids = {r["root"]: r["id"] for r in ws.q("SELECT id, root FROM units")}
        ws.pipeline("rename")
        (ws.src / "B/b.txt").unlink()
        (ws.src / "B").rmdir()
        ws.ok("inventory")
        ws.ok("units")
        now = {r["root"]: r["id"] for r in ws.q("SELECT id, root FROM units")}
        self.assertEqual(now, {"A": ids["A"]})
        self.assertIsNone(ws.plan("B"))

    def test_signature_depends_on_structure(self):
        ws = self.ws
        for d in ("P1", "P2"):
            ws.file(f"{d}/x/a.txt", "same")
        ws.file("P3/y/a.txt", "same")
        ws.ok("inventory")
        ws.ok("units")
        sig = {r["root"]: r["signature"] for r in ws.q("SELECT root, signature FROM units")}
        self.assertEqual(sig["P1"], sig["P2"])
        self.assertNotEqual(sig["P1"], sig["P3"])

    seg = st.sampled_from(["Box", "Mail", "p", "q", "r"])
    trees = st.lists(st.tuples(st.lists(seg, min_size=0, max_size=3),
                               st.sampled_from(["f.txt", "g.pdf", "package.json", "h.otf"])),
                     min_size=1, max_size=15)

    @FAST
    @given(trees)
    def test_assignment_invariants(self, entries):
        ws = Workspace(self.local_rules)
        try:
            files = set()
            for dirs, name in entries:
                rel = "/".join([*dirs, name])
                if any(rel.startswith(f + "/") for f in files) or any(f.startswith(rel + "/") for f in files):
                    continue
                files.add(rel)
                ws.file(rel, rel)
            ws.ok("inventory")
            ws.ok("units")
            roots = [r[0] for r in ws.q("SELECT root FROM units")]
            for a in roots:  # antichain
                self.assertFalse(any(b.startswith(a + "/") for b in roots if b != a))
            collections_ = ("box", "mail")
            for r in ws.q("SELECT f.path, u.root FROM files f LEFT JOIN units u ON u.id=f.unit_id"):
                path, root = r
                parts = path.split("/")
                if root is None:
                    # standalone: every ancestor is a collection (Box, Mail, Mail/*) and no marker root above
                    for k in range(1, len(parts)):
                        prefix = "/".join(parts[:k]).lower()
                        self.assertTrue(prefix in collections_ or prefix.startswith("mail/"), path)
                else:
                    self.assertTrue(path.startswith(root + "/"), (path, root))
        finally:
            ws.cleanup()


# --------------------------------------------------------------------------- hash

class HashFile(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.h = load("hash")

    def test_partial_equals_full_for_small_files(self):
        p = write(self.d / "s", os.urandom(1000))
        self.assertEqual(self.h.hash_file(str(p), "blake2b", 512, True), self.h.hash_file(str(p), "blake2b", 512, False))

    def test_middle_change_detected_only_by_full(self):
        head, tail = os.urandom(512), os.urandom(512)
        a = write(self.d / "a", head + b"A" * 2000 + tail)
        b = write(self.d / "b", head + b"B" * 2000 + tail)
        hf = self.h.hash_file
        self.assertEqual(hf(str(a), "blake2b", 512, True), hf(str(b), "blake2b", 512, True))
        self.assertNotEqual(hf(str(a), "blake2b", 512, False), hf(str(b), "blake2b", 512, False))

    @given(st.binary(max_size=3000), st.binary(max_size=3000), st.sampled_from(["blake2b", "sha256", "md5"]))
    def test_full_hash_is_content_equality(self, x, y, algo):
        a, b = write(self.d / "x", x), write(self.d / "y", y)
        same = self.h.hash_file(str(a), algo, 256, False) == self.h.hash_file(str(b), algo, 256, False)
        self.assertEqual(same, x == y)


class HashStep(WorkspaceCase, unittest.TestCase):
    def test_only_candidates_and_staleness(self):
        ws = self.ws
        ws.file("u1.txt", "unique-size-1")
        ws.file("a.txt", "same")
        ws.file("b.txt", "same")
        ws.file("c.txt", "diff")
        ws.ok("inventory")
        ws.ok("units")
        ws.ok("hash")
        hashed = {r[0] for r in ws.q("SELECT f.path FROM hashes h JOIN files f ON f.id=h.file_id")}
        self.assertEqual(hashed, {"a.txt", "b.txt", "c.txt"})
        self.assertIn("0 partial", ws.ok("hash"), "unchanged files are never re-hashed")
        c = ws.src / "c.txt"
        c.write_text("same")
        os.utime(c, (5, 5))
        ws.ok("inventory")
        ws.ok("hash")
        rows = ws.q("SELECT f.path, h.full FROM hashes h JOIN files f ON f.id=h.file_id")
        full = {p: h for p, h in rows}
        self.assertEqual(full["a.txt"], full["c.txt"])

    def test_algo_change_invalidates(self):
        ws = self.ws
        ws.file("a.txt", "same")
        ws.file("b.txt", "same")
        ws.pipeline("hash")
        ws.set_rules('[hash]\nalgo = "sha256"\n')
        ws.ok("hash")
        self.assertEqual({r[0] for r in ws.q("SELECT algo FROM hashes")}, {"sha256"})

    def test_budget_resume(self):
        ws = self.ws
        for i in range(6):
            ws.file(f"f{i}.bin", "x" * 10)
        ws.ok("inventory")
        ws.ok("units")
        mod = load("hash")
        calls = {"n": 0}

        class TinyBudget:
            def __init__(self, s):
                pass

            def exceeded(self):
                calls["n"] += 1
                return calls["n"] > 2
        with mock.patch.object(mod, "Budget", TinyBudget):
            code, out = ws.run("hash", "--budget", "1")
        self.assertEqual(code, 3, out)
        self.assertEqual(len(ws.q("SELECT * FROM hashes")), 2)
        self.assertEqual(ws.run("hash")[0], 0)
        self.assertEqual(len(ws.q("SELECT * FROM hashes WHERE full IS NOT NULL")), 6)

    def test_unreadable_file_marked(self):
        ws = self.ws
        ws.file("a.txt", "same")
        ws.file("b.txt", "same")
        ws.ok("inventory")
        ws.ok("units")
        mod = load("hash")
        real = mod.hash_file

        def flaky(path, *a):
            if path.endswith("a.txt"):
                raise PermissionError("denied")
            return real(path, *a)
        with mock.patch.object(mod, "hash_file", flaky):
            out = ws.ok("hash")
        self.assertIn("1 unreadable", out)
        self.assertEqual(ws.q("SELECT h.partial FROM hashes h JOIN files f ON f.id=h.file_id WHERE f.path='a.txt'")[0][0],
                         "ERR")


# --------------------------------------------------------------------------- dupes

class Keeper(unittest.TestCase):
    def test_order(self):
        d = load("dupes")
        cfg = {"prefer": ["Organized/*", "Documents/*"], "avoid": ["*/_to_delete/*"]}
        cands = [("x/_to_delete/a.pdf", 1), ("a.pdf", 1), ("Misc/a (1).pdf", 1), ("Misc/a.pdf", 5),
                 ("Misc/b/a.pdf", 2), ("Documents/a.pdf", 9), ("Organized/z/a.pdf", 9)]
        ranked = sorted(cands, key=lambda c: d.keeper_key(c[0], c[1], cfg))
        self.assertEqual([c[0] for c in ranked],
                         ["Organized/z/a.pdf", "Documents/a.pdf", "Misc/b/a.pdf", "Misc/a.pdf",
                          "Misc/a (1).pdf", "a.pdf", "x/_to_delete/a.pdf"])

    @given(st.lists(st.tuples(st.text(string.ascii_lowercase + "/ ()1", min_size=1, max_size=12),
                              st.floats(0, 1e9)), min_size=1, max_size=8, unique_by=lambda t: t[0]))
    def test_total_order_deterministic(self, cands):
        d = load("dupes")
        key = lambda c: d.keeper_key(c[0], c[1], {})  # noqa: E731
        self.assertEqual(sorted(cands, key=key), sorted(reversed(cands), key=key))


class Dupes(WorkspaceCase, unittest.TestCase):
    @FAST
    @given(st.lists(st.tuples(st.sampled_from(["", "A/", "Documents/", "B/c/"]),
                              st.sampled_from(["x", "y", "z", "w"]),
                              st.sampled_from([b"one", b"two", b"three" * 50000, b"four"])),
                    min_size=1, max_size=12))
    def test_groups_match_ground_truth(self, entries):
        ws = Workspace('[units]\ncollections = ["A", "Documents", "B", "B/*"]\n')
        try:
            content = {}
            for i, (folder, stem, data) in enumerate(entries):
                rel = f"{folder}{stem}{i}.bin"
                ws.file(rel, data)
                content[rel] = data
            ws.pipeline("dupes")
            truth = collections.defaultdict(set)
            for rel, data in content.items():
                truth[data].add(rel)
            expected = {frozenset(v) for v in truth.values() if len(v) > 1}
            got = collections.defaultdict(set)
            roles = collections.defaultdict(list)
            for gid, path, role in ws.q("""SELECT m.group_id, f.path, m.role FROM dup_members m
                                           JOIN files f ON f.id=m.item_id WHERE m.item_type='file'"""):
                got[gid].add(path)
                roles[gid].append(role)
            self.assertEqual({frozenset(v) for v in got.values()}, expected)
            for gid, r in roles.items():
                self.assertEqual(r.count("keep"), 1)
                self.assertEqual(r.count("remove"), len(r) - 1)
        finally:
            ws.cleanup()

    def test_unit_duplicates_need_full_proof(self):
        ws = self.ws
        ws.file("Pack/a.png", "same-a")
        ws.file("Pack 2/a.png", "same-a")
        ws.file("Other/a.png", "diff-a")   # same structure and size, different content
        ws.pipeline("dupes")
        rows = ws.q("""SELECT u.root, m.role FROM dup_members m JOIN units u ON u.id=m.item_id
                       WHERE m.item_type='unit' ORDER BY u.root""")
        self.assertEqual([tuple(r) for r in rows], [("Pack", "keep"), ("Pack 2", "remove")])

    def test_files_inside_units_never_grouped(self):
        ws = self.ws
        ws.file("proj/package.json", "{}")
        ws.file("proj/a.js", "same")
        ws.file("proj/b.js", "same")
        ws.pipeline("dupes")
        self.assertEqual(ws.q("SELECT COUNT(*) FROM dup_groups WHERE kind='file'")[0][0], 0)

    def zip_case(self, members, disk, zip_rel="Bundle.zip", no_crc=False):
        ws = self.ws
        for rel, data in disk.items():
            ws.file(rel, data)
        make_zip(ws.src / zip_rel, members)
        ws.ok("inventory")
        ws.ok("units")
        ws.ok("hash")
        ws.ok("dupes", *(["--no-crc"] if no_crc else []))
        return ws.q("SELECT m.reason FROM dup_members m JOIN dup_groups g ON g.id=m.group_id "
                    "WHERE g.kind='zip' AND m.role='remove'")

    def test_zip_extracted_with_top_folder(self):
        hit = self.zip_case({"Bundle/a.txt": "A", "Bundle/b/c.txt": "C"},
                            {"Bundle/a.txt": "A", "Bundle/b/c.txt": "C"})
        self.assertEqual(hit[0][0], "already extracted in Bundle")

    def test_zip_extracted_without_top_folder(self):
        hit = self.zip_case({"a.txt": "A", "b.txt": "B"}, {"Bundle (1)/a.txt": "A", "Bundle (1)/b.txt": "B"})
        self.assertEqual(hit[0][0], "already extracted in Bundle (1)")

    def test_zip_crc_mismatch(self):
        self.assertEqual(self.zip_case({"Bundle/a.txt": "AAAA"}, {"Bundle/a.txt": "BBBB"}), [])

    def test_zip_crc_skipped_with_flag(self):
        self.assertEqual(len(self.zip_case({"Bundle/a.txt": "AAAA"}, {"Bundle/a.txt": "BBBB"}, no_crc=True)), 1)

    def test_zip_missing_member(self):
        self.assertEqual(self.zip_case({"Bundle/a.txt": "A", "Bundle/extra.txt": "E"}, {"Bundle/a.txt": "A"}), [])

    def test_zip_inside_its_folder_is_not_its_copy(self):
        self.assertEqual(self.zip_case({"a.txt": "A"}, {"Bundle/a.txt": "A"}, zip_rel="Bundle/Bundle.zip"), [])

    def test_bad_zip_ignored(self):
        ws = self.ws
        ws.file("Bundle/a.txt", "A")
        ws.file("Bundle.zip", "not a zip")
        ws.pipeline("dupes")
        self.assertEqual(ws.q("SELECT COUNT(*) FROM dup_groups WHERE kind='zip'")[0][0], 0)

    def test_report_written(self):
        ws = self.ws
        ws.file("a.txt", "same")
        ws.file("b (1).txt", "same")
        ws.pipeline("dupes")
        csv_ = (ws.state.parent / "reports" / "dupes.csv").read_text()
        self.assertIn("b (1).txt", csv_)
        self.assertIn("keep", csv_)


# --------------------------------------------------------------------------- profile

class Profile(WorkspaceCase, unittest.TestCase):
    def test_report_consistent(self):
        ws = self.ws
        ws.file("a.pdf", "1")
        ws.file("b.pdf", "22")
        ws.file("c.jpg", "333")
        ws.file("d (1).jpg", "333")
        ws.file("Fonts/x.otf", "4")
        ws.pipeline("dupes")
        out = ws.ok("profile")
        md = (ws.state.parent / "reports" / "profile.md").read_text()
        self.assertIn("**5 files", md)
        self.assertIn("Duplicate ratio", md)
        ext_csv = (ws.state.parent / "reports" / "extensions.csv").read_text().splitlines()
        self.assertEqual(sum(int(line.split(",")[2]) for line in ext_csv[1:]), 5)
        self.assertIn("document", out)

    def test_empty_state(self):
        code, out = self.ws.run("profile")
        self.assertNotEqual(code, 0)
        self.assertIn("run inventory.py first", out)


if __name__ == "__main__":
    unittest.main()
