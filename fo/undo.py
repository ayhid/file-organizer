#!/usr/bin/env python3
"""Reverse an apply run, newest operation first.

  undo.py --list          show apply runs
  undo.py --run 12 --yes  reverse run 12
  undo.py --last --yes    reverse the most recent apply run
Dry run unless --yes.

  move    -> moved back (a copy left by a cross-volume move is removed instead)
  trash   -> moved back out of the Trash (items trashed through Finder: use Put Back)
  install -> the installed font is removed, unless it was already installed before the run
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fonts  # noqa: E402
from common import Run, move_path, setup  # noqa: E402


def add_args(ap):
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", type=int)
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--yes", action="store_true", help="really reverse (default: dry run)")


def reverse(o) -> None:
    """Undo one operation or raise OSError."""
    if o["op"] == "install":
        if o["error"] == "preexisting":
            return  # was installed before this run: leave it
        src_hash = fonts.digest(o["src"]) if os.path.exists(o["src"]) else None
        fonts.uninstall(o["dst"], src_hash)
        return
    if o["op"] == "trash" and o["dst"].startswith("finder:"):
        raise OSError("trashed through Finder: restore it with Put Back in the Trash")
    if o["status"] == "copied":
        # the original never left: only the copy has to go
        if os.path.isdir(o["dst"]) and not os.path.islink(o["dst"]):
            shutil.rmtree(o["dst"])
        elif os.path.lexists(o["dst"]):
            os.remove(o["dst"])
        return
    status, err = move_path(o["dst"], o["src"])
    if status != "done":
        raise OSError(err)


def main():
    ctx = setup(__doc__, add_args)
    db, a = ctx.db, ctx.args

    if a.list or not (a.run or a.last):
        for r in db.execute("""SELECT r.id, r.started_at, r.status, r.notes,
                                      SUM(o.status IN ('done','copied')) live, SUM(o.status='undone') undone
                               FROM runs r LEFT JOIN ops o ON o.run_id=r.id WHERE r.script='apply'
                               GROUP BY r.id ORDER BY r.id DESC"""):
            print(f"run {r['id']:4}  {r['started_at']}  {r['status']:8} live={r['live'] or 0} "
                  f"undone={r['undone'] or 0}  {r['notes'] or ''}")
        return

    run_id = a.run or db.execute("SELECT MAX(id) FROM runs WHERE script='apply'").fetchone()[0]
    ops = db.execute("SELECT * FROM ops WHERE run_id=? AND status IN ('done','copied') ORDER BY id DESC",
                     (run_id,)).fetchall()
    if not a.yes:
        print(f"DRY RUN: {len(ops)} operations of run {run_id} would be reversed")
        for o in ops[:15]:
            what = "uninstall" if o["op"] == "install" else "move back"
            print(f"  {what:9} {o['dst']}\n            -> {o['src']}")
        print("Run again with --yes to execute.")
        return

    with Run(ctx, "undo") as run:
        ok = failed = 0
        for o in ops:
            try:
                reverse(o)
            except OSError as e:
                print(f"  failed: {o['op']} {o['dst']}: {e}")
                failed += 1
                continue
            db.execute("UPDATE ops SET status='undone' WHERE id=?", (o["id"],))
            if o["plan_id"]:
                db.execute("UPDATE plan SET status='pending' WHERE id=?", (o["plan_id"],))
            db.commit()
            ok += 1
        db.execute("""UPDATE files SET missing=0 WHERE path IN (
                        SELECT p.src FROM plan p JOIN ops o ON o.plan_id=p.id
                        WHERE o.run_id=? AND o.status='undone' AND p.item_type='file')""", (run_id,))
        db.commit()
        run.status = "ok" if not failed else "partial"
        run.notes = f"undone={ok} failed={failed} of_run={run_id}"
        print(f"undo run {run_id}: {ok} operations reversed, {failed} failed")


if __name__ == "__main__":
    main()
