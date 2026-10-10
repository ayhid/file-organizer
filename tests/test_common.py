"""common.py: naming, dates, config, globbing, safe moves. Unit + property tests."""
from __future__ import annotations

import datetime as dt
import errno
import os
import string
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import settings as _settings
_settings.register_profile('fo', max_examples=int(__import__('os').environ.get('FO_EXAMPLES', '100')), deadline=None)
_settings.load_profile('fo')
from hypothesis import strategies as st

import support  # noqa: F401  (puts fo/ on sys.path)
import common as C

names = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/\x00"),
                min_size=1, max_size=40)


class Slugify(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(C.slugify("Cahier des charges V3 (Élise) & co"), "cahier-des-charges-v3-elise-and-co")
        self.assertEqual(C.slugify("  --Hello__World--  "), "hello-world")
        self.assertEqual(C.slugify("ملاحظة عربية"), "ملاحظة-عربية")
        self.assertEqual(C.slugify("Straße"), "straße")
        self.assertEqual(C.slugify("contact@opkod"), "contact-at-opkod")
        self.assertEqual(C.slugify("!!!"), "")

    @given(names)
    def test_idempotent(self, s):
        once = C.slugify(s)
        self.assertEqual(C.slugify(once), once)

    @given(names)
    def test_shape(self, s):
        out = C.slugify(s)
        self.assertFalse(out.startswith("-") or out.endswith("-"))
        self.assertNotIn("--", out)
        self.assertTrue(all(ch.isalnum() or ch == "-" for ch in out), out)
        self.assertEqual(out, out.lower())
        self.assertFalse(any(unicodedata.combining(ch) for ch in out))

    @given(st.text(alphabet=string.ascii_letters + string.digits + " _-.", min_size=1, max_size=30))
    def test_ascii_keeps_every_alnum(self, s):
        self.assertEqual(C.slugify(s).replace("-", ""), "".join(ch for ch in s.lower() if ch.isalnum()))


class CopySuffix(unittest.TestCase):
    def test_examples(self):
        for raw, clean in [("photo (2)", "photo"), ("Palestine 2", "Palestine"), ("report copy", "report"),
                           ("report copy 3", "report"), ("doc - copie", "doc"), ("x (1) (2)", "x"),
                           ("Windows 10", "Windows 10"), ("Chapter 1", "Chapter 1"), ("v2", "v2"),
                           ("Ko Sans", "Ko Sans"), ("(1)", "(1)")]:
            self.assertEqual(C.strip_copy_suffix(raw), clean, raw)

    def test_is_copy_name(self):
        self.assertTrue(C.is_copy_name("a (1).pdf"))
        self.assertTrue(C.is_copy_name("a 2.pdf"))
        self.assertFalse(C.is_copy_name("a.pdf"))
        self.assertFalse(C.is_copy_name("Windows 10.iso"))

    @given(names)
    def test_idempotent_and_contained(self, s):
        out = C.strip_copy_suffix(s)
        self.assertEqual(C.strip_copy_suffix(out), out)
        self.assertIn(out, s)
        self.assertTrue(out)  # never empties a name

    @given(st.text(alphabet=string.ascii_letters, min_size=1, max_size=20), st.integers(1, 99))
    def test_parenthesised_counter_removed(self, base, n):
        self.assertEqual(C.strip_copy_suffix(f"{base} ({n})"), base)


class Extensions(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(C.ext_of("a.PDF"), ".pdf")
        self.assertEqual(C.ext_of("a.tar.gz"), ".tar.gz")
        self.assertEqual(C.ext_of("Makefile"), "")
        self.assertEqual(C.ext_of(".bashrc"), "")
        self.assertEqual(C.ext_of("Zafran typeface v0.2 final"), "")  # ".2 final" has a space
        self.assertEqual(C.stem_of("a.tar.gz"), "a")

    @given(names)
    def test_stem_plus_ext_is_name(self, n):
        stem, ext = C.stem_of(n), C.ext_of(n)
        self.assertEqual(n[len(stem):].lower(), ext)
        self.assertTrue(n.startswith(stem))

    def test_families(self):
        fam = C.build_family_map({"families": {"image": ["jpg", ".PNG"], "doc": ["pdf"]}})
        self.assertEqual(fam, {".jpg": "image", ".png": "image", ".pdf": "doc"})
        self.assertEqual(C.family_of(".png", fam), "image")
        self.assertEqual(C.family_of(".xyz", fam), "other")
        self.assertEqual(C.family_of("", fam), "none")


FORMATS = ["%Y-%m-%d", "%Y%m%d", "%Y_%m_%d", "%Y.%m.%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d%m%Y",
           "%d %B %Y", "%B %d, %Y", "%b %d %Y", "%d_%b_%Y"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
             "octobre", "novembre", "décembre"]
safe_text = st.text(alphabet="xqzwy _", max_size=12)  # cannot spell a month name or a digit
dates = st.dates(min_value=dt.date(1995, 1, 1), max_value=dt.date(2026, 12, 31))


class Dates(unittest.TestCase):
    def test_examples(self):
        f = lambda s: (C.find_date(s) or (None, None))[:2]  # noqa: E731
        self.assertEqual(f("invoice_20250403_1819"), ("2025-04-03", "day"))
        self.assertEqual(f("2026-02 INFOS SITE"), ("2026-02", "month"))
        self.assertEqual(f("ChatGPT Image Mar 11, 2026, 09_20_38 PM"), ("2026-03-11", "day"))
        self.assertEqual(f("ChatGPT_Image_Feb_16__2026__08_15"), ("2026-02-16", "day"))
        self.assertEqual(f("releve 14 février 2024"), ("2024-02-14", "day"))
        self.assertEqual(f("1er mai 2025"), ("2025-05-01", "day"))
        self.assertEqual(f("relevé mars 2024"), ("2024-03", "month"))
        self.assertEqual(f("scan 03/04/2025"), ("2025-04-03", "day"))
        self.assertEqual(f("Document_numerise_23052025"), ("2025-05-23", "day"))
        self.assertEqual(f("exercice_28_10_2025"), ("2025-10-28", "day"))
        for none in ("2025X_269473", "id 5236524644", "pieces-jointes-60187392", "v1.2.2", "31/02/2024",
                     "2025-13-01", "IMG_1234", "", "1990-01-01", "2099-01-01"):
            self.assertIsNone(C.find_date(none), none)

    def test_year_only_when_allowed(self):
        self.assertIsNone(C.find_date("avis impot 2024"))
        self.assertEqual(C.find_date("avis impot 2024", allow_year=True)[:2], ("2024", "year"))

    def test_day_wins_over_month(self):
        self.assertEqual(C.find_date("2024-05 then 2024-06-07")[:2], ("2024-06-07", "day"))

    @given(dates, st.sampled_from(FORMATS), safe_text, safe_text)
    def test_roundtrip(self, d, fmt, pre, post):
        text = f"{pre} {d.strftime(fmt)} {post}"
        hit = C.find_date(text)
        self.assertIsNotNone(hit, text)
        self.assertEqual(hit[:2], (d.isoformat(), "day"), text)
        a, b = hit[2]
        self.assertTrue(0 <= a < b <= len(text))

    @given(dates, safe_text)
    def test_roundtrip_french(self, d, pre):
        text = f"{pre} {d.day} {FR_MONTHS[d.month - 1]} {d.year}"
        self.assertEqual(C.find_date(text)[:2], (d.isoformat(), "day"), text)

    @given(dates, safe_text)
    def test_month_precision(self, d, pre):
        self.assertEqual(C.find_date(f"{pre} {d:%Y-%m} {pre}")[:2], (d.isoformat()[:7], "month"))

    @given(st.text(max_size=80), st.booleans())
    def test_never_raises_and_result_is_valid(self, text, allow_year):
        hit = C.find_date(text, allow_year=allow_year)
        if hit:
            iso, precision, (a, b) = hit
            self.assertEqual(len(iso), {"day": 10, "month": 7, "year": 4}[precision])
            year = int(iso[:4])
            self.assertTrue(1995 <= year <= dt.date.today().year + 1)
            if precision == "day":
                dt.date.fromisoformat(iso)

    @given(dates, st.sampled_from(["day", "month", "year"]), st.sampled_from(["day", "month", "year"]))
    def test_format_date_never_more_precise(self, d, found, wanted):
        iso = {"day": d.isoformat(), "month": d.isoformat()[:7], "year": str(d.year)}[found]
        out = C.format_date(iso, found, wanted)
        order = {"year": 4, "month": 7, "day": 10}
        self.assertEqual(len(out), min(order[found], order[wanted]))
        self.assertTrue(iso.startswith(out))

    def test_remove_span(self):
        hit = C.find_date("Facture Globex 2025-03-14")
        self.assertEqual(C.remove_span("Facture Globex 2025-03-14", hit[2]), "Facture Globex")


nested = st.recursive(
    st.one_of(st.integers(), st.text(max_size=5), st.lists(st.integers(), max_size=3)),
    lambda inner: st.dictionaries(st.text("abc", min_size=1, max_size=2), inner, max_size=3),
    max_leaves=6)
tables = st.dictionaries(st.text("abc", min_size=1, max_size=2), nested, max_size=4)


class Config(unittest.TestCase):
    @settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
    @given(tables, tables)
    def test_merge_properties(self, base, local):
        out = C.merge_config(base, local)
        self.assertEqual(set(out), set(base) | set(local))
        for k, v in out.items():
            if k not in local:
                self.assertEqual(v, base[k])
            elif isinstance(local[k], list) and isinstance(base.get(k), list):
                self.assertEqual(v, local[k] + base[k])  # local items first
            elif isinstance(local[k], dict) and isinstance(base.get(k), dict):
                self.assertEqual(v, C.merge_config(base[k], local[k]))
            else:
                self.assertEqual(v, local[k])

    @settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
    @given(tables)
    def test_merge_identity(self, base):
        self.assertEqual(C.merge_config(base, {}), base)
        self.assertEqual(C.merge_config({}, base), base)

    def test_merge_does_not_mutate(self):
        base, local = {"a": [1], "t": {"x": 1}}, {"a": [0], "t": {"y": 2}}
        C.merge_config(base, local)
        self.assertEqual(base, {"a": [1], "t": {"x": 1}})

    def test_load_config_with_local(self):
        with tempfile.TemporaryDirectory() as d:
            base, local = Path(d) / "r.toml", Path(d) / "l.toml"
            base.write_text('[paths]\nsource = "a"\n[units]\ncollections = ["X"]\n')
            local.write_text('[paths]\nsource = "b"\n[units]\ncollections = ["Y"]\n')
            cfg = C.load_config(base, local)
            self.assertEqual(cfg["paths"]["source"], "b")
            self.assertEqual(cfg["units"]["collections"], ["Y", "X"])
            self.assertEqual(cfg["_local_rules"], str(local.resolve()))
            # missing local file is fine
            cfg = C.load_config(base, Path(d) / "missing.toml")
            self.assertEqual(cfg["paths"]["source"], "a")
            # environment variable
            with mock.patch.dict(os.environ, {"FO_LOCAL_RULES": str(local)}):
                self.assertEqual(C.load_config(base)["paths"]["source"], "b")

    def test_repo_rules_parse_and_are_generic(self):
        cfg = C.load_config(C.DEFAULT_CONFIG, "/nonexistent")
        self.assertTrue(cfg["classify"]["rules"])
        self.assertEqual(cfg["classify"]["rules"][-1]["name"], "fallback")
        names_ = [r["name"] for r in cfg["classify"]["rules"]]
        self.assertEqual(len(names_), len(set(names_)), "rule names must be unique")
        self.assertNotIn("clients", cfg, "client names belong in the local rules file")
        for r in cfg["classify"]["rules"]:
            if r.get("name_re"):
                import re
                re.compile(r["name_re"])


class Glob(unittest.TestCase):
    def test_examples(self):
        self.assertTrue(C.glob_any("Organized/a/b.pdf", ["Organized/*"]))
        self.assertTrue(C.glob_any("organized", ["Organized"]))
        self.assertFalse(C.glob_any("Organized", ["Organized/*"]))
        self.assertFalse(C.glob_any("x", []))
        self.assertFalse(C.glob_any("x", None))

    @given(st.text(alphabet=string.ascii_letters + "/._", max_size=20),
           st.text(alphabet=string.ascii_letters + "/*?", max_size=10))
    def test_case_insensitive(self, path, pattern):
        self.assertEqual(C.glob_any(path.upper(), [pattern]), C.glob_any(path.lower(), [pattern.upper()]))


class Misc(unittest.TestCase):
    def test_human(self):
        self.assertEqual(C.human(0), "0 B")
        self.assertEqual(C.human(1536), "1.50 KB")
        self.assertEqual(C.human(5 * 1024 ** 3), "5.00 GB")

    def test_nfc(self):
        nfd = unicodedata.normalize("NFD", "été")
        self.assertNotEqual(nfd, "été")
        self.assertEqual(C.nfc(nfd), "été")

    def test_budget(self):
        self.assertFalse(C.Budget(0).exceeded())
        b = C.Budget(0.01)
        time.sleep(0.02)
        self.assertTrue(b.exceeded())

    def test_expand(self):
        self.assertEqual(C.expand("~/x"), Path.home().joinpath("x").resolve())


tree_names = st.text(alphabet=string.ascii_letters + string.digits + " _-é", min_size=1, max_size=8).filter(
    lambda s: s.strip() not in ("", ".", ".."))
trees = st.dictionaries(st.lists(tree_names, min_size=1, max_size=3).map(lambda p: "/".join(p)),
                        st.binary(max_size=64), min_size=1, max_size=6)


class MovePath(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_file_and_dir(self):
        support.write(self.d / "a.txt", "A")
        self.assertEqual(C.move_path(str(self.d / "a.txt"), str(self.d / "x/y/a.txt")), ("done", None))
        self.assertEqual((self.d / "x/y/a.txt").read_text(), "A")
        support.write(self.d / "dir/sub/f", "F")
        C.move_path(str(self.d / "dir"), str(self.d / "moved/dir"))
        self.assertEqual((self.d / "moved/dir/sub/f").read_text(), "F")
        self.assertFalse((self.d / "dir").exists())

    def test_never_overwrites(self):
        support.write(self.d / "a", "A")
        support.write(self.d / "b", "B")
        with self.assertRaises(FileExistsError):
            C.move_path(str(self.d / "a"), str(self.d / "b"))
        self.assertEqual((self.d / "a").read_text(), "A")
        self.assertEqual((self.d / "b").read_text(), "B")

    def test_never_overwrites_broken_symlink(self):
        support.write(self.d / "a", "A")
        os.symlink(self.d / "nowhere", self.d / "link")
        with self.assertRaises(FileExistsError):
            C.move_path(str(self.d / "a"), str(self.d / "link"))

    def _exdev(self, *a, **k):
        raise OSError(errno.EXDEV, "cross-device link")

    def test_cross_volume_file_and_dir(self):
        support.write(self.d / "a", "A")
        support.write(self.d / "dir/f", "F")
        with mock.patch("os.rename", self._exdev):
            self.assertEqual(C.move_path(str(self.d / "a"), str(self.d / "b/a")), ("done", None))
            self.assertEqual(C.move_path(str(self.d / "dir"), str(self.d / "b/dir")), ("done", None))
        self.assertEqual((self.d / "b/a").read_text(), "A")
        self.assertEqual((self.d / "b/dir/f").read_text(), "F")
        self.assertFalse((self.d / "a").exists() or (self.d / "dir").exists())

    def test_cross_volume_source_not_removable(self):
        support.write(self.d / "a", "A")

        def deny(*a, **k):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch("os.rename", self._exdev), mock.patch("os.remove", deny):
            status, err = C.move_path(str(self.d / "a"), str(self.d / "b/a"))
        self.assertEqual(status, "copied")
        self.assertIn("not removed", err)
        self.assertTrue((self.d / "a").exists() and (self.d / "b/a").exists())

    def test_other_errors_propagate(self):
        support.write(self.d / "a", "A")

        def boom(*a, **k):
            raise OSError(errno.EACCES, "denied")
        with mock.patch("os.rename", boom), self.assertRaises(OSError):
            C.move_path(str(self.d / "a"), str(self.d / "b"))
        self.assertTrue((self.d / "a").exists())

    @settings(max_examples=40, deadline=None)
    @given(trees, st.booleans())
    def test_round_trip_preserves_tree(self, files, cross_volume):
        root = Path(tempfile.mkdtemp(dir=self.d))
        src = root / "src"
        try:
            for rel, data in files.items():
                p = src / rel
                assume(not any((src / Path(*Path(rel).parts[:i])).is_file()
                               for i in range(1, len(Path(rel).parts))))
                if p.is_dir():
                    continue
                support.write(p, data)
        except (IsADirectoryError, NotADirectoryError, FileExistsError):
            assume(False)
        before = support.snapshot(src)
        ctx = mock.patch("os.rename", self._exdev) if cross_volume else mock.patch("os.getpid")
        with ctx:
            C.move_path(str(src), str(root / "elsewhere" / "src"))
            C.move_path(str(root / "elsewhere" / "src"), str(src))
        self.assertEqual(support.snapshot(src), before)


if __name__ == "__main__":
    unittest.main()
