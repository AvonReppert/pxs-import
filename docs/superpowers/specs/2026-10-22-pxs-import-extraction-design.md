# Extracting the PXS importer into a standalone package (`pxs-import`)

> **Historical note.** This is the design and plan of the extraction that
> created this package, written in the `xraydc` repository before the move.
> It is carried verbatim: it names the old module paths (`src/xraydc/io/`,
> `xraydc.io.udkm`) and the new ones (`src/pxs_import/`,
> `pxs_import.reader`) side by side, because that mapping is what it is
> about. Everything it describes as "this repo" is xraydc.

**Status:** design, awaiting plan. No code written.
**Date:** 2026-10-22
**Target repositories:** a new `gitlab.com/kmc3-xpp/pxs-import`, cloned locally
next to this one at `…/GitHub/pxs-import`; and `xraydc` (this repo), which
loses `src/xraydc/io/` and everything that serves it.

---

## 1. Intent and why

`xraydc` is meant to stay what its name says: a generally useful Q-space
cooking layer, usable at KMC3-XPP, at MAX IV, at EuXFEL and in the Potsdam
lab alike. The `io/` subpackage is the opposite of that. It reads one
institution's data — PXS lab-source measurements of the UDKM group at U.
Potsdam — off one particular folder layout into nx5d repo format. It is
site-specific software that happens to live in a general-purpose package.

It has also outgrown its host. `io/` is 2819 LOC against 1650 for
`signal.py` + `cooking.py` + `udkm_xfel23.py`, and 11 of 17 test modules
(~2180 of 2930 test LOC) serve it. It drags `tifffile`, `silx` and `s3fs`
into xraydc's test extra, and it makes `pip install xraydc` install a
`udkm-prepare` console script that dies on `ModuleNotFoundError: silx`
unless the optional `[udkm]` extra was requested.

The split is cheap because the seam already exists: `io/` imports nothing
from `xraydc.signal`/`cooking`, and nothing in `xraydc` imports `io/`. The
only thing crossing the line is a string that nx5d resolves at runtime.

## 2. Goals

- A standalone package `pxs-import` (import name `pxs_import`) holding the
  whole importer, with its own git history starting at a single initial
  commit.
- Behaviour preserved exactly. This is a move, not a rewrite: same parsing,
  same attribute contract, same CLI semantics.
- All 26 corpus tests keep working — against a thinned corpus that is
  committed to the new repository, so they run anywhere rather than only on
  one machine.
- `xraydc` returns to `signal` + `cooking` + `udkm_xfel23`, with packaging,
  CI, docs and `CLAUDE.md` adjusted to match.
- The design documents and plans of the importer work move along as the new
  package's documentation, rewritten to the new names.
- The ~22 GB of raw measurement data under `notebooks/` leaves the disk
  once the corpus is extracted and verified — the raw subtrees only, not
  the analysis work that sits beside them.

## 3. Non-goals

- Fixing anything in `ISSUES.md`. Every item there is a `signal.py` /
  `cooking.py` defect and stays with xraydc.
- Changing the importer: no new scan types, no new attributes, no CLI
  changes beyond the rename.
- Pushing the cleaned xraydc back to `kmc3-xpp/xraydc`. That is a later
  decision; this work commits locally on `master` of `xraydc-2`.
- Creating a code dependency in either direction. A *test-only* dependency
  on xraydc (for the end-to-end cook) is allowed and expected.

## 4. Anti-goals — what makes this a failure even if it runs

- A mini corpus whose assertions were weakened to fit it. If a count no
  longer matches, the corpus is wrong, not the test. The only assertions
  that may change are ones naming a path that no longer exists.
- Measurement bulk in git. If the repository ends up at hundreds of MB, the
  thinning failed.
- `pxs_import` importing `xraydc` or `nx5d` at runtime. The coupling stays
  one-directional and string-resolved.
- Raw data deleted before the corpus is proven to carry the tests.

## 5. Constraints

- **nx5d coupling is by string, not import.** The reader is resolved as
  `module:callable` on `pm scan import -R …`; recipes are resolved as
  `pymod:///xraydc.cooking#sRSM`. Recipe URLs keep pointing at xraydc and
  must not be touched. The reader string is written by the importer itself
  (in `prepare.py`), so renaming it is safe — it is not persisted in any
  proposal's spice records.
- **`pm` is a subprocess, not an import.** A current nx5d install stays a
  runtime prerequisite of `pxs-prepare` without being an import-time one.
