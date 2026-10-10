# file-organizer

Small, deterministic Python scripts to profile a messy folder (typically `~/Downloads`), find
duplicates, and move everything into a clean tree with consistent names. Nothing moves until
you have reviewed the plan, and every move can be undone.

Standard library only (Python 3.11+, or 3.10 with `pip install tomli`). Optional: `pdftotext` /
`pdfinfo` (poppler) to read dates inside PDFs.

## Pipeline

All state lives in one SQLite file (default `~/.file-organizer/state.db`). Each step reads it,
adds to it, and can be re-run: unchanged files are never re-hashed, hand edits are never lost.

| # | Script | Does |
|---|---|---|
| 1 | `fo/inventory.py` | Lists every file (path, size, mtime, extension, family) |
| 2 | `fo/units.py` | Detects **units**: folders handled as a whole (code projects, `.app` bundles, font families, embroidery sets, 3D kits, downloaded folders) |
| 3 | `fo/hash.py` | Hashes only what can be a duplicate: same size, then first + last 64 KB, then full file (BLAKE2b). `--budget N` stops after N s, rerun to resume |
| 4 | `fo/dupes.py` | Duplicate files, duplicate units, zips already extracted. Picks the copy to keep |
| 5 | `fo/profile.py` | Report: families, extensions, folders, units, duplicate ratio (`reports/profile.md`) |
| 6 | `fo/classify.py` | Decides the target folder of every standalone file and unit from `rules.toml` |
| 7 | `fo/rename.py` | Computes new names |
| 8 | `fo/review.py` | `summary`, `export` the plan to CSV, `import` your edits |
| 9 | `fo/apply.py` | Dry run by default; `--apply` executes (move, delete, install fonts) and logs every operation |
| 10 | `fo/undo.py` | `--last --yes` or `--run N --yes` reverses everything (moves back, restores from Trash, uninstalls) |

```bash
./run.sh                                   # steps 1 to 7 + summary, moves nothing
python3 fo/review.py export --only review  # edit ~/.file-organizer/reports/plan.csv
python3 fo/review.py import
python3 fo/apply.py                        # dry run
python3 fo/apply.py --apply
python3 fo/undo.py --last --yes            # if needed
```

## Rules

**Units.** A file belongs to the outermost of: a project root (marker file like `package.json`,
or a `.git` / `*.xcodeproj` folder), a bundle (`*.app`...), or its first ancestor folder that is not
a *collection*. Collections are container folders (`Documents`, `Images`...) whose sub-folders
become units and whose loose files stay standalone. Files inside a unit are never renamed,
deduplicated or split: the unit moves as one block.

**Names.** `lowercase-slug.ext`. When the rule says the date matters (invoices, statements,
attestations, photos...), the date goes before the extension: `facture-acme_2025-03.pdf`.
The date comes from the name, else the PDF text, else the PDF metadata, else the row is flagged
for review. Accents are removed from Latin letters, other scripts are kept.

**Duplicates.** Keeper = best-classified path: not in `[dupes].avoid`, earliest `[dupes].prefer`
match, inside a folder rather than loose, no copy suffix (` (1)`, ` 2`, `copy`), oldest, shortest
path. Redundant copies go to `<target>/_to_delete/`; you empty it yourself.

**Fonts.** Rules with `action = "install"` (on by default for font families and loose fonts)
copy every `.ttf .otf .ttc .otc .dfont` into `[fonts].install_dir` (`~/Library/Fonts` on macOS,
active right away), check each copy (identical bytes + valid font header), and only when all of
them succeeded move the original file or folder to the Trash. A font already installed with the
same content counts as installed; a different font with the same file name blocks the item and
nothing is trashed. Web fonts (`.woff`, `.woff2`, `.eot`) can't be installed and are moved to
`design/fonts/_loose`. `undo.py` takes the item out of the Trash and uninstalls only the fonts
that run installed.

**Review.** Rows are flagged when the name says nothing (uuid, hex, `IMG_1234`), a dated rule found
no date, no rule matched, or the rule asks for it (credentials, mixed folders). Flagged rows are
skipped by `apply.py` unless `--include-review`.

## Personal rules stay out of this repo

`rules.toml` is generic. Put client names, people, schools and personal folder names in
`~/.file-organizer/rules.local.toml` (see `rules.local.example.toml`). It is merged on top: arrays
are prepended, so personal classify rules win. The state DB and reports also live outside the repo.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
