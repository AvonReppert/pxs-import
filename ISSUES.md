# Known defects and deferred findings

Carried over from the reviews that ran while this code lived in `xraydc`
(as `src/xraydc/io/`), plus what the extraction review added. None is
speculative — each was found by reading the code or by a reviewer, and none
is currently covered by a test.

Names below are the ones this package uses, not the ones the reviews used.

## Correctness

- **`reader.scans_from_pxs`'s `only` docstring is wrong under `--s3`.** It
  claims `iscan` counts the full mapping; on the per-scan S3 path the
  mapping passed in is one entry, so `iscan` is always 1.
- **`is_dir()`/`is_file()` probes swallow `OSError`.** A network share that
  drops mid-walk turns real scan folders into "unclassifiable scan folder"
  warnings instead of an error, so an import silently comes out short. The
  one place that does name the failing directory is `format._listdir`.
- **The `--s3` skip check compares `source` but not `root`.** Two scans with
  the same `YYYYMMDD/HHMMSS` from different acquisition folders are only
  told apart by `root`, which the manifest records but the skip test
  ignores.
- **`format.discover_scans` stamps `root` unresolved.** The value is the
  path as the caller passed it, so two spellings of one directory compare
  unequal.
- **The same date under two year folders in one source** yields two
  `ScanFolder`s with one identity. Only `sources.discover_all` collapses
  duplicates; a direct `discover_scans` call does not.
- **A source path containing `=` breaks nx5d's kwarg parser** (`split('=')`
  with no maxsplit, on nx5d's side). Not fixable here; noted in the
  docstring.

## Interface

- **`prepare.prepare_proposal` raises `SystemExit`** for the route-3
  refusal. That is a CLI decision sitting in the function a GUI would call,
  and it should become an exception the caller can catch. Clean this up when
  the measurement-PC GUI arrives.
- **`prepare.format_mapping_table`'s printed form has no `root` column**,
  although the CSV has one — so the table a user actually reads cannot tell
  two same-named scans apart.

## Tests and tooling

- **`tools.thin_corpus.FolderRule.carries_dat` does not validate `dat`** the
  way `flavour` validates `frames`. `dat='All'` or `dat='verbatim'` silently
  means "carry no table verbatim". Loud for folders whose tables a test
  parses, silent for the rest. Validate in `__post_init__`.
- **`tools.thin_corpus.thin` never deletes.** A rule narrowed from
  `READABLE` to `ABSENT` and regenerated in place leaves the old frames
  behind, and `Path.touch()` on an existing readable stub does not truncate
  it, so `READABLE → PLACEHOLDER` is a no-op. The hazard is documented in
  two places but nothing enforces it; a `--clean` flag, or a refusal to
  write into a non-empty `--out`, would.
- **`corpus_test.py`'s three `skipif` guards are vestigial.** They check one
  directory's existence, which made sense when the corpus was gitignored.
  Now the data ships with the repo, so a partial checkout or a botched
  regeneration that loses `tests/corpus/20151226/` makes those tests *skip*
  and leaves CI green. They were deliberately left alone during the
  extraction, because the gate for that work was "every corpus assertion
  unchanged"; turning them into failures is a clean follow-up.
- **The cryostat silence test pins by keyword** rather than by the absence
  of log records.
- **`tests/fixtures.py` writes a trailing tab** on the cryostat log's last
  row, where the real files have none.

## Documentation

- **`reader.py`'s module docstring still advertises a `udkm-*` attribute
  namespace** alongside `k-*`. There is no such namespace — everything
  outside `k-*` is unprefixed. The line is rendered into the API reference.
- **"Present in 6 of 130 surveyed folders"** (the cryostat log, in
  `CLAUDE.md` and in `format.parse_scan_temperatures`) is a stale
  denominator; the survey has grown to roughly 140 unique scans.
- **`tests/conftest.py`'s docstring describes xraydc's layout** — an `io/`
  directory and `qmapper`/`rsm` tests, neither of which is in this package.
  It came over verbatim with the move.
- **`CLAUDE.md`'s `BASE_ENV_VAR` row links to `sources.py:65`**, which is
  `BASE_ENV_VARS`; `BASE_ENV_VAR` itself is at `:69`. The same section says
  "26 assertions" where it means 26 tests.
