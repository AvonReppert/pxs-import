# CLAUDE.md — pxs-import

Context for Claude sessions working on this repository. Written for future
agent sessions doing maintenance, optimization and refactoring work.

## What this package is

`pxs-import` reads the raw output of the UDKM group's plasma X-ray source
(U. Potsdam) — one folder per scan, a `.dat` table, a `parameters*.txt` and
a stack of detector TIFFs — and hands it to
[nx5d](https://gitlab.com/kmc3-xpp/nx5d) as an HDF5-like tree. It discovers
scan folders, classifies them, numbers them chronologically, carries the
acquisition parameters across as scan attributes, and migrates a proposal's
spice anchors onto the new numbering.

It does **not** do physics. Turning an nx5d repository into reciprocal-space
maps is [xraydc](https://gitlab.com/kmc3-xpp/xraydc)'s job; this package is
what gets the measurement into the repository in the first place.

This code was extracted from `xraydc`'s `src/xraydc/io/` subpackage in
October 2026. The design history that produced it is carried under
[docs/superpowers/](docs/superpowers/), with a note on each document saying
it was written under the old names.

License GPLv3+. Authors: Alexander von Reppert, Matthias Rössle,
Florin Boariu. Upstream: <https://gitlab.com/kmc3-xpp/pxs-import/>

## Relationship to nx5d — read this before refactoring

**There is no code dependency in either direction, and there must not be
one.** `pxs_import` never imports `nx5d`; `pxs_import.prepare` reaches nx5d
by shelling out to the `pm` console script (see `_resolve_pm`,
[prepare.py:177](src/pxs_import/prepare.py#L177)), so a current nx5d install
is a *runtime* prerequisite for `pxs-prepare` without being an import-time
one. It never imports `xraydc` either. `pyproject.toml` lists only `numpy`,
`tifffile` and `silx`.

The coupling is **one-directional and string-resolved**, in two places:

1. **The reader.** `pxs_import.reader.scans_from_pxs` is an nx5d *reader*
   for its `pm scan import` CLI, resolved by name:

   ```bash
   pm scan import <proposal> -R pxs_import.reader:scans_from_pxs …
   ```

   `prepare.py` builds that argument itself, in two places. Zero nx5d source
   changes are needed to add a reader this way.

2. **The recipes.** A proposal's spice `recipes` record maps scan type →
   recipe URL, and those URLs point at xraydc:

   ```python
   repo.<proposal>.seed('recipes',
                        sRSM='pymod:///xraydc.cooking#sRSM',
                        TRSM='pymod:///xraydc.cooking#TRSM')
   ```

   nx5d resolves them at *cook* time, not import time. That is why a missing
   `xraydc` surfaces as a cook failure rather than an import error, and why
   the end-to-end corpus test guards with `importorskip('xraydc')` as well as
   `importorskip('nx5d')`.

Neither contract is reachable by grep from inside this repository. When
changing `scans_from_pxs`'s signature or the scan-key dict it yields,
remember the real consumer is nx5d's `pm`, driven by a string.

### Names that keep `udkm`, deliberately

Three identifiers kept their old spelling through the rename. Each has a
reason, and each will look like an oversight to someone "finishing the
rename". Do not touch them.

| Name | Where | Why |
|---|---|---|
| `MANIFEST_NAME = '.udkm-import.json'` | [s3.py:208](src/pxs_import/s3.py#L208) | Live buckets already hold this file. Renaming it makes every imported proposal look un-imported and silently re-uploads it. Pinned by a test in `tests/s3_test.py`. |
| `BASE_ENV_VAR = 'UDKM_BASE'` | [sources.py:65](src/pxs_import/sources.py#L65) | Already set in users' shells. `BASE_ENV_VARS` reads `UDKM_BASE` then `PXS_BASE`, so the alias works and the old name wins when both are set. |
| `udkm_source` | the scan-key dict in `reader.py` | nx5d sees it; it names the `YYYYMMDD/HHMMSS` a scan came from. |

## Architecture

Seven modules, ~3000 LOC. `format`/`frames` know only about files;
`sources`/`spice`/`s3` are site and bookkeeping knowledge; `reader` is the
nx5d-facing entry point; `prepare` is the orchestrator and the CLI.

| Module | LOC | Owns |
|---|---|---|
| [format.py](src/pxs_import/format.py) | 656 | Proposal layout: discovers `YYYYMMDD/HHMMSS/` scan folders (optionally behind one `YYYY` level), filters them by date range *before* descending, classifies scan type by signature filename (`SIGNATURES`), numbers them `r0001…`. Also parses `.dat` tables, `parameters*.txt`, the cryostat log, and the numeric-value grammar. Four types: `sRSM`, `tRSS`, `xRSS`, `alignment`. |
| [sources.py](src/pxs_import/sources.py) | 302 | Site knowledge: the three `BASE_PRESETS`, the four `PROGRAM_DIRS`, and the three source routes (`--source` → the proposal folder → the base path). `discover_all` merges several sources into one chronological numbering and coalesces duplicates. |
| [frames.py](src/pxs_import/frames.py) | 164 | Lazy TIFF frame-stack loading (NaN-filled on missing frames). The `.dat` table is the sole authority on which frames belong to a scan — never a glob. |
| [reader.py](src/pxs_import/reader.py) | 546 | The nx5d reader entry point, `scans_from_pxs`, plus the mapping-table read/write helpers `prepare` uses. Owns the scan-attribute layout — see "Listing attributes" below. |
| [spice.py](src/pxs_import/spice.py) | 220 | Migrates a proposal's spice anchors from old scan keys to the new run-number scheme (`migrate_spice`); drops/fixes stale recipe mappings (`fix_recipes`). |
| [prepare.py](src/pxs_import/prepare.py) | 753 | Orchestrator and `pxs-prepare` CLI: ties source resolution, date-range discovery, the `pm scan import` subprocess call and spice migration into one command (`prepare_proposal`). Also owns the `--s3` per-scan import loop and the `--s3-login` mode. |
| [s3.py](src/pxs_import/s3.py) | 394 | Everything `--s3` needs to reach a bucket: the `~/.aws` profile writer, the import manifest, prefix/delete/upload helpers. Imports `s3fs` inside functions only — `tests/packaging_test.py` pins that `import pxs_import` leaves `s3fs`/`fsspec`/`boto3` out of `sys.modules`. |

`src/pxs_import/__init__.py` re-exports nothing: every consumer names a
module.

### Listing attributes — another string-resolved contract

`build_scan_h5like` ([reader.py:125](src/pxs_import/reader.py#L125)) writes
two *deliberately* different families of root attribute, and the split is not
cosmetic. nx5d's `_get_header_items` (`nx5d/repo/universal.py`) harvests
**only** `k-header:*` into the scan summary, stripping the prefix; `_list`
(`nx5d/repo/base.py`) then mashes every summary entry that is not a scan
dimension into the `others` column. So a `k-header:` prefix *is* the decision
to show a value in the listing:

| Attribute | Where it shows |
|---|---|
| `k-header:description` | the `description` column, alone |
| `k-header:integration` | the `others` column |
| `k-header:stage`, `k-header:unit` (**`alignment` scans only**) | the `others` column |
| `k-header:tsample` (**only where a cryostat log exists**) | the `others` column |
| every `parameters*.txt` key under its own bare name, plus `notes`, `source`, `date`, `time`, `datetime`, `photon_energy`, `truncated`/`rows`/`frames` | nowhere in the listing; read them off the scan |

`stage`/`unit` are the one case where a `parameters*.txt` key gets a second,
prefixed copy. `alignment` scans are told apart only by which motor was moved
(`Omega`, `X Linear`, `Horizontal`, …), which varies per scan and is the
reason a reader opens the listing at all; the unit rides along because the raw
`position` values are not comparable between a goniometer scan in degrees and
a Zaber scan in steps. The bare `Stage`/`Unit` attributes the generic sweep
writes are untouched.

`tsample` comes from a different file again: `scan data<sfx>.txt`, a running
cryostat log copied into each scan folder, one row longer each time, whose
**last** row is that scan's own and carries its own `Day`/`Time` to prove it.
Present in 6 of 130 surveyed folders, all 2015 — absence is the normal case
and is silent. The row's stamp is checked against the scan, and a log ending
elsewhere is refused with a warning rather than written. Its two values also
land as the index-aligned variables `measurement/tsample_readback` and
`measurement/tcryo_readback`; the first name is deliberate, being the second
entry in the search list `xraydc.cooking.TRSM` already walks, so grouping by
temperature needs no change in the analysis layer. Watch the separator:
the header is tab-separated, the data rows are **space**-separated.

Two things about the value itself. It is a *start-of-scan* reading — the row
carries the scan's own `HHMMSS`, and in the surveyed cooldown consecutive
rows lie ~31 min and ~3 K apart, so writing it across every frame says the
sample held still when it did not. And the parser degrades rather than
failing: an absent file, no data row, a short row and a non-numeric
temperature all yield `None` with a warning naming the file, because an
optional extra present in a handful of folders must never abort a proposal
import.

`notes` holds the raw `Comments` lines and is deliberately *not* a
`k-header:` item: `description` already shows them, prefixed with the scan
folder, and a `k-header:notes` would repeat the whole string in `others`. The
same reasoning keeps the `parameters*.txt` entries out of `k-header:` — 12 to
17 of them per scan would make `others` unreadable. Everything outside `k-*`
is **unprefixed, by request**: there is no `udkm-` attribute namespace any
more, and `RESERVED_ATTRS` ([reader.py:116](src/pxs_import/reader.py#L116))
names the nine the module owns.

That sweep is generic on purpose: the key set differs per scan type (`File
Name` vs `Filename`, `Calib` vs `Calibration`, `Number of Loops` vs `# of
Loops`), and a table like `COLUMN_VARIABLES` would silently drop anything the
acquisition software adds later. The two entries the importer interprets for
its own use keep derived attributes as well (`k-header:integration`,
`datetime`); the parameters key keeps its own name either way. `pytest -m
corpus` pins the real per-type vocabulary across all 71 parameters files.

Each key gets exactly **one** attribute: the value parsed where
`numeric_value` ([format.py:358](src/pxs_import/format.py#L358)) finds an
unambiguous number, the raw string where it does not. Four shapes are
recognised — a region as four ints, tab-separated numbers as a float list, a
bare integer as `int`, a decimal with an optional `°`/`s` suffix as `float` —
and nothing else, so `'Triangle'`, a path, a `Date Time` and a delay vector
like `'-10:0.5:40, 41:1:200'` all stay strings. Two traps are deliberately
encoded: a leading-zero integer gets no reading at all (it is an identifier,
not a count), and `IDENTIFIER_KEYS` additionally excludes `File
Name`/`Filename`, whose `HHMMSS` values look numeric but are stems —
`'085055'` would become `85055` and `'120703'` would slip past the
leading-zero guard entirely. Over the real corpus the split is total: no key
is numeric for only some of its scans, which a corpus test pins.

`description` is a special case worth knowing before "simplifying" it: nx5d
seeds `summary['description']` with its own `Bad header spec:` error and
*then* runs `summary.update(header)`, so `k-header:description` overwrites
that message. The importer accepts that trade (it sets the attribute
unconditionally); the symptom of a bad `k-primary:0` is therefore empty
`primary`/`pri. values`, not a visible error. Neither side of this is
reachable by grep from inside this repo — the only test that pins it is the
attribute assertion in [tests/build_test.py](tests/build_test.py).

## The test corpus

`tests/corpus/` is **committed real data**, about 40 MB over 10 500 files,
and `tests/corpus_test.py`'s 26 assertions are measured off it: parameter
vocabularies, frame-plan counts, date-range totals, the cryostat log's own
stamps, and one whole-proposal import through `pm`.

It is not the raw data. [tools/thin_corpus.py](tools/thin_corpus.py) made it
from 22 GB of real proposal folders and is the provenance record: its `RULES`
table says, per folder, which signature tables are carried **verbatim** and
whether a frame is a readable stub at the real detector's shape, a zero-byte
name, or absent. Every row is justified by a named test. Rules of engagement:

- **A corpus assertion that disagrees with the corpus means the corpus is
  wrong, not the test.** The numbers came from the real proposals; fix the
  rule table and regenerate.
- The frames carry no real pixels. Nothing in the corpus tests reads one —
  they check shapes, counts, dtypes and NaN masks. Do not add an assertion
  about intensity.
- The tables *are* real, including `20151226/000051`'s 1.4 MB and PtAu's
  25 MB, because row counts are what the importer is tested on.
- Regenerating needs the raw proposals, which live only on the author's
  machine (and are deleted since the extraction). The tool stays as the
  record of how the corpus was made.

## Conventions

- **Markers**: `corpus` (runs against `tests/corpus/`), `nx5d` (needs an
  installed nx5d), `slow` (whole-proposal imports through `pm`). `addopts`
  is `-ra -m 'not slow'`, so bare `pytest` runs 212 tests in ~20 s —
  including every cheap corpus test, because the data ships with the repo.
  `pytest -m slow` runs the one 50-second end-to-end import.
- **Tests**: pytest, 213 collected. `tests/fixtures.py` writes synthetic
  scan folders (real header shapes, Windows-1252, CRLF) into `tmp_path`;
  `tests/conftest.py` holds `FakeFS`, the dict-backed fsspec stand-in the
  `--s3` tests share, and puts the repository root on `sys.path` so
  `tools_test.py` can import `tools.thin_corpus`.
- **Docstrings**: Google-style (`Args:`/`Returns:`/`Raises:`), `'''` quotes,
  mixed Markdown/reST inline markup. They carry the *why*, often at length —
  that is deliberate, and the file-format quirks documented in them (the
  tab-vs-space cryostat row, the undeclared leading `Loop` column, the
  one-character gap between `scans` and `scan`) are not guesses.
- **Logging**: every module has a module-level `logger`; the importer warns
  and degrades rather than aborting a proposal over one odd file.
- **Line endings**: `.gitattributes` forces LF and marks `*.tif` binary.
  The corpus's real text is CRLF on disk and LF in git; everything that
  reads it opens in binary and strips the CR. `tests/tools_test.py` pins
  that round trip.
- **Versioning**: `setuptools_scm`, `write_to = src/pxs_import/_version.py`
  (gitignored). Release via the interactive `scripts/release.sh` (SemVer +
  clean-tree + tag guards).
- **CI** (`.gitlab-ci.yml`): `pip install -e ".[test]"` then
  `coverage run --source=pxs_import -m pytest`. Neither `nx5d` nor `xraydc`
  is on PyPI, so the end-to-end tests skip there — **a green pipeline is not
  proof that an import still works**; the local `pytest -m slow` run is.
  The job installs only the `test` extra, so anything the suite imports has
  to be reachable from it: `s3fs` is listed there as well as in `[s3]` for
  that reason.

## Working on this repo

```bash
pip install -e ".[test]"
pytest                      # 212 tests, ~20 s, corpus included
pytest -m slow              # the whole-proposal import through pm (~50 s)
mkdocs serve -f doc/mkdocs.yml
```

Run `pytest -m corpus` after any change to the attribute or summary
contract: those tests are the only ones that exercise real
`parameters*.txt` vocabularies and the real `pm` import.

The two spice tests read `spice.pre-migration/`, not `spice/` —
`pxs-prepare` migrates `spice/` in place and `_ensure_backup` never
overwrites the backup, so once a proposal has been imported the live folder
no longer has the date/time anchors those tests migrate from.

Docs are mkdocs + mkdocstrings (`doc/pages/`).
