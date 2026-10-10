#!/usr/bin/env python3
"""Step 6. Decide where every standalone file and every unit goes: fills the `plan` table.

  * redundant copies (dup_members role=remove) -> action delete, target _to_delete/<original path>
  * everything else -> first matching [[classify.rules]] entry
  * dated rules look for a date in the name, then in the PDF text, then in the PDF metadata
  * meaningless names and rules with review=true are flagged needs_review

Rows edited by hand (locked=1) or already applied are never touched.
Exit code 3 means the time budget ran out (PDF reading): run again to continue.
"""
import datetime as dt
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Budget, Run, _fold, find_date, glob_any, now_iso, setup, slugify  # noqa: E402

INCOMPLETE = 3


class Rules:
    def __init__(self, cfg):
        self.rules = cfg.get("classify", {}).get("rules", [])
        for r in self.rules:
            r["_re"] = re.compile(r["name_re"], re.I) if r.get("name_re") else None
        self.clients = [(re.compile(c["pattern"], re.I), c["slug"]) for c in cfg.get("clients", [])]
        self.meaningless = [re.compile(p, re.I) for p in cfg.get("review", {}).get("meaningless_names", [])]

    def client_of(self, path: str):
        folded = _fold(path)
        for rx, slug in self.clients:
            if rx.search(folded):
                return slug
        return None

    def match(self, item: dict):
        """item: type, name, path, family, kind, ext. Returns (rule, client)."""
        folded = _fold(item["name"])
        for r in self.rules:
            if r.get("item") and r["item"] != item["type"]:
                continue
            if r.get("kind") and item["kind"] not in r["kind"]:
                continue
            if r.get("family") and item["family"] not in r["family"]:
                continue
            if r.get("ext") and item["ext"] not in [e.lower() for e in r["ext"]]:
                continue
            if r["_re"] and not (r["_re"].search(folded) or r["_re"].search(item["name"])):
                continue
            if r.get("path") and not glob_any(item["path"], r["path"]):
                continue
            client = None
            if r.get("client"):
                client = self.client_of(item["path"])
                if not client:
                    continue
            return r, client
        return None, None

    def is_meaningless(self, stem: str) -> bool:
        return any(rx.search(stem.strip()) for rx in self.meaningless)


class PdfDates:
    """Date from PDF text (near keywords) or metadata, cached in doc_dates by size+mtime."""

    def __init__(self, ctx):
        self.db, self.root = ctx.db, str(ctx.source)
        dcfg = ctx.cfg.get("dates", {})
        self.keywords = [k.lower() for k in dcfg.get("pdf_keywords", [])]
        self.use_meta = dcfg.get("use_pdf_metadata", True)
        self.min_year = int(dcfg.get("min_year", 1995))
        self.pdftotext = shutil.which("pdftotext")
        self.pdfinfo = shutil.which("pdfinfo")

    def get(self, f):
        row = self.db.execute("SELECT * FROM doc_dates WHERE file_id=? AND size=? AND mtime=?",
                              (f["id"], f["size"], f["mtime"])).fetchone()
        if row:
            return (row["date"], row["precision"], row["source"]) if row["date"] else None
        result = self._extract(os.path.join(self.root, f["path"]))
        self.db.execute("INSERT OR REPLACE INTO doc_dates(file_id,size,mtime,date,precision,source) VALUES(?,?,?,?,?,?)",
                        (f["id"], f["size"], f["mtime"], *(result or (None, None, "none"))))
        return result

    def _extract(self, path):
        if self.pdftotext:
            try:
                text = subprocess.run([self.pdftotext, "-f", "1", "-l", "2", "-q", path, "-"],
                                      capture_output=True, timeout=20).stdout.decode("utf-8", "replace")
            except (subprocess.TimeoutExpired, OSError):
                text = ""
            low = text.lower()
            for kw in self.keywords:
                i = low.find(kw)
                while i != -1:
                    hit = find_date(text[i: i + len(kw) + 60], min_year=self.min_year)
                    if hit:
                        return hit[0], hit[1], "pdf-text"
                    i = low.find(kw, i + 1)
            hit = find_date(text, min_year=self.min_year)
            if hit:
                return hit[0], hit[1], "pdf-text"
        if self.use_meta and self.pdfinfo:
            try:
                info = subprocess.run([self.pdfinfo, "-isodates", path], capture_output=True,
                                      timeout=20).stdout.decode("utf-8", "replace")
            except (subprocess.TimeoutExpired, OSError):
                info = ""
            m = re.search(r"^CreationDate:\s+(\d{4}-\d{2}-\d{2})", info, re.M)
            if m:
                return m.group(1), "day", "pdf-meta"
        return None


