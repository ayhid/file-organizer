"""Shared test helpers: an isolated workspace and an in-process runner for the step scripts.

Every test gets its own temp source, target, state, font folder and Trash, plus a local rules
file, so nothing on the real machine is ever read or written.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FO = REPO / "fo"
if str(FO) not in sys.path:
    sys.path.insert(0, str(FO))

EXAMPLES = int(os.environ.get("FO_EXAMPLES", "25"))  # FO_EXAMPLES=500 for a thorough run

OTF = b"OTTO"
TTF = b"\x00\x01\x00\x00"

_modules: dict[str, object] = {}


def load(step: str):
    """Import fo/<step>.py under a private name (fo/profile.py must not shadow the stdlib module)."""
    if step not in _modules:
        spec = importlib.util.spec_from_file_location(f"fo_{step}", FO / f"{step}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _modules[step] = mod
    return _modules[step]


def write(path: Path, data: bytes | str = b"x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_zip(path: Path, members: dict[str, bytes | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def font_bytes(header: bytes = OTF, body: str = "glyphs") -> bytes:
    return header + body.encode()


def snapshot(root: Path) -> dict[str, bytes]:
    """relative path -> content, for every file under root."""
    out = {}
    if not root.exists():
        return out
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class Workspace:
    """Temp dirs + local rules + in-process runner."""

    def __init__(self, local_rules: str = ""):
        self.tmp = Path(tempfile.mkdtemp(prefix="fo-ws-"))
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.state = self.tmp / "state" / "state.db"
        self.fonts = self.tmp / "Fonts"
        self.trash = self.tmp / "Trash"
        self.local = self.tmp / "rules.local.toml"
        self.src.mkdir()
        self.set_rules(local_rules)

    def set_rules(self, extra: str = ""):
        base = (f'[fonts]\ninstall_dir = "{self.fonts}"\ntrash_dir = "{self.trash}"\n'
                f'trash_method = "dir"\n')
        self.local.write_text(base + extra, encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, rel: str, data: bytes | str = b"x", mtime: float | None = None) -> Path:
        return write(self.src / rel, data, mtime)

    def argv(self, *args):
        # defaults first: argparse keeps the last value, so a test's explicit flags win
        return ["--source", str(self.src), "--target", str(self.dst), "--state", str(self.state),
                "--local-rules", str(self.local), *args]

    def run(self, step: str, *args) -> tuple[int, str]:
        """Run fo/<step>.py main() in-process. Returns (exit_code, stdout)."""
        mod = load(step)
        out = io.StringIO()
        old = sys.argv
        sys.argv = [f"{step}.py", *self.argv(*args)]
        code = 0
        try:
            with contextlib.redirect_stdout(out):
                mod.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if not isinstance(e.code, int) and e.code is not None:
                out.write(str(e.code))
        finally:
            sys.argv = old
            import common
            common.close_all()
        return code, out.getvalue()

    def ok(self, step: str, *args) -> str:
        code, out = self.run(step, *args)
        if code != 0:
            raise AssertionError(f"{step} exited {code}:\n{out}")
        return out

    def pipeline(self, upto: str = "rename"):
        order = ["inventory", "units", "hash", "dupes", "profile", "classify", "rename"]
        for s in order[: order.index(upto) + 1]:
            self.ok(s)

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.state)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *params):
        with contextlib.closing(self.db()) as con:
            return con.execute(sql, params).fetchall()

    def exec(self, sql: str, *params) -> None:
        with contextlib.closing(self.db()) as con, con:
            con.execute(sql, params)

    def plan(self, src: str):
        rows = self.q("SELECT * FROM plan WHERE src=?", src)
        return rows[0] if rows else None


class WorkspaceCase:
    """Mixin for unittest.TestCase: self.ws is a fresh Workspace per test."""

    local_rules = ""

    def setUp(self):
        super().setUp()
        self.ws = Workspace(self.local_rules)

    def tearDown(self):
        self.ws.cleanup()
        super().tearDown()
