#!/usr/bin/env python3
"""Step 5. File distribution profile: families, extensions, folders, units, duplicates.

Writes reports/profile.md and reports/extensions.csv, prints a short summary.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Run, human, now_iso, setup  # noqa: E402


def pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "-"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main():
    ctx = setup(__doc__, lambda ap: ap.add_argument("--top", type=int, default=20, help="rows per table"))
    db, top = ctx.db, ctx.args.top

    with Run(ctx, "profile"):
        t = db.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s, SUM(unit_id IS NULL) solo "
                       "FROM files WHERE missing=0").fetchone()
        N, S = t["n"], t["s"]
        if not N:
            sys.exit("No files in state: run inventory.py first")
        md = [f"# File profile: {ctx.source}", f"_Generated {now_iso()}_", "",
              f"**{N} files, {human(S)}**. {t['solo']} standalone files, {N - t['solo']} inside units.", ""]

        fams = db.execute("SELECT family, COUNT(*) n, SUM(size) s FROM files WHERE missing=0 "
                          "GROUP BY family ORDER BY n DESC").fetchall()
        md += ["## Families", table(["family", "files", "% files", "size", "% size"],
                                    [(f["family"], f["n"], pct(f["n"], N), human(f["s"]), pct(f["s"], S))
                                     for f in fams]), ""]

        exts = db.execute("SELECT ext, family, COUNT(*) n, SUM(size) s FROM files WHERE missing=0 "
                          "GROUP BY ext ORDER BY n DESC").fetchall()
        md += ["## Most frequent extensions", table(
            ["ext", "family", "files", "% files", "size"],
            [(e["ext"] or "(none)", e["family"], e["n"], pct(e["n"], N), human(e["s"])) for e in exts[:top]]), ""]
        md += ["## Heaviest extensions", table(
            ["ext", "files", "size", "% size"],
            [(e["ext"] or "(none)", e["n"], human(e["s"]), pct(e["s"], S))
             for e in sorted(exts, key=lambda e: -e["s"])[:top]]), ""]

        tops = {}
        for r in db.execute("SELECT path, size FROM files WHERE missing=0"):
            k = r["path"].split("/")[0] if "/" in r["path"] else "(source root)"
            n, s = tops.get(k, (0, 0))
            tops[k] = (n + 1, s + r["size"])
        md += ["## Top-level folders by size", table(
            ["folder", "files", "size", "% size"],
            [(k, n, human(s), pct(s, S)) for k, (n, s) in sorted(tops.items(), key=lambda x: -x[1][1])[:top]]), ""]

        kinds = db.execute("SELECT kind, COUNT(*) n, SUM(file_count) f, SUM(size) s FROM units "
                           "GROUP BY kind ORDER BY s DESC").fetchall()
        if kinds:
            md += ["## Units (handled as a whole)", table(
                ["kind", "units", "files", "size"], [(k["kind"], k["n"], k["f"], human(k["s"])) for k in kinds]), ""]
            big = db.execute("SELECT root, kind, file_count, size FROM units ORDER BY size DESC LIMIT ?", (top,))
            md += ["### Largest units", table(["unit", "kind", "files", "size"],
                                              [(u["root"], u["kind"], u["file_count"], human(u["size"])) for u in big]),
                   ""]

        big = db.execute("SELECT path, size FROM files WHERE missing=0 AND unit_id IS NULL "
                         "ORDER BY size DESC LIMIT ?", (top,))
        md += ["## Largest standalone files", table(["file", "size"], [(b["path"], human(b["size"])) for b in big]), ""]

        g = db.execute("SELECT kind, COUNT(*) groups, SUM(members-1) extra, SUM(wasted) w FROM dup_groups "
                       "GROUP BY kind").fetchall()
        if g:
            md += ["## Duplicates", table(
                ["kind", "groups", "redundant copies", "wasted", "% of size"],
                [(r["kind"], r["groups"], r["extra"], human(r["w"]), pct(r["w"], S)) for r in g])]
            fg = next((r for r in g if r["kind"] == "file"), None)
            if fg:
                md += ["", f"Duplicate ratio (standalone files): **{pct(fg['extra'], N)} of files**, "
                           f"**{pct(fg['w'], S)} of size**."]
            fam = db.execute("""SELECT f.family, COUNT(*) n, SUM(f.size) s FROM dup_members m
                                JOIN files f ON f.id=m.item_id WHERE m.item_type='file' AND m.role='remove'
                                GROUP BY f.family ORDER BY s DESC""").fetchall()
            if fam:
                md += ["", "### Redundant copies by family", table(
                    ["family", "copies", "wasted"], [(r["family"], r["n"], human(r["s"])) for r in fam])]
            md.append("")

        ctx.reports.mkdir(parents=True, exist_ok=True)
        (ctx.reports / "profile.md").write_text("\n".join(md), encoding="utf-8")
        with open(ctx.reports / "extensions.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ext", "family", "files", "bytes"])
            for e in exts:
                w.writerow([e["ext"] or "(none)", e["family"], e["n"], e["s"]])

        print(f"profile: {N} files, {human(S)}")
        for f in fams[:8]:
            print(f"  {f['family']:12} {f['n']:7} {pct(f['n'], N):>6}  {human(f['s']):>10}")
        print(f"  report: {ctx.reports / 'profile.md'}")


if __name__ == "__main__":
    main()
