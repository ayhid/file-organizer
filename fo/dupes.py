#!/usr/bin/env python3
"""Step 4. Build duplicate groups and pick the copy to keep.

  file : standalone files with the same size + full hash
  unit : units with the same content (same relative paths and file hashes)
  zip  : a standalone .zip whose every member exists, same size and CRC, in an extracted folder

Keeper rule ("best-classified path"), first difference wins:
  1. not matching [dupes].avoid
  2. earliest match in [dupes].prefer
  3. inside a folder rather than loose at the source root
  4. name without a copy suffix like " (1)", " 2", "copy"
  5. oldest modification time
  6. shortest path
Writes dup_groups / dup_members and reports/dupes.csv.
"""
from __future__ import annotations
import collections as C
import csv
import hashlib
import os
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (Run, glob_any, human, is_copy_name, setup, slugify,  # noqa: E402
                    stem_of, strip_copy_suffix)


def keeper_key(path, mtime, dcfg):
    prefer = dcfg.get("prefer", [])
    rank = next((i for i, p in enumerate(prefer) if glob_any(path, [p])), len(prefer))
    return (glob_any(path, dcfg.get("avoid", [])), rank, "/" not in path,
            is_copy_name(os.path.basename(path)), mtime, len(path), path)


def norm(name: str) -> str:
    return slugify(strip_copy_suffix(stem_of(name)))


def crc_of(path: str) -> int:
    crc = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(block, crc)
    return crc & 0xFFFFFFFF