- **GPLv3+**, `setuptools_scm` versioning, GitLab CI — mirror xraydc's
  existing setup rather than inventing a new one.
- Windows + OneDrive working copy; `.gitattributes` forces LF.
- The raw proposal folders also exist on the group server, so the deletion
  at the end is recoverable.
- Python ≥ 3.10. (xraydc's lower bound comes from `xrayutilities`, which
  the new package does not depend on; keeping 3.10 avoids a second answer
  to the same question.)

## 6. Decisions already made

These were settled in conversation; the builder implements them rather than
reconsidering them.

| Decision | Value |
|---|---|
| Distribution / import name | `pxs-import` / `pxs_import` |
| Scope | PXS lab source only. Other UDKM instruments have different formats and are explicitly out of scope. |
| History | Fresh. One initial commit. No `git filter-repo` transplant. |
| Module names | Drop the `udkm_` prefix; it is redundant inside `pxs_import`. |
| Console script | `udkm-prepare` → `pxs-prepare` |
| Reader callable | `scans_from_udkm` → `scans_from_pxs`, so the nx5d reader string becomes `pxs_import.reader:scans_from_pxs`. No backwards-compatible alias: the clean break is affordable because the string is only used at import time. |
| Hosting | New GitLab project under `kmc3-xpp`, beside xraydc. **Public**, like xraydc. |
| Corpus | Thinned and committed into the new repo (option "curated mini corpus"). Its real measurement text — sample descriptions, fluences, spice records — is publishable as-is, so nothing is scrubbed and no assertion changes. |
| Raw data | Only the **raw subtrees** are deleted at the end: the `YYYYMMDD/` scan folders, `Pilatus tif images/`, `raw_data_old/` and the `*.zip` siblings. Labbook notebooks, `pictures/`, `RSM_export_plots/`, `results_analysis/`, `scans/` and the derived `*.h5` files stay — they are local analysis work, not covered by the group-server copy, and not the subject of this split. |

### Names that keep `udkm`

The prefix drops from *module* names. Three names are not module names and
are not redundant, because they are contracts with things outside this
repository. They stay exactly as they are (my call, and consistent with
"replace the prefix only where it carries meaning"):

- `s3.MANIFEST_NAME = '.udkm-import.json'` — this file already exists in
  the live bucket (`s3://pxsrepo/<proposal>/.udkm-import.json`). Renaming
  it makes every imported proposal look un-imported and silently re-uploads
  it from scratch.
- `sources.BASE_ENV_VAR = 'UDKM_BASE'` — documented in the how-to and set
  on real machines. `PXS_BASE` is additionally accepted as an alias, with
  `UDKM_BASE` winning if both are set.
- the `udkm_source` key in the reader's nx5d scan-key dict — it feeds
  `scan_format` templating and could be referenced from a spice record.

Module mapping:

| Today | In `pxs_import` |
|---|---|
| `xraydc/io/udkm_format.py` | `format.py` |
| `xraydc/io/udkm_sources.py` | `sources.py` |
| `xraydc/io/udkm_frames.py` | `frames.py` |
| `xraydc/io/udkm.py` | `reader.py` |
| `xraydc/io/udkm_spice.py` | `spice.py` |
| `xraydc/io/udkm_prepare.py` | `prepare.py` |
| `xraydc/io/udkm_s3.py` | `s3.py` |

`format.py` is deliberate: the module's own vocabulary throughout is "the
UDKM on-disk format". It shadows nothing at runtime.

## 7. What moves and what stays

**Moves to `pxs-import`:**

- `src/xraydc/io/*.py` (7 modules, 2819 LOC) → `src/pxs_import/`.
- `tests/udkm_*.py` (11 modules), `tests/udkm_fixtures.py`, and
  `tests/conftest.py` — the `FakeFS` stand-in in `conftest.py` has no other
  user in the repo, so it moves whole.
- `doc/pages/howto-udkm-import.md` → the new package's how-to page.
- `docs/superpowers/specs/2026-10-19…`, `2026-10-20…`, `2026-10-21…` (4
  specs) and the matching 4 plans — all four are 100 % importer work. They
  move, and every `xraydc.io.…` reference in them is rewritten.
- `notebooks/202601_PtAu/data-prepare.py` → `examples/data-prepare.py`. It
  is the only tracked file under `notebooks/` and it calls
  `prepare_proposal`.
- This design document, as the record of the split.

