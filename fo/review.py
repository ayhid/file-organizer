#!/usr/bin/env python3
"""Step 8. Review the plan before applying it.

  review.py summary                       counts per action, target folder and review flag
  review.py export [--only review|all]    write reports/plan.csv (edit it in any spreadsheet)
  review.py import [--file plan.csv]      read edits back; every edited row becomes locked=1

Editable columns: action (move | delete | skip | install), target_dir, new_name, needs_review.
Locked rows are never overwritten by classify.py or rename.py.
"""
import collections as C
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import now_iso, setup  # noqa: E402

COLUMNS = ["id", "item_type", "src", "action", "target_dir", "new_name", "needs_review", "reason",
           "rule", "family", "date_found", "date_source", "locked"]
EDITABLE = ["action", "target_dir", "new_name", "needs_review"]
ACTIONS = {"move", "delete", "skip", "install"}


def add_args(ap):
    ap.add_argument("command", choices=["summary", "export", "import"])
    ap.add_argument("--only", choices=["review", "all", "move", "delete"], default="all")
    ap.add_argument("--file", help="CSV path (default: reports/plan.csv)")


def main():
    ctx = setup(__doc__, add_args)
    db, a = ctx.db, ctx.args
    path = a.file or str(ctx.reports / "plan.csv")

    if a.command == "summary":
        rows = db.execute("SELECT action, target_dir, needs_review FROM plan WHERE status='pending'").fetchall()
        by_action = C.Counter(r["action"] for r in rows)
        by_top = C.Counter((r["target_dir"] or "").split("/")[0] for r in rows if r["action"] == "move")
        print(f"plan: {len(rows)} pending items | " + ", ".join(f"{k} {v}" for k, v in by_action.items()))
        print(f"  needs review: {sum(r['needs_review'] for r in rows)}")
        for k, v in by_top.most_common():
            print(f"  {k:20} {v}")
        return

    if a.command == "export":
        where = {"all": "1=1", "review": "needs_review=1", "move": "action='move'", "delete": "action='delete'"}[a.only]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            n = 0
            for r in db.execute(f"SELECT {', '.join(COLUMNS)} FROM plan WHERE status='pending' AND {where} "
                                "ORDER BY needs_review DESC, target_dir, src"):
                w.writerow([r[c] for c in COLUMNS])
                n += 1
        print(f"export: {n} rows -> {path}")
        return

    # import
    errors, changed = [], 0
    with open(path, newline="", encoding="utf-8") as fh:
        for line, row in enumerate(csv.DictReader(fh), start=2):
            cur = db.execute("SELECT * FROM plan WHERE id=?", (row.get("id"),)).fetchone()
            if cur is None or cur["status"] != "pending":
                continue
            new = {c: (row.get(c) or "").strip() for c in EDITABLE}
            new["needs_review"] = 1 if new["needs_review"] in ("1", "true", "yes", "x") else 0
            if new["action"] not in ACTIONS:
                errors.append(f"line {line}: action must be one of {sorted(ACTIONS)}")
                continue
            td = new["target_dir"]
            if td.startswith("/") or ".." in td.split("/"):
                errors.append(f"line {line}: target_dir must stay inside the target root")
                continue
            if "/" in new["new_name"] or new["new_name"] in ("", ".", ".."):
                errors.append(f"line {line}: new_name must be a plain name")
                continue
            if any(str(cur[c] if cur[c] is not None else "") != str(new[c]) for c in EDITABLE):
                db.execute("""UPDATE plan SET action=?, target_dir=?, new_name=?, needs_review=?, locked=1,
                              updated_at=? WHERE id=?""",
                           (new["action"], td.strip("/"), new["new_name"], new["needs_review"], now_iso(), cur["id"]))
                changed += 1
    db.commit()
    print(f"import: {changed} rows updated and locked")
    for e in errors[:20]:
        print("  " + e)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
