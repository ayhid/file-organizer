#!/usr/bin/env python3
"""Step 9. Execute the plan. DRY RUN unless --apply is given.

  move    -> <target>/<target_dir>/<new_name>
  delete  -> <target>/_to_delete/<original path>   (nothing is ever really deleted here)
  install -> every installable font of the file/folder is copied to [fonts].install_dir and
             verified (byte-identical + valid font header); only when ALL succeeded is the
             original moved to the Trash. Any failure rolls back the fonts installed by this row.
  skip    -> left where it is

Rows flagged needs_review are skipped unless --include-review.
Every operation is logged in `ops` BEFORE it happens, so undo.py can always reverse a run.
"""
import collections as C
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fonts  # noqa: E402
from common import Run, human, move_path, now_iso, setup  # noqa: E402

ACTIONS = ("move", "delete", "install")


def add_args(ap):
    ap.add_argument("--apply", action="store_true", help="really execute (default: dry run)")
    ap.add_argument("--include-review", action="store_true", help="also apply rows flagged needs_review")
    ap.add_argument("--only", choices=list(ACTIONS), help="apply one action type only")
    ap.add_argument("--limit", type=int, default=0, help="apply at most N rows (0 = all)")
    ap.add_argument("--prune-empty", action="store_true", help="remove folders left empty in the source")


class OpLog:
    """Writes `ops` rows: logged as pending before acting, then updated with the outcome."""

    def __init__(self, db, run_id):
        self.db, self.run_id = db, run_id

    def start(self, plan_id, op, src, dst=""):
        oid = self.db.execute("INSERT INTO ops(run_id,plan_id,op,src,dst,at,status) VALUES(?,?,?,?,?,?,'pending')",
                              (self.run_id, plan_id, op, src, dst, now_iso())).lastrowid
        self.db.commit()
        return oid

    def end(self, oid, status, error=None, dst=None):
        self.db.execute("UPDATE ops SET status=?, error=COALESCE(?, error), dst=COALESCE(?, dst) WHERE id=?",
                        (status, error, dst, oid))
        self.db.commit()


def main():
    ctx = setup(__doc__, add_args)
    db, a = ctx.db, ctx.args
    if not ctx.source or not ctx.target:
        sys.exit("source and target must be set (rules.toml [paths] or --source/--target)")
    src_root, dst_root = str(ctx.source), str(ctx.target)
    if dst_root.startswith(src_root + os.sep):
        print("warning: target is inside the source; add it to [inventory].ignore_paths")
    fcfg = fonts.settings(ctx.cfg)

    where = ["status='pending'", f"action IN {ACTIONS}", "new_name IS NOT NULL", "target_dir IS NOT NULL"]
    if not a.include_review:
        where.append("needs_review=0")
    if a.only:
        where.append(f"action='{a.only}'")
    rows = db.execute(f"SELECT * FROM plan WHERE {' AND '.join(where)} ORDER BY action DESC, src").fetchall()
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
            label = "fonts installed, then Trash" if act == "install" else top
            print(f"  {act:7} {label:28} {n}")
        if any(r["action"] == "install" for r in rows):
            print(f"  font folder: {fcfg['install_dir']} | trash: {fcfg['trash_dir']} ({fcfg['trash_method']})")
        for r in rows[:15]:
            dest = "install + Trash" if r["action"] == "install" else f"{r['target_dir']}/{r['new_name']}"
            print(f"  {r['action']:7} {r['src']}\n          -> {dest}")
        print("Run again with --apply to execute.")
        return

    with Run(ctx, "apply") as run:
        ops, stats = OpLog(db, run.id), C.Counter()
        for r in rows:
            src = os.path.join(src_root, r["src"])
            if not os.path.lexists(src):
                db.execute("UPDATE plan SET status='error', reason=COALESCE(reason||'; ','')||'source missing' "
                           "WHERE id=?", (r["id"],))
                stats["missing"] += 1
                continue

            if r["action"] == "install":
                status, err = install_row(r["id"], src, fcfg, ops)
            else:
                dst = os.path.join(dst_root, r["target_dir"], r["new_name"])
                oid = ops.start(r["id"], "move", src, dst)
                try:
                    status, err = move_path(src, dst)
                except OSError as e:
                    status, err = "error", str(e)
                ops.end(oid, status, err)

            db.execute("UPDATE plan SET status=? WHERE id=?", (status, r["id"]))
            db.commit()
            stats[r["action"] if status == "done" else f"{r['action']}:{status}"] += 1
            if status != "done":
                print(f"  {r['action']} {status}: {r['src']}: {err}")

        if a.prune_empty:
            stats["pruned"] = _prune(src_root)
        db.execute("UPDATE files SET missing=1 WHERE path IN (SELECT src FROM plan WHERE item_type='file' "
                   "AND status='done')")
        db.commit()
        failed = sum(v for k, v in stats.items() if ":" in k)
        run.status = "ok" if not failed else "partial"
        run.notes = " ".join(f"{k}={v}" for k, v in stats.items())
        print(f"apply (run {run.id}): " + ", ".join(f"{k} {v}" for k, v in stats.items()))
        if any(k.endswith(":copied") for k in stats):
            print("  Some sources could not be removed after copying (deleting is not allowed there). "
                  "The copies are in place; remove the originals once you have checked them.")
        print(f"  undo with: python3 fo/undo.py --run {run.id} --yes")


def install_row(plan_id, src, fcfg, ops: OpLog):
    """Install every font of a file or folder, verify each one, roll back on any failure,
    and only then move the original to the Trash."""
    found = fonts.fonts_in(src, fcfg["installable"])
    if not found:
        return "error", "no installable font found"
    installed = []  # (op_id, path, hash, already_there)
    oid = None
    try:
        for font in found:
            oid = ops.start(plan_id, "install", font,
                            os.path.join(str(fcfg["install_dir"]), os.path.basename(font)))
            path, already = fonts.install(font, fcfg["install_dir"])
            ops.end(oid, "done", "preexisting" if already else None)
            installed.append((oid, path, fonts.digest(path), already))
            oid = None
    except (OSError, fonts.FontError) as e:
        if oid is not None:
            ops.end(oid, "error", str(e))
        for op_id, path, h, already in installed:
            if not already:
                fonts.uninstall(path, h)
            ops.end(op_id, "undone", "rolled back")
        return "error", f"install failed, nothing trashed: {e}"

    oid = ops.start(plan_id, "trash", src)
    try:
        where = fonts.trash(src, fcfg)
    except OSError as e:
        ops.end(oid, "error", str(e))
        return "error", f"{len(installed)} font(s) installed but the original could not be trashed: {e}"
    ops.end(oid, "done", dst=where)
    return "done", None


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