**Stays in `xraydc`:** `signal.py`, `cooking.py`, `udkm_xfel23.py`,
`__init__.py`; `qmapper_test.py`, `rsm_test.py`, and the two orphaned,
already-disabled nx5d leftovers (`test_xrd_helpers.py`,
`xrd_data_test.py` — out of scope here); `ISSUES.md`;
`doc/pages/index.md` and `api.md`; `scripts/release.sh`; the CI file.

## 8. The mini corpus

### The principle

**Structure and text are real; pixels are not.**

No corpus test reads a pixel value. They assert vocabularies, counts,
attributes and dimension names. `load_stack` takes the frame shape from the
first readable file and checks later frames against *that*, never against
the ROI in `parameters*.txt`. So of the 22 GB of TIFFs, what the tests
actually need is **names, counts and shape** — not content.

### Frames

Two flavours, chosen per scan by what the tests do with it:

- **Readable stub** — real pixel geometry (e.g. 195×487 for the Pilatus
  100K), deflate-compressed, filled with one constant synthetic Bragg
  pattern. Compression makes it ~1 kB on disk instead of 375 kB, and
  because every such file is byte-identical, git stores exactly **one**
  blob for all of them regardless of count. Used wherever a test opens
  frames: all of `202601_PtAu` (the end-to-end import reads every scan),
  `202602_FeAu/20260212/180247` (`load_stack`), and the alignment and
  cryostat scans that are imported end-to-end.
- **Zero-byte placeholder** — used where only existence and name matter:
  `plan_frames` does `is_file()`, unreferenced frames are *never opened*,
  and truncation is decided by the contiguous run of existing names. This
  is what keeps `120401`'s 751 unreferenced files and `125733`'s truncation
  at 489 of 1729 rows genuinely real at near-zero cost.

A scan that no test ever imports needs no TIFFs at all.

### Text

- `parameters*.txt`, `scan data*.txt` (cryostat logs), `spice/`,
  `spice.pre-migration/`, `scan-mapping.csv` and friends: carried verbatim.
  They are the substance the corpus tests exist for and they are small.
- Signature `.dat` tables (`rockingcurves*`, `scans*`, `allSteps*`,
  `scan*`) are the real bulk of the text layer — one row per frame, 155 MB
  across the surveyed proposals. Carried verbatim **only where a test
  parses them**; a header-only stub elsewhere, since classification and
  date-range discovery look at the filename, not the contents.
- `averages*.dat` and other extras are not signature files and are dropped.

### Folders carried

| Folder | Why it is needed | Scans |
|---|---|---|
| `202601_PtAu` | end-to-end import + cook, spice migration (11 rewrites), vocabulary, classification | 15, full |
| `202602_FeAu` | vocabulary, classification counts, and the three frame-quirk tests | 38; full `.dat` + frame names for `120401`, `180247`, `125733` |
| `202608_DyCo5Pt` | vocabulary, spice migration no-op, classification counts | 18 |
| `202504_NbO2` | date-range discovery counts across all four acquisition programs; alignment vocabulary | 63 |
| `20250408`, `20250424` | alignment classification, stage vocabulary, rows-match-frames, end-to-end | 19 |
| `20151226` | the only material with cryostat logs | 6 |

### Where it lives and how it is made

- Committed under `tests/corpus/` in the new repo; the `NB` constant in the
  corpus test module points there instead of at `notebooks/`.
- Produced by a committed, re-runnable `tools/thin_corpus.py` that takes
  `--source` (the original proposal root) and a declarative per-folder rule
  table, and writes the corpus tree. It is the provenance record: it says
  in code which files were kept verbatim, which were stubbed, and which
  frames were synthesised.
- Because the data now ships with the repository, the `corpus` marker stops
  being deselected by default. But the three end-to-end tests import a
  whole proposal through `pm` — PtAu alone is 15 scans and 6042 frames,
  several minutes and a multi-GB `tmp_path` — so they get a `slow` marker
  and `addopts` becomes `-ra -m 'not slow'`. Bare `pytest` then runs every
  cheap corpus test on real files and still finishes in seconds; `pytest -m
  slow` runs the imports. This also disposes of the `xfel` leftover that
  matched nothing. The `nx5d` marker and the `importorskip` guards stay,
  since nx5d is not installable from PyPI.