def main():
    ctx = setup(__doc__, lambda ap: ap.add_argument(
        "--budget", type=float, default=0, help="stop after N seconds (0 = no limit); rerun to resume"))
    db = ctx.db
    rules, pdf = Rules(ctx.cfg), PdfDates(ctx)
    budget = Budget(ctx.args.budget)
    min_year = int(ctx.cfg.get("dates", {}).get("min_year", 1995))

    with Run(ctx, "classify") as run:
        frozen = {(r["item_type"], r["item_id"]) for r in
                  db.execute("SELECT item_type, item_id FROM plan WHERE locked=1 OR status!='pending'")}
        removed = {(r["item_type"], r["item_id"]): r["reason"] for r in
                   db.execute("SELECT item_type, item_id, reason FROM dup_members WHERE role='remove'")}

        items = []
        for u in db.execute("""SELECT u.id, u.root, u.kind, (SELECT MIN(mtime) FROM files WHERE unit_id=u.id) mtime
                               FROM units u"""):
            name = u["root"].rsplit("/", 1)[-1]
            items.append({"type": "unit", "id": u["id"], "path": u["root"], "name": name, "kind": u["kind"],
                          "family": u["kind"], "ext": "", "mtime": u["mtime"] or 0, "size": 0})
        for f in db.execute("SELECT * FROM files WHERE missing=0 AND unit_id IS NULL"):
            items.append({"type": "file", "id": f["id"], "path": f["path"], "name": f["name"], "kind": "",
                          "family": f["family"], "ext": f["ext"], "mtime": f["mtime"], "size": f["size"]})

        counts, complete = {}, True
        for n, it in enumerate(items):
            key = (it["type"], it["id"])
            if key in frozen:
                continue
            if budget.exceeded():
                complete = False
                break
            row = _decide(it, removed.get(key), rules, pdf, min_year)
            counts[row["rule"]] = counts.get(row["rule"], 0) + 1
            db.execute("""
                INSERT INTO plan(item_type,item_id,src,family,rule,action,target_dir,dated,precision,
                                 date_found,date_source,needs_review,reason,updated_at)
                VALUES(:type,:id,:path,:family,:rule,:action,:target_dir,:dated,:precision,
                       :date_found,:date_source,:needs_review,:reason,:now)
                ON CONFLICT(item_type,item_id) DO UPDATE SET src=excluded.src, family=excluded.family,
                  rule=excluded.rule, action=excluded.action, target_dir=excluded.target_dir,
                  dated=excluded.dated, precision=excluded.precision, date_found=excluded.date_found,
                  date_source=excluded.date_source, needs_review=excluded.needs_review,
                  reason=excluded.reason, updated_at=excluded.updated_at
                WHERE plan.locked=0 AND plan.status='pending'""",
                       {**row, "type": it["type"], "id": it["id"], "path": it["path"], "now": now_iso()})
            if n % 200 == 0:
                db.commit()

        # plan rows whose item vanished
        db.execute("""DELETE FROM plan WHERE status='pending' AND locked=0 AND (
                        (item_type='file' AND item_id NOT IN (SELECT id FROM files WHERE missing=0 AND unit_id IS NULL))
                     OR (item_type='unit' AND item_id NOT IN (SELECT id FROM units)))""")
        db.commit()

        review = db.execute("SELECT COUNT(*) FROM plan WHERE needs_review=1 AND status='pending'").fetchone()[0]
        run.status = "ok" if complete else "partial"
        run.notes = f"classified={sum(counts.values())} review={review}"
        print(f"classify: {sum(counts.values())} items, {review} need review"
              + ("" if complete else " | budget reached, run again to resume"))
        for rule, k in sorted(counts.items(), key=lambda x: -x[1])[:25]:
            print(f"  {rule:18} {k}")
    sys.exit(0 if complete else INCOMPLETE)


def _decide(it, removed_reason, rules: Rules, pdf: PdfDates, min_year: int) -> dict:
    out = {"family": it["family"], "dated": 0, "precision": None, "date_found": None,
           "date_source": None, "needs_review": 0, "reason": None}
    if removed_reason:
        parent = os.path.dirname(it["path"])
        return {**out, "rule": "duplicate", "action": "delete",
                "target_dir": "_to_delete" + ("/" + parent if parent else ""), "reason": removed_reason}

    rule, client = rules.match(it)
    if rule is None:
        return {**out, "rule": "none", "action": "move", "target_dir": "_review", "needs_review": 1,
                "reason": "no rule matched"}
    reasons = [rule["reason"]] if rule.get("reason") else []
    review = bool(rule.get("review"))

    date = None
    if rule.get("dated"):
        precision = rule.get("precision", "day")
        out.update(dated=1, precision=precision)
        hit = find_date(os.path.splitext(it["name"])[0], allow_year=precision == "year", min_year=min_year)
        if hit:
            date = (hit[0], hit[1], "name")
        elif it["type"] == "file" and it["ext"] == ".pdf":
            date = pdf.get(it)
        if date:
            out.update(date_found=date[0], date_source=date[2])
        else:
            review = True
            reasons.append("no date found")

    stem = it["name"] if it["type"] == "unit" else os.path.splitext(it["name"])[0]
    if it["type"] == "file" and rules.is_meaningless(stem):
        review = True
        reasons.append("meaningless name")

    year = (date[0][:4] if date else str(dt.date.fromtimestamp(it["mtime"]).year) if it["mtime"] else "unknown")
    target = rule["target"].format(year=year, client=client or "", kind=slugify(it["kind"] or "other"),
                                   family=it["family"] or "other").strip("/")
    target = re.sub(r"/+", "/", target)
    return {**out, "rule": rule.get("name", "rule"), "action": "move", "target_dir": target,
            "needs_review": int(review), "reason": "; ".join(reasons) or None}


if __name__ == "__main__":
    main()
