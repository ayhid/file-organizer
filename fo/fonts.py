"""Font install + trash helpers used by apply.py and undo.py.

Install = copy into the user font folder (macOS: ~/Library/Fonts, activated automatically).
Success  = the installed copy exists, is byte-identical to the source and looks like a font.
Only after every font of an item is installed is the source moved to the Trash.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys

from common import expand, move_path

# first 4 bytes of installable font formats
MAGIC = {
    b"\x00\x01\x00\x00": "truetype",
    b"true": "truetype (mac)",
    b"OTTO": "opentype (cff)",
    b"ttcf": "collection",
    b"typ1": "postscript",
}


class FontError(Exception):
    pass


def settings(cfg: dict) -> dict:
    f = cfg.get("fonts", {})
    exts = f.get("installable", [".ttf", ".otf", ".ttc", ".otc", ".dfont"])
    return {
        "install_dir": expand(f.get("install_dir", "~/Library/Fonts")),
        "trash_dir": expand(f.get("trash_dir", "~/.Trash")),
        "trash_method": f.get("trash_method", "auto"),  # auto | dir | finder
        "installable": {e.lower() if e.startswith(".") else "." + e.lower() for e in exts},
    }


def digest(path: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def looks_like_font(path: str) -> bool:
    if path.lower().endswith(".dfont"):  # resource-fork suitcase: no simple magic, trust the extension
        return os.path.getsize(path) > 0
    with open(path, "rb") as fh:
        return fh.read(4) in MAGIC


def fonts_in(path: str, installable: set) -> list[str]:
    """The installable font files of a file or a folder (recursive, hidden files and __MACOSX ignored)."""
    if os.path.isfile(path):
        return [path] if os.path.splitext(path)[1].lower() in installable else []
    out = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
        for fn in filenames:
            if not fn.startswith(".") and os.path.splitext(fn)[1].lower() in installable:
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def install(font: str, install_dir) -> tuple[str, bool]:
    """Copy one font into install_dir. Returns (installed_path, already_there).
    Never overwrites a different font with the same file name."""
    if not looks_like_font(font):
        raise FontError(f"not a valid font file: {font}")
    os.makedirs(install_dir, exist_ok=True)
    dst = os.path.join(str(install_dir), os.path.basename(font))
    src_hash = digest(font)
    if os.path.exists(dst):
        if digest(dst) == src_hash:
            return dst, True
        raise FontError(f"a different font is already installed as {dst}")
    tmp = dst + ".fo-tmp"
    shutil.copy2(font, tmp)
    os.replace(tmp, dst)
    if digest(dst) != src_hash:
        os.remove(dst)
        raise FontError(f"install verification failed for {dst}")
    return dst, False


def uninstall(installed: str, expected_hash: str | None = None) -> None:
    if os.path.exists(installed) and (expected_hash is None or digest(installed) == expected_hash):
        os.remove(installed)


def trash(path: str, s: dict) -> str:
    """Move to the Trash. Returns where it went ("finder:" prefix = Finder trash, restore with Put Back)."""
    method = s["trash_method"]
    if method in ("auto", "dir"):
        name = os.path.basename(path.rstrip("/"))
        dst = os.path.join(str(s["trash_dir"]), name)
        if os.path.lexists(dst):
            stem, ext = os.path.splitext(name)
            if os.path.isdir(path):
                stem, ext = name, ""
            dst = os.path.join(str(s["trash_dir"]), f"{stem} {dt.datetime.now():%H.%M.%S}{ext}")
        try:
            os.makedirs(str(s["trash_dir"]), exist_ok=True)
            status, err = move_path(path, dst)
            if status != "done":
                raise OSError(err)
            return dst
        except OSError:
            if method == "dir" or sys.platform != "darwin":
                raise
    # macOS Finder (works without Full Disk Access, supports Put Back)
    script = f'tell application "Finder" to delete (POSIX file "{path}" as alias)'
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise OSError(f"Finder could not trash {path}: {p.stderr.strip()}")
    return "finder:" + path