- **Signature `.dat` tables are carried verbatim where they are parsed,
  even when they are large** (`scans145450.dat` is 12 MB on its own; PtAu's
  tables total ~26 MB, `20151226`'s six ~8 MB). Row-reducing them would
  shrink the checkout but is exactly the "weakened corpus" this design
  forbids — the row counts are what the importer is being tested on. My
  call: pay the size.
- Expected weight: ~10–20 MB in git, ~45 MB checked out, ~10 000 stub TIFF
  files. The file count is the uncomfortable part on a OneDrive-synced
  Windows checkout; it is accepted rather than designed around.

## 9. The xraydc side

- Delete `src/xraydc/io/`, the 11 `udkm_*` test modules,
  `tests/udkm_fixtures.py` and `tests/conftest.py`.
- `pyproject.toml`: drop the `[udkm]` extra; drop `tifffile`, `silx`,
  `s3fs` from `test` and `all`; drop the `udkm-prepare` console script
  (which removes the "installs a command that cannot run" defect); drop the
  `corpus` and `nx5d` markers; `addopts` becomes `-ra`.
- `doc/mkdocs.yml`: remove the how-to nav entry.
- `CLAUDE.md`: rewritten. The `io/` rows leave the module table, and the
  whole "Listing attributes" chapter and the `io/`-specific parts of the
  nx5d-relationship section move to the new repo's own `CLAUDE.md`. What
  stays is the pre-fork picture plus a short pointer that the PXS importer
  now lives in `pxs-import`.
- `notebooks/` goes back to just `.gitkeep`; the `.gitignore` entry stays.
- `ISSUES.md` is untouched.

## 10. The new repo's skeleton

`pyproject.toml` (name `pxs-import`, runtime deps `numpy`, `tifffile`,
`silx`; `s3` extra for `s3fs`; `test` extra mirroring them plus `pytest`,
`pytest-cov`; note in a comment that the end-to-end corpus test additionally
wants `xraydc` and `nx5d` and skips without them), `.gitlab-ci.yml` adapted
from xraydc's (`coverage --source=pxs_import`, same pinned `python:3.13`
image and the same reason for the pin), `.gitattributes`, `.gitignore`,
`README.md`, `doc/mkdocs.yml` + pages, `CLAUDE.md`, `scripts/release.sh`,
`examples/data-prepare.py`, `tools/thin_corpus.py`, `tests/`,
`tests/corpus/`.

No `xarray`, `dask`, `xrayutilities` or `tqdm`: none of `io/` imports them.

## 11. Order of work and verification

1. New repo skeleton; code moved and renamed; tests moved and renamed; full
   suite green except the corpus tests, which are skipped for want of data.
2. `tools/thin_corpus.py` written; corpus extracted into `tests/corpus/`;
   **every corpus test green with its assertions unchanged**. This is the
   gate: a corpus test that needs its numbers adjusted means the thinning
   dropped something it should have kept.

   What counts as evidence: all 26 green on a developer machine with nx5d
   *and* xraydc installed. CI cannot close this gate — neither package is
   on PyPI — so there the end-to-end tests skip and the pipeline shows
   24 green + 2 skipped. A green pipeline is therefore **not** proof that
   the split works; the local run is. The end-to-end test currently guards
   only `importorskip('nx5d')`, while a missing `xraydc` would surface as a
   cook failure rather than a skip (the recipe URL
   `pymod:///xraydc.cooking#sRSM` is resolved by nx5d at cook time): add
   the matching `importorskip('xraydc')`.
3. Docs, `CLAUDE.md`, CI; initial commit. The GitLab project is created and
   the push happens only after explicit confirmation.
4. xraydc cleanup; its suite green; `python -c "import xraydc"` must not
   pull `silx`/`tifffile` into `sys.modules` (the packaging test's
   assertion survives in a slimmer form, and gains a mirror in the new
   package: importing `pxs_import` must not pull `s3fs`).
5. Only then, and only after an explicit confirmation that lists them: the
   raw subtrees are deleted from disk — the `YYYYMMDD/` scan folders,
   `Pilatus tif images/`, `raw_data_old/` and the `*.zip` siblings under
   the alignment day folders. Everything else under `notebooks/` stays
   untouched. The deletion is driven from a printed list the builder shows
   first, not from a glob applied blind.

## 12. Left to the builder

- The exact per-scan rule table for the thinning script, derived from what
  each corpus test touches. The table above gives the folders and the
  reasons; the per-scan detail is a reading exercise over
  `udkm_corpus_test.py`.
- Updating every `xraydc.io.…` string in docstrings, docs, specs, plans and
  error messages after the rename, including the reader string that
  `prepare.py` passes to `pm scan import -R …`.
- How much of xraydc's `CLAUDE.md` prose to carry over verbatim versus
  rewrite for the new package's point of view.
- CI details beyond mirroring: whether the corpus tests get their own job.
