#!/usr/bin/env python3
"""Step 1. Walk the source folder and record every file in the `files` table.

Incremental: unchanged files keep their row (and their hashes); files that disappeared
are flagged missing=1. Unreadable folders are logged in `scan_errors`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Run, ext_of, family_of, glob_any, human, meta_set, nfc, setup  # noqa: E402


def main():
    ctx = setup(__doc__)
    if not ctx.source:
        sys.exit("No source folder: pass --source or set [paths].source in rules.toml")
    if not ctx.source.is_dir():
        sys.exit(f"Source folder not found: {ctx.source}")
    ignore = ctx.cfg.get("inventory", {}).get("ignore_names", [".DS_Store", "Icon\r", "Thumbs.db", "desktop.ini"])
    ignore_paths = ctx.cfg.get("inventory", {}).get("ignore_paths", [])
    db = ctx.db

    with Run(ctx, "inventory") as run:
        meta_set(db, "source", str(ctx.source))
        root = str(ctx.source)
        errors = []

        def onerror(e: OSError):
            errors.append((getattr(e, "filename", "?"), str(e.strerror or e)))

        existing = {r["path"]: (r["size"], r["mtime"]) for r in db.execute("SELECT path,size,mtime FROM files")}
        batch, seen, n_new, n_changed = [], 0, 0, 0
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
            rel_dir = nfc(os.path.relpath(dirpath, root))
            rel_dir = "" if rel_dir == "." else rel_dir
            # never descend into our own output folders if they live inside the source
            dirnames[:] = [d for d in dirnames
                           if not glob_any(os.path.join(rel_dir, nfc(d)), ignore_paths)]
            for fn in filenames:
                if fn in ignore or fn.startswith("._"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = nfc(os.path.join(rel_dir, fn)) if rel_dir else nfc(fn)
                if glob_any(rel, ignore_paths):
                    continue
                try:
                    st = os.lstat(full)
                except OSError as e:
                    errors.append((full, str(e)))
                    continue
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                name = nfc(fn)
                ext = ext_of(name)
                prev = existing.get(rel)
                if prev is None:
                    n_new += 1
                elif prev != (st.st_size, st.st_mtime):
                    n_changed += 1
                batch.append((rel, name, ext, family_of(ext, ctx.families), st.st_size, st.st_mtime, run.id))
                seen += 1
                if len(batch) >= 500:
                    _flush(db, batch)
                    batch = []
        _flush(db, batch)

        missing = db.execute("UPDATE files SET missing=1 WHERE seen_run IS NOT ? AND missing=0", (run.id,)).rowcount
        db.executemany("INSERT INTO scan_errors(run_id,path,error) VALUES(?,?,?)",
                       [(run.id, p, e) for p, e in errors])
        db.commit()

        total = db.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files WHERE missing=0").fetchone()
        run.notes = f"seen={seen} new={n_new} changed={n_changed} missing={missing} errors={len(errors)}"
        print(f"inventory: {total['n']} files, {human(total['s'])} | new {n_new}, changed {n_changed}, "
              f"gone {missing}, unreadable {len(errors)}")
        for p, e in errors[:10]:
            print(f"  unreadable: {p} ({e})")


def _flush(db, batch):
    db.executemany(
        """INSERT INTO files(path,name,ext,family,size,mtime,seen_run,missing) VALUES(?,?,?,?,?,?,?,0)
           ON CONFLICT(path) DO UPDATE SET name=excluded.name, ext=excluded.ext, family=excluded.family,
             size=excluded.size, mtime=excluded.mtime, seen_run=excluded.seen_run, missing=0""",
        batch)
    db.commit()


if __name__ == "__main__":
    main()
