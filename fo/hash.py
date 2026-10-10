#!/usr/bin/env python3
"""Step 3. Hash what is needed to find exact duplicates, cheapest first, resumable.

  A. standalone files sharing a size -> partial hash (first + last chunk)
  B. partial collisions               -> full hash
  C. units sharing a signature        -> full hash of their files (to confirm duplicate units)

A hash is reused while the file's size and mtime are unchanged.
Exit code 3 means the time budget ran out: run again to continue.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Budget, Run, human, setup  # noqa: E402

INCOMPLETE = 3


def hasher(algo: str):
    if algo == "blake2b":
        return hashlib.blake2b(digest_size=16)
    return hashlib.new(algo)


def hash_file(path: str, algo: str, chunk: int, partial: bool) -> str:
    h = hasher(algo)
    with open(path, "rb") as fh:
        if partial:
            h.update(fh.read(chunk))
            size = fh.seek(0, 2)
            if size > 2 * chunk:
                fh.seek(-chunk, 2)
                h.update(fh.read(chunk))
            elif size > chunk:
                fh.seek(chunk)
                h.update(fh.read())
        else:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def main():
    ctx = setup(__doc__, lambda ap: ap.add_argument(
        "--budget", type=float, default=0, help="stop after N seconds (0 = no limit); rerun to resume"))
    db, hcfg = ctx.db, ctx.cfg.get("hash", {})
    algo, chunk = hcfg.get("algo", "blake2b"), int(hcfg.get("chunk", 65536))
    budget = Budget(ctx.args.budget)
    root = str(ctx.source)

    with Run(ctx, "hash") as run:
        # forget stale hashes (file changed or algorithm changed)
        db.execute("""DELETE FROM hashes WHERE file_id IN (
                        SELECT h.file_id FROM hashes h JOIN files f ON f.id=h.file_id
                        WHERE f.size!=h.size OR f.mtime!=h.mtime OR h.algo!=?)""", (algo,))
        db.commit()
        done = {"partial": 0, "full": 0, "bytes": 0, "errors": 0}

        def store(fid, size, mtime, col, value):
            db.execute(f"""INSERT INTO hashes(file_id,algo,size,mtime,{col}) VALUES(?,?,?,?,?)
                           ON CONFLICT(file_id) DO UPDATE SET {col}=excluded.{col}""",
                       (fid, algo, size, mtime, value))

        def run_phase(rows, col, partial):
            for i, r in enumerate(rows):
                if budget.exceeded():
                    db.commit()
                    return False
                try:
                    value = hash_file(os.path.join(root, r["path"]), algo, chunk, partial)
                    done["bytes"] += min(r["size"], 2 * chunk) if partial else r["size"]
                except OSError:
                    value = "ERR"
                    done["errors"] += 1
                store(r["id"], r["size"], r["mtime"], col, value)
                if partial and r["size"] <= 2 * chunk:  # whole file already read
                    store(r["id"], r["size"], r["mtime"], "full", value)
                done["partial" if partial else "full"] += 1
                if i % 200 == 0:
                    db.commit()
            db.commit()
            return True

        standalone = "f.missing=0 AND f.unit_id IS NULL AND f.size>0"
        phase_a = db.execute(f"""
            SELECT f.id, f.path, f.size, f.mtime FROM files f LEFT JOIN hashes h ON h.file_id=f.id
            WHERE {standalone} AND h.partial IS NULL
              AND f.size IN (SELECT size FROM files f WHERE {standalone} GROUP BY size HAVING COUNT(*)>1)
            ORDER BY f.size""").fetchall()
        complete = run_phase(phase_a, "partial", True)

        if complete:
            phase_b = db.execute(f"""
                SELECT f.id, f.path, f.size, f.mtime FROM files f JOIN hashes h ON h.file_id=f.id
                WHERE {standalone} AND h.full IS NULL AND h.partial!='ERR'
                  AND (f.size, h.partial) IN (
                      SELECT f.size, h.partial FROM files f JOIN hashes h ON h.file_id=f.id
                      WHERE {standalone} GROUP BY f.size, h.partial HAVING COUNT(*)>1)
                ORDER BY f.size""").fetchall()
            complete = run_phase(phase_b, "full", False)

        if complete:
            phase_c = db.execute("""
                SELECT f.id, f.path, f.size, f.mtime FROM files f
                JOIN units u ON u.id=f.unit_id LEFT JOIN hashes h ON h.file_id=f.id
                WHERE f.missing=0 AND h.full IS NULL
                  AND u.signature IN (SELECT signature FROM units GROUP BY signature HAVING COUNT(*)>1)
                ORDER BY f.size""").fetchall()
            complete = run_phase(phase_c, "full", False)

        run.status = "ok" if complete else "partial"
        run.notes = f"partial={done['partial']} full={done['full']} read={done['bytes']} errors={done['errors']}"
        print(f"hash: {done['partial']} partial, {done['full']} full, {human(done['bytes'])} read, "
              f"{done['errors']} unreadable" + ("" if complete else " | budget reached, run again to resume"))
    sys.exit(0 if complete else INCOMPLETE)


if __name__ == "__main__":
    main()
