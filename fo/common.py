"""Shared helpers: SQLite state, config loading, run tracking, naming and date utilities.

Standard library only. Python 3.10+ (TOML via tomllib on 3.11+, tomli on 3.10).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:
        _toml = None

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_DIR / "rules.toml"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    script     TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL DEFAULT 'running',   -- running | ok | partial | error
    args       TEXT,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS scan_errors (
    run_id INTEGER REFERENCES runs(id),
    path   TEXT,
    error  TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,      -- relative to source root, NFC
    name     TEXT NOT NULL,
    ext      TEXT NOT NULL,
    family   TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    unit_id  INTEGER REFERENCES units(id) ON DELETE SET NULL,
    seen_run INTEGER,
    missing  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_files_size ON files(size);
CREATE INDEX IF NOT EXISTS ix_files_unit ON files(unit_id);

CREATE TABLE IF NOT EXISTS units (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL UNIQUE,    -- relative dir path
    kind       TEXT NOT NULL,           -- git | node | xcode | app | font | embroidery | model3d | folder ...
    reason     TEXT,
    file_count INTEGER NOT NULL DEFAULT 0,
    size       INTEGER NOT NULL DEFAULT 0,
    signature  TEXT                     -- hash of sorted (relpath, size), for duplicate units
);

CREATE TABLE IF NOT EXISTS hashes (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    algo    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL NOT NULL,
    partial TEXT,
    full    TEXT
);

CREATE TABLE IF NOT EXISTS dup_groups (
    id      INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,              -- file | unit | zip
    key     TEXT NOT NULL,              -- hash, signature or zip path
    size    INTEGER NOT NULL,
    members INTEGER NOT NULL,
    wasted  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_members (
    group_id  INTEGER NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,            -- file | unit
    item_id   INTEGER NOT NULL,
    role      TEXT NOT NULL,            -- keep | remove
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS ix_dup_item ON dup_members(item_type, item_id);

CREATE TABLE IF NOT EXISTS doc_dates (
    file_id   INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    date      TEXT,                     -- ISO date or NULL
    precision TEXT,                     -- day | month | year
    source    TEXT                      -- pdf-text | pdf-meta | none
);

CREATE TABLE IF NOT EXISTS plan (
    id           INTEGER PRIMARY KEY,
    item_type    TEXT NOT NULL,         -- file | unit
    item_id      INTEGER NOT NULL,
    src          TEXT NOT NULL,         -- relative to source root
    family       TEXT,
    rule         TEXT,
    action       TEXT NOT NULL,         -- move | delete | skip
    target_dir   TEXT,                  -- relative to target root
    new_name     TEXT,
    dated        INTEGER NOT NULL DEFAULT 0,
    precision    TEXT,
    date_found   TEXT,
    date_source  TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    locked       INTEGER NOT NULL DEFAULT 0,  -- 1 = edited by hand, never overwritten by classify/rename
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error | undone
    updated_at   TEXT,
    UNIQUE(item_type, item_id)
);

CREATE TABLE IF NOT EXISTS ops (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER NOT NULL REFERENCES runs(id),
    plan_id INTEGER REFERENCES plan(id),
    op      TEXT NOT NULL,              -- move
    src     TEXT NOT NULL,              -- absolute
    dst     TEXT NOT NULL,              -- absolute
    at      TEXT NOT NULL,
    status  TEXT NOT NULL,              -- pending | done | copied | error | undone
    error   TEXT
);
"""


# --------------------------------------------------------------------------- config / db

def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


DEFAULT_LOCAL = "~/.file-organizer/rules.local.toml"


def _read_toml(path: Path) -> dict:
    if _toml is None:
        sys.exit("Python 3.10 needs the 'tomli' package to read TOML: pip install tomli")
    with open(path, "rb") as fh:
        return _toml.load(fh)


def merge_config(base: dict, local: dict) -> dict:
    """Deep merge: tables merge recursively, arrays are concatenated with LOCAL ITEMS FIRST
    (so personal classify rules win over generic ones), scalars are overridden."""
    out = dict(base)
    for k, v in local.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_config(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(v) + list(out[k])
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None, local: str | os.PathLike | None = None) -> dict:
    """Generic rules (in the repo) + personal overrides (outside the repo, never committed)."""
    cfg = _read_toml(Path(path) if path else DEFAULT_CONFIG)
    local_path = expand(local or os.environ.get("FO_LOCAL_RULES")
                        or cfg.get("paths", {}).get("local_rules", DEFAULT_LOCAL))
    if local_path.exists():
        cfg = merge_config(cfg, _read_toml(local_path))
        cfg["_local_rules"] = str(local_path)
    return cfg


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


