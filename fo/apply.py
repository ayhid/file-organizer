#!/usr/bin/env python3
"""Step 9. Execute the plan. DRY RUN unless --apply is given.

  move   -> <target>/<target_dir>/<new_name>
  delete -> <target>/_to_delete/<original path>   (nothing is ever really deleted here)
  skip   -> left where it is

Rows flagged needs_review are skipped unless --include-review.
Every operation is logged in `ops` BEFORE it happens, so undo.py can always reverse a run.
"""
import collections as C
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Run, human, move_path, now_iso, setup  # noqa: E402


def add_args(ap):
    ap.add_argument("--apply", action="store_true", help="really move files (default: dry run)")
    ap.add_argument("--include-review", action="store_true", help="also apply rows flagged needs_review")
    ap.add_argument("--only", choices=["move", "delete"], help="apply one action type only")
    ap.add_argument("--limit", type=int, default=0, help="apply at most N rows (0 = all)")
    ap.add_argument("--prune-empty", action="store_true", help="remove folders left empty in the source")


def main():
    ctx = setup(__doc__, add_args)
    db, a = ctx.db, ctx.args
    if not ctx.source or not ctx.target:
        sys.exit("source and target must be set (rules.toml [paths] or --source/--target)")
    src_root, dst_root = str(ctx.source), str(ctx.target)
    if dst_root.startswith(src_root + os.sep):
        print("warning: target is inside the source; add it to [inventory].ignore_paths")

    where = ["status='pending'", "action IN ('move','delete')", "new_name IS NOT NULL", "target_dir IS NOT NULL"]
    if not a.include_review:
        where.append("needs_review=0")
    if a.only:
        where.append(f"action='{a.only}'")
    sql = f"SELECT * FROM plan WHERE {' AND '.join(where)} ORDER BY action DESC, src"
    rows = db.execute(sql).fetchall()
    if a.limit:
        rows = rows[: a.limit]
    held = db.execute("SELECT COUNT(*) FROM plan WHERE status='pending' AND needs_review=1").fetchone()[0]

    sizes = {}
    for r in db.execute("SELECT 'file' t, id, size FROM files UNION ALL SELECT 'unit', id, size FROM units"):
        sizes[(r["t"], r["id"])] = r["size"]

    if not a.apply:
        by = C.Counter((r["action"], (r["target_dir"] or "").split("/")[0]) for r in rows)
        vol = sum(sizes.get((r["item_type"], r["item_id"]), 0) for r in rows)
        print(f"DRY RUN: {len(rows)} operations, {human(vol)}  ({held} rows held for review)")
        for (act, top), n in sorted(by.items(), key=lambda x: -x[1]):
            print(f"  {act:6} {top:22} {n}")
        for r in rows[:15]:
            print(f"  {r['action']:6} {r['src']}\n         -> {r['target_dir']}/{r['new_name']}")
        print("Run again with --apply to execute.")
        return

    with Run(ctx, "apply") as run:
        stats = C.Counter()
        for r in rows:
            src = os.path.join(src_root, r["src"])
            dst = os.path.join(dst_root, r["target_dir"], r["new_name"])
            if not os.path.lexists(src):
                db.execute("UPDATE plan SET status='error', reason=COALESCE(reason||'; ','')||'source missing' "
                           "WHERE id=?", (r["id"],))
                stats["missing"] += 1
                continue
            op = db.execute("INSERT INTO ops(run_id,plan_id,op,src,dst,at,status) VALUES(?,?,?,?,?,?,'pending')",
                            (run.id, r["id"], "move", src, dst, now_iso())).lastrowid
            db.commit()
            try:
                status, err = move_path(src, dst)
            except OSError as e:
                status, err = "error", str(e)
            db.execute("UPDATE ops SET status=?, error=? WHERE id=?", (status, err, op))
            db.execute("UPDATE plan SET status=? WHERE id=?", ("done" if status == "done" else status, r["id"]))
            db.commit()
            stats[status] += 1
            if status != "done":
                print(f"  {status}: {r['src']}: {err}")

        if a.prune_empty:
            stats["pruned"] = _prune(src_root)
        db.execute("UPDATE files SET missing=1 WHERE path IN (SELECT src FROM plan WHERE item_type='file' "
                   "AND status='done')")
        db.commit()
        run.status = "ok" if not (stats["error"] or stats["copied"]) else "partial"
        run.notes = " ".join(f"{k}={v}" for k, v in stats.items())
        print(f"apply (run {run.id}): " + ", ".join(f"{k} {v}" for k, v in stats.items()))
        if stats["copied"]:
            print("  Some sources could not be removed after copying (deleting is not allowed there). "
                  "The copies are in place; remove the originals once you have checked them.")
        print(f"  undo with: python3 fo/undo.py --run {run.id}")


def _prune(root: str) -> int:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        try:
            leftovers = [f for f in os.listdir(dirpath) if f not in (".DS_Store",)]
            if not leftovers:
                for f in os.listdir(dirpath):
                    os.remove(os.path.join(dirpath, f))
                os.rmdir(dirpath)
                n += 1
        except OSError:
            pass
    return n


if __name__ == "__main__":
    main()
