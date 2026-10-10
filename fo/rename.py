#!/usr/bin/env python3
"""Step 7. Compute the new name of every planned item.

  file  : lowercase-slug.ext, and lowercase-slug_DATE.ext when its rule is dated
          (the date is taken out of the name first, so it never appears twice)
  unit  : lowercase-slug of the folder name, its content is never renamed
  delete: original name kept (it only moves to _to_delete)
  install: original name kept (fonts are installed under their own file name)

Name clashes inside the same target folder get -2, -3... (also against files already on disk).
Rows edited by hand (locked=1) keep their name but still reserve it.
"""
import collections as C
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (Run, ext_of, find_date, format_date, remove_span, setup, slugify,  # noqa: E402
                    stem_of, strip_copy_suffix)


def build_name(row, cfg, min_year) -> str:
    rcfg = cfg.get("rename", {})
    max_len = int(rcfg.get("max_length", 80))
    name = os.path.basename(row["src"])
    if row["action"] in ("delete", "install"):
        return name  # trashed or installed under its original name
    if row["item_type"] == "unit":
        return _slug(strip_copy_suffix(name), max_len) or "folder"

    ext = ext_of(name)
    stem = stem_of(name)
    ext = rcfg.get("ext_aliases", {}).get(ext, ext)
    if row["dated"] and row["date_source"] == "name":
        hit = find_date(stem, allow_year=row["precision"] == "year", min_year=min_year)
        if hit:
            stem = remove_span(stem, hit[2])
    for pattern in rcfg.get("strip_patterns", []):
        stem = re.sub(pattern, "", stem, flags=re.I)
    stem = strip_copy_suffix(stem.strip(" _-."))
    slug = _slug(stem, max_len) or "untitled"
    if row["dated"] and row["date_found"]:
        found_precision = {4: "year", 7: "month"}.get(len(row["date_found"]), "day")
        slug = f"{slug}_{format_date(row['date_found'], found_precision, row['precision'] or 'day')}"
    return slug + ext


def _slug(text, max_len):
    s = slugify(text)
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s


def main():
    ctx = setup(__doc__)
    db = ctx.db
    min_year = int(ctx.cfg.get("dates", {}).get("min_year", 1995))

    with Run(ctx, "rename") as run:
        rows = db.execute("SELECT * FROM plan WHERE status='pending' ORDER BY target_dir, src").fetchall()
        taken: dict[str, set] = C.defaultdict(set)

        def on_disk(target_dir):
            if ctx.target is None:
                return set()
            d = ctx.target / target_dir
            try:
                return {n.lower() for n in os.listdir(d)} if d.is_dir() else set()
            except OSError:
                return set()

        # names fixed by hand are reserved first
        for r in rows:
            if r["locked"] and r["new_name"]:
                taken[r["target_dir"]].add(r["new_name"].lower())

        changed, scanned = 0, set()
        for r in rows:
            if r["locked"] and r["new_name"]:
                continue
            tdir = r["target_dir"] or ""
            if tdir not in scanned:
                taken[tdir] |= on_disk(tdir)
                scanned.add(tdir)
            want = build_name(r, ctx.cfg, min_year)
            new = _unique(want, taken[tdir], r["item_type"] == "unit")
            taken[tdir].add(new.lower())
            if new != r["new_name"]:
                db.execute("UPDATE plan SET new_name=? WHERE id=?", (new, r["id"]))
                changed += 1
        db.commit()
        run.notes = f"rows={len(rows)} changed={changed}"
        print(f"rename: {len(rows)} planned items, {changed} names set or updated")
        for r in db.execute("""SELECT src, target_dir, new_name FROM plan WHERE status='pending' AND action='move'
                               ORDER BY RANDOM() LIMIT 12"""):
            print(f"  {r['src']}\n    -> {r['target_dir']}/{r['new_name']}")


def _unique(name: str, taken: set, is_dir: bool) -> str:
    if name.lower() not in taken:
        return name
    stem, ext = (name, "") if is_dir else (stem_of(name), ext_of(name))
    i = 2
    while f"{stem}-{i}{ext}".lower() in taken:
        i += 1
    return f"{stem}-{i}{ext}"


if __name__ == "__main__":
    main()
