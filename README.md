# file-organizer

Small, deterministic Python scripts to profile a messy folder (typically `~/Downloads`), find
duplicates, and move everything into a clean tree with consistent names. Nothing moves until
you have reviewed the plan, and every move can be undone.

Standard library only (Python 3.11+, or 3.9 / 3.10 with `pip install tomli`). Optional: `pdftotext` /
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
| 8b | `fo/report.py` | Human friendly HTML report (`reports/report.html`, one offline page): KPIs, filters, search, and every row opens its details on click (full paths, rule, reason, dates, unit content, duplicate group) |
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

## Routine (periodic run)

`routine.sh` runs the whole analysis and refreshes the plan. It never moves, deletes or installs
anything. State, personal rules, reports and logs live in `FO_HOME` (default
`~/Downloads/.file-organizer`, which the scan ignores), so every run picks up where the last one left off.

```bash
FO_SOURCE=~/Downloads MAX_SECONDS=170 ./routine.sh   # exit 3 + "ROUTINE: RESUME" = call again
```

Outputs: `reports/report.html` (open it in a browser: the readable view of the plan, also kept as
`report-YYYY-MM-DD.html`), `reports/review.csv` (rows that need a decision), `reports/plan.csv`,
`reports/profile.md`, `reports/dupes.csv`. You then review and run `fo/apply.py --apply` yourself.

The report shows `reports/notes.md` at the top when it exists. `FO_PATH_ALIAS="FROM=TO ..."` rewrites
path prefixes in the report (when the routine runs through a mount) and `FO_REPO_DIR` sets the repo
path shown in its commands. "New" and "decided" compare with the previous report; re-running the
routine the same day keeps the same baseline.

**Fonts** need macOS itself. `macos/install-fonts-job.sh` installs a launchd agent (weekly, Sunday
22:00 by default) that runs `apply.py --only install --apply` against the same state:

```bash
bash macos/install-fonts-job.sh --run-now   # install + first run (accept the macOS prompts)
bash macos/install-fonts-job.sh --uninstall
```

## Personal rules stay out of this repo

`rules.toml` is generic. Put client names, people, schools and personal folder names in
`~/.file-organizer/rules.local.toml` (see `rules.local.example.toml`). It is merged on top: arrays
are prepended, so personal classify rules win. The state DB and reports also live outside the repo.

## Tests

Every step and helper has unit tests, and the logic that must never fail has property tests
([Hypothesis](https://hypothesis.readthedocs.io)) that try hundreds of random inputs:

| Area | Properties checked |
|---|---|
| names | slug is idempotent, only `[alnum-]`, lowercase, no accents; copy-suffix removal is idempotent and never empties a name; `stem + ext == name` |
| dates | any date written in 12 formats (and French month names) inside random text is found exactly; never raises; never invents a date out of range; never formats more precisely than found |
| config | local overrides: tables merge, arrays prepend, scalars win, inputs never mutated |
| units | roots never nest; every file sits under its unit root; standalone only under collections |
| hashing | full hash equal ⇔ content equal; a change in the middle of a big file is caught by the full hash |
| duplicates | groups equal the ground truth by content; exactly one keeper per group; keeper order is total and deterministic |
| plan | every standalone file and unit gets exactly one row; deletes are exactly the redundant copies; targets never escape the target root; re-running the pipeline gives the same plan |
| apply / undo | on random trees: no file lost, nothing pre-existing overwritten, installed fonts identical to trashed originals, and undo restores source, target, font folder and Trash byte for byte |

Everything runs in temp folders: your real files, fonts and Trash are never touched.

```bash
pip install -r requirements-dev.txt
cd tests
python3 -m unittest discover -s . -v                          # ~20 s
FO_EXAMPLES=500 python3 -m unittest discover -s .             # thorough property run
coverage run --source=../fo -m unittest discover -s . && coverage report -m
```

CI (`.github/workflows/tests.yml`) runs the suite on Linux and macOS, Python 3.10 and 3.13.
