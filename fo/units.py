#!/usr/bin/env python3
"""Step 2. Detect "units": folders that must be handled as a whole (projects, app bundles,
font families, embroidery sets, 3D kits, downloaded folders...).

A file belongs to the OUTERMOST of:
  * a project root (folder holding a marker file such as package.json, or a .git / *.xcodeproj dir)
  * a bundle dir (*.app, *.framework...)
  * the first ancestor folder that is NOT listed in [units].collections
Files whose ancestors are all collections (or that sit at the source root) stay standalone.
Files inside a unit are never hashed individually, deduplicated or renamed.
"""
from __future__ import annotations
import collections as C
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Run, glob_any, human, setup  # noqa: E402


def match_kind(name: str, table: dict) -> str | None:
    low = name.lower()
    for pattern, kind in table.items():
        if glob_any(low, [pattern]):
            return kind
    return None


def main():
    ctx = setup(__doc__)
    db, ucfg = ctx.db, ctx.cfg.get("units", {})
    marker_files = ucfg.get("marker_files", {})
    marker_dirs = ucfg.get("marker_dirs", {})
    bundles = ucfg.get("bundles", {})
    collections = ucfg.get("collections", [])
    threshold = float(ucfg.get("family_threshold", 0.6))

    with Run(ctx, "units") as run:
        rows = db.execute("SELECT id, path, family, size FROM files WHERE missing=0").fetchall()

        # 1. project / bundle roots found from file paths
        marker_roots: dict[str, str] = {}
        for r in rows:
            parts = r["path"].split("/")
            for i, comp in enumerate(parts[:-1]):
                k = match_kind(comp, bundles)
                if k:  # the bundle dir itself is the root
                    _keep_outer(marker_roots, "/".join(parts[: i + 1]), k)
                    break
                k = match_kind(comp, marker_dirs)
                if k and i > 0:  # .git, *.xcodeproj -> their parent is the root
                    _keep_outer(marker_roots, "/".join(parts[:i]), k)
                    break
            k = match_kind(parts[-1], marker_files)
            if k and len(parts) > 1:
                _keep_outer(marker_roots, "/".join(parts[:-1]), k)

        # 2. assign each file to its outermost unit root
        assign: dict[int, str | None] = {}
        for r in rows:
            parts = r["path"].split("/")
            root = None
            for k in range(1, len(parts)):
                prefix = "/".join(parts[:k])
                if prefix in marker_roots or not glob_any(prefix, collections):
                    root = prefix
                    break
            assign[r["id"]] = root

        # 3. describe units
        by_root: dict[str, list] = C.defaultdict(list)
        for r in rows:
            if assign[r["id"]]:
                by_root[assign[r["id"]]].append(r)

        units = {}
        for root, members in by_root.items():
            kind, reason = _kind_for(root, members, marker_roots, threshold)
            sig = hashlib.blake2b(digest_size=16)
            for m in sorted(members, key=lambda m: m["path"]):
                sig.update(f"{m['path'][len(root) + 1:]}\0{m['size']}\n".encode())
            units[root] = (kind, reason, len(members), sum(m["size"] for m in members), sig.hexdigest())

        # 4. persist (stable ids: upsert by root, drop vanished roots)
        db.execute("UPDATE files SET unit_id=NULL")
        old = {r["root"]: r["id"] for r in db.execute("SELECT id, root FROM units")}
        for root in set(old) - set(units):
            db.execute("DELETE FROM plan WHERE item_type='unit' AND item_id=? AND locked=0 AND status='pending'",
                       (old[root],))
            db.execute("DELETE FROM units WHERE id=?", (old[root],))
        for root, (kind, reason, n, size, sig) in units.items():
            db.execute("""INSERT INTO units(root,kind,reason,file_count,size,signature) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(root) DO UPDATE SET kind=excluded.kind, reason=excluded.reason,
                          file_count=excluded.file_count, size=excluded.size, signature=excluded.signature""",
                       (root, kind, reason, n, size, sig))
        ids = {r["root"]: r["id"] for r in db.execute("SELECT id, root FROM units")}
        db.executemany("UPDATE files SET unit_id=? WHERE id=?",
                       [(ids[root], fid) for fid, root in assign.items() if root])
        # standalone files no longer need unit plan rows, unit members no longer need file plan rows
        db.execute("""DELETE FROM plan WHERE item_type='file' AND locked=0 AND status='pending'
                      AND item_id IN (SELECT id FROM files WHERE unit_id IS NOT NULL)""")
        db.commit()

        standalone = sum(1 for v in assign.values() if not v)
        kinds = C.Counter(u[0] for u in units.values())
        run.notes = f"units={len(units)} standalone={standalone}"
        print(f"units: {len(units)} units holding {len(rows) - standalone} files, {standalone} standalone files")
        for kind, n in kinds.most_common():
            size = sum(u[3] for u in units.values() if u[0] == kind)
            print(f"  {kind:14} {n:5}  {human(size)}")


def _keep_outer(roots: dict, root: str, kind: str):
    """Record a marker root unless an ancestor is already a root; drop descendants of a new outer root."""
    for existing in list(roots):
        if root == existing or root.startswith(existing + "/"):
            return
        if existing.startswith(root + "/"):
            del roots[existing]
    roots[root] = kind


def _kind_for(root, members, marker_roots, threshold):
    if root in marker_roots:
        return marker_roots[root], "project marker"
    inner = sorted((r for r in marker_roots if r.startswith(root + "/")), key=len)
    if inner:
        return marker_roots[inner[0]], f"contains project {inner[0]}"
    fams = C.Counter(m["family"] for m in members if m["family"] not in ("none", "other"))
    if fams:
        fam, n = fams.most_common(1)[0]
        if n / len(members) >= threshold:
            return fam, f"{n}/{len(members)} files are {fam}"
    return "folder", "downloaded folder"


if __name__ == "__main__":
    main()