def main():
    ctx = setup(__doc__, lambda ap: ap.add_argument(
        "--no-crc", action="store_true", help="zip check on names and sizes only (faster, less strict)"))
    db, dcfg = ctx.db, ctx.cfg.get("dupes", {})
    root = str(ctx.source)

    with Run(ctx, "dupes") as run:
        db.execute("DELETE FROM dup_members")
        db.execute("DELETE FROM dup_groups")
        removed_files: set[int] = set()

        def add_group(kind, key, size, members, reasons, keep_reason=None, presorted=False):
            """members: list of (item_type, item_id, sort_key); the first one (after sorting) is kept."""
            if not presorted:
                members = sorted(members, key=lambda m: m[2])
            gid = db.execute("INSERT INTO dup_groups(kind,key,size,members,wasted) VALUES(?,?,?,?,?)",
                             (kind, key, size, len(members), size * (len(members) - 1))).lastrowid
            for i, (itype, iid, _) in enumerate(members):
                db.execute("INSERT INTO dup_members(group_id,item_type,item_id,role,reason) VALUES(?,?,?,?,?)",
                           (gid, itype, iid, "keep" if i == 0 else "remove", keep_reason if i == 0 else reasons))
                if i and itype == "file":
                    removed_files.add(iid)

        # --- file duplicates (standalone only)
        groups = C.defaultdict(list)
        for r in db.execute("""SELECT f.id, f.path, f.size, f.mtime, h.full FROM files f JOIN hashes h ON h.file_id=f.id
                               WHERE f.missing=0 AND f.unit_id IS NULL AND h.full IS NOT NULL AND h.full!='ERR'"""):
            groups[(r["size"], r["full"])].append(r)
        n_file = 0
        for (size, full), rows in groups.items():
            if len(rows) > 1:
                add_group("file", full, size,
                          [("file", r["id"], keeper_key(r["path"], r["mtime"], dcfg)) for r in rows],
                          "identical content")
                n_file += 1

        # --- unit duplicates (same relative paths + same file hashes)
        n_unit = 0
        sig_groups = C.defaultdict(list)
        for u in db.execute("SELECT id, root, size, signature FROM units"):
            sig_groups[u["signature"]].append(u)
        for sig, units in sig_groups.items():
            if len(units) < 2:
                continue
            content = C.defaultdict(list)
            for u in units:
                rows = db.execute("""SELECT f.path, h.full, f.mtime FROM files f LEFT JOIN hashes h ON h.file_id=f.id
                                     WHERE f.unit_id=? AND f.missing=0 ORDER BY f.path""", (u["id"],)).fetchall()
                if any(r["full"] in (None, "ERR") for r in rows):
                    continue  # not fully hashed: never claim a duplicate we have not proven
                h = hashlib.blake2b(digest_size=16)
                for r in rows:
                    h.update(f"{r['path'][len(u['root']) + 1:]}\0{r['full']}\n".encode())
                oldest = min((r["mtime"] for r in rows), default=0)
                content[h.hexdigest()].append((u, oldest))
            for key, items in content.items():
                if len(items) > 1:
                    add_group("unit", key, items[0][0]["size"],
                              [("unit", u["id"], keeper_key(u["root"], mt, dcfg)) for u, mt in items],
                              "identical folder")
                    n_unit += 1

        # --- zips that duplicate an extracted folder
        n_zip = 0
        dirs = C.defaultdict(list)  # normalised dir name -> dir paths
        files_by_dir = C.defaultdict(dict)  # dir path -> {relpath: (size, abs)}
        for r in db.execute("SELECT path, size FROM files WHERE missing=0"):
            parts = r["path"].split("/")
            for k in range(1, len(parts)):
                d = "/".join(parts[:k])
                files_by_dir[d]["/".join(parts[k:])] = r["size"]
        for d in files_by_dir:
            dirs[norm(d.rsplit("/", 1)[-1] + ".x")].append(d)

        for z in db.execute("""SELECT id, path, size, mtime FROM files
                               WHERE missing=0 AND unit_id IS NULL AND ext='.zip'""").fetchall():
            if z["id"] in removed_files:
                continue
            try:
                with zipfile.ZipFile(os.path.join(root, z["path"])) as zf:
                    members = [i for i in zf.infolist() if not i.is_dir()
                               and not i.filename.startswith("__MACOSX/")
                               and os.path.basename(i.filename) not in (".DS_Store",)]
            except (zipfile.BadZipFile, OSError, NotImplementedError):
                continue
            if not members:
                continue
            tops = {m.filename.split("/")[0] for m in members}
            zkey = norm(os.path.basename(z["path"]))
            names = {zkey} | ({norm(next(iter(tops)) + ".x")} if len(tops) == 1 and len(members) > 1 else set())
            candidates = {d for n in names for d in dirs.get(n, [])}
            candidates |= {d for n, ds in dirs.items() if len(n) >= 4 and zkey.startswith(n + "-") for d in ds}
            for d in sorted(candidates, key=len):
                if d == os.path.dirname(z["path"]) or z["path"].startswith(d + "/"):
                    continue  # the zip's own parent folder is not an extracted copy of it
                if _zip_matches(members, files_by_dir[d], os.path.join(root, d), ctx.args.no_crc):
                    add_group("zip", z["path"], z["size"], [("dir", 0, None), ("file", z["id"], None)],
                              f"already extracted in {d}", keep_reason=d, presorted=True)
                    n_zip += 1
                    break
        db.commit()

        # --- report
        ctx.reports.mkdir(parents=True, exist_ok=True)
        out = ctx.reports / "dupes.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["group", "kind", "role", "path", "size", "reason"])
            for r in db.execute("""
                SELECT g.id, g.kind, m.role, m.reason, g.size,
                       CASE m.item_type WHEN 'file' THEN (SELECT path FROM files WHERE id=m.item_id)
                                        WHEN 'unit' THEN (SELECT root FROM units WHERE id=m.item_id)
                                        ELSE m.reason END AS path
                FROM dup_groups g JOIN dup_members m ON m.group_id=g.id
                ORDER BY g.wasted DESC, g.id, m.role"""):
                w.writerow([r["id"], r["kind"], r["role"], r["path"], r["size"],
                            r["reason"] if r["role"] == "remove" else ""])

        tot = db.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files WHERE missing=0").fetchone()
        g = db.execute("SELECT COALESCE(SUM(members-1),0) extra, COALESCE(SUM(wasted),0) w FROM dup_groups "
                       "WHERE kind='file'").fetchone()
        allw = db.execute("SELECT COALESCE(SUM(wasted),0) w FROM dup_groups").fetchone()["w"]
        run.notes = f"file_groups={n_file} unit_groups={n_unit} zips={n_zip} wasted={allw}"
        print(f"dupes: {n_file} file groups, {n_unit} unit groups, {n_zip} redundant zips")
        if tot["n"]:
            print(f"  standalone copies: {g['extra']} files = {g['extra'] / tot['n'] * 100:.1f}% of files, "
                  f"{human(g['w'])}")
            print(f"  all redundancy:    {human(allw)} = {allw / max(tot['s'], 1) * 100:.1f}% of size")
        print(f"  report: {out}")


def _zip_matches(members, disk: dict, disk_root: str, no_crc: bool) -> bool:
    """Every zip member must exist under disk_root (with or without the zip's top folder), same size (+CRC)."""
    for m in members:
        rel = m.filename
        candidates = [rel] + ([rel.split("/", 1)[1]] if "/" in rel else [])
        hit = next((c for c in candidates if disk.get(c) == m.file_size), None)
        if hit is None:
            return False
        if not no_crc:
            try:
                if crc_of(os.path.join(disk_root, hit)) != m.CRC:
                    return False
            except OSError:
                return False
    return True


if __name__ == "__main__":
    main()