def connect(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state_path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def meta_get(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


class Context:
    """Parsed args + config + db + resolved paths, shared by every script."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = load_config(args.config, args.local_rules)
        paths = self.cfg.get("paths", {})
        self.state = expand(args.state or paths.get("state", "~/.file-organizer/state.db"))
        self.db = connect(self.state)
        src = args.source or meta_get(self.db, "source") or paths.get("source")
        tgt = args.target or paths.get("target")
        self.source = expand(src) if src else None
        self.target = expand(tgt) if tgt else None
        # reports live next to the state file unless configured (and --state always wins)
        self.reports = (expand(paths["reports"]) if paths.get("reports") and not args.state
                        else self.state.parent / "reports")
        self.families = build_family_map(self.cfg)


def setup(description: str, add_args=None) -> Context:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--config", help="rules file (default: rules.toml in the repo)")
    ap.add_argument("--local-rules", help=f"personal overrides, kept outside the repo (default: {DEFAULT_LOCAL})")
    ap.add_argument("--state", help="SQLite state file (default from rules.toml [paths].state)")
    ap.add_argument("--source", help="folder to analyse (default from rules.toml or last inventory)")
    ap.add_argument("--target", help="organised tree root (default from rules.toml)")
    if add_args:
        add_args(ap)
    return Context(ap.parse_args())


class Run:
    """Context manager recording a row in `runs`."""

    def __init__(self, ctx: Context, script: str):
        self.ctx, self.script = ctx, script
        self.status, self.notes = "ok", None

    def __enter__(self):
        args = {k: v for k, v in vars(self.ctx.args).items()}
        cur = self.ctx.db.execute("INSERT INTO runs(script, started_at, args) VALUES(?,?,?)",
                                  (self.script, now_iso(), json.dumps(args, default=str)))
        self.id = cur.lastrowid
        self.ctx.db.commit()
        return self

    def __exit__(self, exc_type, exc, tb):
        status = "error" if exc_type and exc_type is not SystemExit else self.status
        notes = self.notes if not exc else f"{exc_type.__name__}: {exc}"
        self.ctx.db.execute("UPDATE runs SET ended_at=?, status=?, notes=? WHERE id=?",
                            (now_iso(), status, notes, self.id))
        self.ctx.db.commit()
        return False


class Budget:
    """Wall-clock budget so long steps can stop cleanly and resume later (0 = unlimited)."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds if seconds and seconds > 0 else None

    def exceeded(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline


# --------------------------------------------------------------------------- files / families

MULTI_EXT = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def ext_of(name: str) -> str:
    low = name.lower()
    for m in MULTI_EXT:
        if low.endswith(m):
            return m
    e = os.path.splitext(low)[1]
    return e if e and len(e) <= 12 and " " not in e else ""


def stem_of(name: str) -> str:
    e = ext_of(name)
    return name[: len(name) - len(e)] if e else name


def build_family_map(cfg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for fam, exts in cfg.get("families", {}).items():
        for e in exts:
            e = e.lower()
            out[e if e.startswith(".") else "." + e] = fam
    return out


def family_of(ext: str, families: dict[str, str]) -> str:
    return families.get(ext, "other" if ext else "none")


def glob_any(relpath: str, patterns) -> bool:
    low = relpath.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in patterns or [])


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# --------------------------------------------------------------------------- naming

COPY_SUFFIX_RE = re.compile(r"(\s*\(\d+\)|\s+\d{1,2}|[\s_-]+copy(\s*\d+)?|[\s_-]+copie(\s*\d+)?)$", re.I)


def strip_copy_suffix(stem: str) -> str:
    prev = None
    while prev != stem:
        prev, stem = stem, COPY_SUFFIX_RE.sub("", stem).strip()
    return stem or prev


def slugify(text: str) -> str:
    """lowercase-slug: accents removed from Latin letters, other scripts kept, separators -> '-'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ").replace("@", " at ").lower()
    out, dash = [], False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            dash = False
        elif not dash:
            out.append("-")
            dash = True
    return "".join(out).strip("-")


def is_copy_name(name: str) -> bool:
    stem = stem_of(name)
    return strip_copy_suffix(stem) != stem


# --------------------------------------------------------------------------- dates

MONTHS = {
    "jan": 1, "janv": 1, "janvier": 1, "january": 1,
    "feb": 2, "fev": 2, "fevr": 2, "fevrier": 2, "february": 2,
    "mar": 3, "mars": 3, "march": 3,
    "apr": 4, "avr": 4, "avril": 4, "april": 4,
    "may": 5, "mai": 5,
    "jun": 6, "juin": 6, "june": 6,
    "jul": 7, "juil": 7, "juillet": 7, "july": 7,
    "aug": 8, "aou": 8, "aout": 8, "august": 8,
    "sep": 9, "sept": 9, "septembre": 9, "september": 9,
    "oct": 10, "octobre": 10, "october": 10,
    "nov": 11, "novembre": 11, "november": 11,
    "dec": 12, "decembre": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
Y = r"(?P<y>(?:19|20)\d{2})"

DATE_PATTERNS = [
    # 2025-04-03, 2025_04_03, 2025.04.03, 20250403
    ("day", re.compile(rf"(?<!\d){Y}(?P<sep>[-_. ]?)(?P<m>0[1-9]|1[0-2])(?P=sep)(?P<d>0[1-9]|[12]\d|3[01])(?!\d)")),
    # 03/04/2025, 03-04-2025, 3.4.2025 (day first, French order)
    ("day", re.compile(rf"(?<!\d)(?P<d>0?[1-9]|[12]\d|3[01])(?P<sep>[-/._])(?P<m>0?[1-9]|1[0-2])(?P=sep){Y}(?!\d)")),
    # 11 mars 2026 / 11 March 2026
    ("day", re.compile(rf"(?<!\d)(?P<d>[0-3]?\d)(?:er)?\s+(?P<mn>{_MONTH_ALT})\.?\s+{Y}(?!\d)", re.I)),
    # Mar 11, 2026
    ("day", re.compile(rf"\b(?P<mn>{_MONTH_ALT})\.?\s+(?P<d>[0-3]?\d),?\s+{Y}(?!\d)", re.I)),
    # 2026-02, 2026_02
    ("month", re.compile(rf"(?<!\d){Y}[-_](?P<m>0[1-9]|1[0-2])(?![\d])")),
    # mars 2026 / March 2026
    ("month", re.compile(rf"\b(?P<mn>{_MONTH_ALT})\.?\s+{Y}(?!\d)", re.I)),
    # 2026 alone (only used when a rule asks for year precision)
    ("year", re.compile(rf"(?<![\d])(?P<y>20[0-3]\d)(?![\d])")),
]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def find_date(text: str, allow_year: bool = False, min_year: int = 1995, max_year: int | None = None):
    """Return (iso_date, precision, (start, end)) for the first plausible date in text, else None.

    Patterns are tried by precision (day first) so '2025-04-03' wins over '2025-04'.
    """
    max_year = max_year or dt.date.today().year + 1
    folded = _fold(text)
    for precision, rx in DATE_PATTERNS:
        if precision == "year" and not allow_year:
            continue
        for m in rx.finditer(folded):
            g = m.groupdict()
            y = int(g["y"])
            if not (min_year <= y <= max_year):
                continue
            if g.get("mn"):
                mon = MONTHS.get(g["mn"].lower().rstrip("."))
                if not mon:
                    continue
            else:
                mon = int(g["m"]) if g.get("m") else 1
            day = int(g["d"]) if g.get("d") else 1
            try:
                d = dt.date(y, mon, day)
            except ValueError:
                continue
            if precision == "day":
                return d.isoformat(), "day", m.span()
            if precision == "month":
                return d.isoformat()[:7], "month", m.span()
            return str(y), "year", m.span()
    return None


def format_date(iso: str, found_precision: str, wanted: str) -> str:
    """Render a found date at the precision a rule asks for, never more precise than what was found."""
    order = {"year": 0, "month": 1, "day": 2}
    p = min(order.get(found_precision, 0), order.get(wanted, 2), key=lambda x: x)
    return iso[:4] if p == 0 else iso[:7] if p == 1 else iso[:10]


def move_path(src: str, dst: str) -> tuple[str, str | None]:
    """Move a file or a folder without ever overwriting.

    Returns ("done", None), or ("copied", reason) when the copy succeeded but the source
    could not be removed (another volume where deleting is not allowed). Raises on failure.
    """
    import errno
    import shutil

    if os.path.lexists(dst):
        raise FileExistsError(f"target exists: {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
        return "done", None
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    # different volume: copy, then remove the source
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.rmtree(src)
        else:
            os.remove(src)
    except OSError as e:
        return "copied", f"copied but source not removed: {e}"
    return "done", None


def remove_span(text: str, span) -> str:
    a, b = span
    return (text[:a] + " " + text[b:]).strip()
