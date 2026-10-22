# PXS Importer Extraction Implementation Plan

> **Historical note.** This is the design and plan of the extraction that
> created this package, written in the `xraydc` repository before the move.
> It is carried verbatim: it names the old module paths (`src/xraydc/io/`,
> `xraydc.io.udkm`) and the new ones (`src/pxs_import/`,
> `pxs_import.reader`) side by side, because that mapping is what it is
> about. Everything it describes as "this repo" is xraydc.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the UDKM/PXS importer out of `xraydc` into a standalone,
publishable package `pxs-import` that carries its own thinned test corpus, and
leave `xraydc` as the general-purpose cooking layer it was before the fork.

**Architecture:** The seam already exists — `src/xraydc/io/` imports nothing
from `xraydc` and nothing imports it. The move is therefore a rename plus a
packaging change, not a redesign. The one genuinely new artefact is
`tools/thin_corpus.py`, which turns 22 GB of real proposal folders into a
~45 MB corpus that keeps every structure and every text file the tests read
and replaces only pixel content.

**Tech Stack:** Python ≥ 3.10, setuptools + setuptools_scm, pytest, numpy,
tifffile, silx, s3fs (optional). No xarray/dask/xrayutilities in the new
package.

**Spec:** [docs/superpowers/specs/2026-10-22-pxs-import-extraction-design.md](../specs/2026-10-22-pxs-import-extraction-design.md)

## Global Constraints

- New package: distribution name `pxs-import`, import name `pxs_import`,
  at `C:\Users\aleks\OneDrive\Dokumente\GitHub\pxs-import`, remote
  `gitlab.com/kmc3-xpp/pxs-import`, **public**.
- Fresh history: exactly one initial commit for the whole new package.
  Commits named in tasks 1–5 are staged work *inside* that single commit —
  do the work, keep the tree clean between tasks with `git add -A` and a
  `git commit --amend` onto the initial commit, or commit normally and
  squash before the push. Never `git filter-repo` from xraydc.
- `pxs_import` must not import `xraydc` or `nx5d` at runtime. `nx5d` is
  reached only as the `pm` subprocess.
- Module renames: `udkm_format→format`, `udkm_sources→sources`,
  `udkm_frames→frames`, `udkm→reader`, `udkm_spice→spice`,
  `udkm_prepare→prepare`, `udkm_s3→s3`.
- Renamed public names: `scans_from_udkm→scans_from_pxs`; console script
  `udkm-prepare→pxs-prepare`; reader string
  `xraydc.io.udkm:scans_from_udkm → pxs_import.reader:scans_from_pxs`.
- **Names that keep `udkm`, verbatim:** `s3.MANIFEST_NAME = '.udkm-import.json'`,
  `sources.BASE_ENV_VAR = 'UDKM_BASE'` (with `PXS_BASE` as an additional
  alias, `UDKM_BASE` winning when both are set), and the `udkm_source` key
  in the reader's nx5d scan-key dict.
- Python ≥ 3.10, GPLv3+, authors as in xraydc's `pyproject.toml` plus
  Alexander von Reppert.
- Corpus text is carried verbatim; no assertion in `corpus_test.py` may be
  changed except a path constant. If a number no longer matches, the
  thinning is wrong.
- `.gitattributes` forces LF, as in xraydc.

## Review Focus

Five things the spec implies that no task's happy path exercises. Each has a
test in the task named.

1. **Both base env vars set.** `UDKM_BASE` and `PXS_BASE` together must
   resolve to `UDKM_BASE`, and `PXS_BASE` alone must work. (Task 3)
2. **CRLF corpus files after LF normalisation.** The real `.dat` and
   `parameters*.txt` are CRLF/latin-1; `.gitattributes` normalises text on
   commit. A carried file must still parse after a round trip. (Task 4)
3. **`tools/thin_corpus.py` run twice.** Re-running over an existing corpus
   tree must be safe and produce the same tree, or the provenance claim is
   worthless. (Task 4)
4. **`import pxs_import` must not pull `s3fs`/`fsspec`/`boto3`.** The s3
   imports are function-local on purpose and a careless move breaks that
   silently. (Task 2)
5. **The S3 manifest name must not drift.** A later "finish the rename"
   cleanup that renames `.udkm-import.json` re-uploads every proposal.
   Pin it with a test that states why. (Task 3)

---

### Task 1: New repository skeleton

**Files:**
- Create: `../pxs-import/pyproject.toml`, `.gitignore`, `.gitattributes`,
  `.gitlab-ci.yml`, `README.md`, `src/pxs_import/__init__.py`,
  `tests/.gitkeep`
- Reference: `pyproject.toml`, `.gitlab-ci.yml`, `.gitattributes`,
  `.gitignore` of this repo

**Interfaces:**
- Produces: an installable empty package `pxs_import` with a `test` extra,
  and a git repository with one commit.

- [ ] **Step 1: Create the directory and `git init`**

`git init` in `C:\Users\aleks\OneDrive\Dokumente\GitHub\pxs-import`. No
remote yet — the GitLab project is created in Task 5.

- [ ] **Step 2: Write `pyproject.toml`**

Copy xraydc's structure. Differences:
`name = "pxs-import"`; `description = "Import PXS lab-source (UDKM) scan folders into nx5d repo format"`;
`dependencies = ["numpy", "tifffile", "silx"]`;
`[project.optional-dependencies]` `s3 = ["s3fs"]`,
`test = ["pytest", "pytest-cov", "s3fs"]`,
`doc = ["mkdocs", "mkdocstrings[python]", "mkdocs-jupyter"]`;
`[project.scripts] pxs-prepare = "pxs_import.prepare:main"`;
`[tool.setuptools_scm] write_to = "src/pxs_import/_version.py"`;
`[tool.pytest.ini_options] addopts = "-ra -m 'not slow'"`,
`markers = ["corpus: runs against the committed corpus under tests/corpus/", "nx5d: needs an installed nx5d", "slow: whole-proposal imports through pm (minutes, GB of tmp)"]`.
Add a comment on the `test` extra saying the end-to-end tests additionally
want `nx5d` and `xraydc`, which are not on PyPI and are skipped without.

- [ ] **Step 3: Write `.gitignore`, `.gitattributes`, `README.md`, `.gitlab-ci.yml`**

`.gitignore` copied from xraydc minus the `notebooks/` stanza, plus
`_version.py`. `.gitattributes` copied verbatim. `.gitlab-ci.yml` copied
with `coverage run --source=pxs_import`, keeping the pinned `python:3.13`
image **and its comment explaining the pin**. `README.md`: what the package
does, the `pxs-prepare` command, the nx5d reader string, and a pointer to
xraydc for the cooking side.

- [ ] **Step 4: Write `src/pxs_import/__init__.py`**

Docstring naming the package's job; re-export nothing yet.

- [ ] **Step 5: Verify the install**

Run: `pip install -e ".[test]"` then `python -c "import pxs_import"`
Expected: both succeed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: pxs-import package skeleton"
```

---

### Task 2: Move and rename the seven modules

**Files:**
- Create: `../pxs-import/src/pxs_import/{format,sources,frames,reader,spice,prepare,s3}.py`
- Create: `../pxs-import/tests/{format,sources,frames,reader,build,prepare,s3,spice,nx5d,packaging}_test.py`,
  `tests/fixtures.py`, `tests/conftest.py`
- Source: this repo's `src/xraydc/io/*.py`, `tests/udkm_*.py`

**Interfaces:**
- Produces: every public name of today's `xraydc.io.*` under its new module
  path, unchanged except `scans_from_udkm → scans_from_pxs`.

- [ ] **Step 1: Copy the modules under their new names**

Copy file-for-file per the mapping in Global Constraints. No logic edits in
this step.

- [ ] **Step 2: Rewrite imports, docstring references and strings**

Every `from xraydc.io.udkm_x import` becomes `from pxs_import.x import`;
every `:mod:`xraydc.io.udkm_x`` / `:func:`~xraydc.io...`` reference follows.
In `prepare.py`, both `'-R', 'xraydc.io.udkm:scans_from_udkm'` occurrences
(today at `udkm_prepare.py:259` and `:508`) become
`'-R', 'pxs_import.reader:scans_from_pxs'`, and the module docstring with
them. Rename `scans_from_udkm` to `scans_from_pxs` in `reader.py` and at
its call sites. Leave `MANIFEST_NAME`, `BASE_ENV_VAR` and the `udkm_source`
scan key untouched.

- [ ] **Step 3: Copy the test modules under their new names**

`udkm_format_test.py→format_test.py`, `udkm_sources_test.py→sources_test.py`,
`udkm_frames_test.py→frames_test.py`, `udkm_reader_test.py→reader_test.py`,
`udkm_build_test.py→build_test.py`, `udkm_prepare_test.py→prepare_test.py`,
`udkm_s3_test.py→s3_test.py`, `udkm_spice_test.py→spice_test.py`,
`udkm_nx5d_test.py→nx5d_test.py`, `udkm_packaging_test.py→packaging_test.py`,
`udkm_fixtures.py→fixtures.py`, `conftest.py` verbatim. Rewrite their
imports the same way. `udkm_corpus_test.py` is **not** moved here — it comes
with Task 4.

- [ ] **Step 4: Rewrite `packaging_test.py` for the new package**

Three tests, same shape as today's:
`test_core_import_does_not_pull_s3_deps` — `import pxs_import` must leave
`{'s3fs', 'fsspec', 'boto3', 'botocore'}` out of `sys.modules`;
`test_module_import_does_not_pull_s3_deps` — same assertion after
`import pxs_import.prepare`, which is the module that reaches s3;
`test_package_importable` — `importlib.import_module('pxs_import')`.
(Review Focus 4.)

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: every test passes; no corpus module collected yet.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: move the UDKM importer in as pxs_import"
```

---

### Task 3: The names that stay, and the base-path alias

**Files:**
- Modify: `../pxs-import/src/pxs_import/sources.py`
- Modify: `../pxs-import/tests/sources_test.py`, `tests/s3_test.py`

**Interfaces:**
- Consumes: `sources.resolve_sources(...)` and `sources.BASE_ENV_VAR` from Task 2.
- Produces: `sources.BASE_ENV_VARS: tuple[str, ...] = ('UDKM_BASE', 'PXS_BASE')`,
  with `BASE_ENV_VAR = BASE_ENV_VARS[0]` kept as the documented primary.

- [ ] **Step 1: Write the failing tests**

In `tests/sources_test.py`:

```python
def test_pxs_base_is_accepted_as_an_alias():
    assert resolve_base(None, environ={'PXS_BASE': '/env'},
                        exists=lambda p: False) == Path('/env')

def test_udkm_base_wins_when_both_are_set():
    assert resolve_base(None, environ={'UDKM_BASE': '/old', 'PXS_BASE': '/new'},
                        exists=lambda p: False) == Path('/old')
```

In `tests/s3_test.py`:

```python
def test_manifest_name_is_not_renamed_with_the_package():
    # Live buckets already hold this file; renaming it makes every imported
    # proposal look un-imported and silently re-uploads it.
    assert MANIFEST_NAME == '.udkm-import.json'
```

(Review Focus 1 and 5. Use the module's existing helper/fixture names
rather than the placeholders above if they differ.)

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/sources_test.py tests/s3_test.py -q`
Expected: the two `sources` tests fail (alias unknown); the manifest test
passes already and stays as a pin.

- [ ] **Step 3: Read both variables in `sources.py`**

Where `environ.get(BASE_ENV_VAR)` is read today (`udkm_sources.py:124`, in
`resolve_base(explicit=None, *, environ=None, exists=None)`),
read `BASE_ENV_VARS` in order and take the first set, non-empty value.
Update the docstring of the resolution routine and of `BASE_ENV_VAR` to
name both and say which wins.

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: accept PXS_BASE beside UDKM_BASE"
```

---

### Task 4: The thinned corpus

**Files:**
- Create: `../pxs-import/tools/thin_corpus.py`
- Create: `../pxs-import/tests/corpus_test.py` (from this repo's `tests/udkm_corpus_test.py`)
- Create: `../pxs-import/tests/corpus/**` (generated, committed)

**Interfaces:**
- Consumes: `format.discover_scans`, `format.ScanFolder` from Task 2.
- Produces: `tools/thin_corpus.py` with
  `thin(source_root: Path, out_root: Path, rules: dict[str, FolderRule]) -> None`
  and a `__main__` taking `--source` and `--out`; a committed corpus tree
  under `tests/corpus/`.

**Rule table** — this is the decision the task exists to encode. For each
carried folder: which signature `.dat` files are verbatim, and which frames
are readable stubs (`R`), name-only zero-byte placeholders (`Z`) or absent
(`–`). Derived from what each corpus test touches.

| Folder | Scans | `.dat` verbatim | Frames |
|---|---|---|---|
| `202601_PtAu` | all 15 | all 15 (end-to-end imports every scan) | `R` all 6042 |
| `202602_FeAu` | all 38 | only `20260203/120401`, `20260212/180247`, `20260213/125733` | `Z` for 120401 (2115 names incl. 751 unreferenced) and 125733 (489); `R` for 180247 (172 rows, 165 present, 7 absent — the gaps are made by *omitting* names, not by empty files); `–` elsewhere |
| `202608_DyCo5Pt` | all 18 | none (classification + vocabulary only) | `–` |
| `202504_NbO2` | all 63 | none (date-range counts read filenames) | `–` |
| `20250408`, `20250424` | 19 | all 19 (`rows_match_frames` and end-to-end) | `R` all 900 |
| `20151226` | all 6 | only `000051` | `R` 298 for `000051`; `–` elsewhere |

Everything else is carried verbatim in every folder: `parameters*.txt`,
`scan data*.txt`, `spice/`, `spice.pre-migration/`. A stubbed `.dat` keeps
its real header line(s) and no data rows. `averages*.dat`, `*.png`, `*.zip`,
notebooks, `pictures/`, `results_analysis/`, `scans/`, `*.h5` are not
carried.

- [ ] **Step 1: Write the failing test for the thinning tool**

`tests/tools_test.py`, against a tiny synthetic source tree built in
`tmp_path` (reuse `tests/fixtures.py` helpers):

```python
def test_thin_keeps_text_and_shrinks_frames(tmp_path): ...
    # parameters*.txt byte-identical; .dat verbatim where the rule says so,
    # header-only where it does not; every planned frame name present;
    # a readable frame opens with tifffile and has the source frame's shape;
    # a placeholder frame is zero bytes.

def test_thin_is_idempotent(tmp_path): ...
    # running thin() twice produces a byte-identical tree (Review Focus 3)

def test_thinned_dat_survives_lf_normalisation(tmp_path): ...
    # write a CRLF/latin-1 .dat through thin(), re-read it with
    # format.parse_dat after converting line endings to LF, and get the
    # same rows (Review Focus 2)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/tools_test.py -q`
Expected: FAIL, `tools.thin_corpus` not importable.

- [ ] **Step 3: Implement `tools/thin_corpus.py`**

`FolderRule` is a dataclass holding the rule-table row (which scans, which
`.dat` are verbatim, the frame flavour per scan). `thin()` walks
`discover_scans(source_root/folder)` and writes the out tree. The readable
stub is one `tifffile.imwrite(..., compression='deflate')` of a constant
synthetic frame at the **source frame's own shape**, read once per scan from
its first real frame (`tifffile.imread(...).shape`) so detector geometry is
never guessed. Identical stubs are byte-identical on purpose: git stores one
blob. Placeholders are created with `Path.touch()`.

- [ ] **Step 4: Run the tool's tests**

Run: `pytest tests/tools_test.py -q`
Expected: PASS.

- [ ] **Step 5: Generate the corpus**

Run:
`python tools/thin_corpus.py --source "C:/Users/aleks/OneDrive/Dokumente/GitHub/xraydc-2/notebooks" --out tests/corpus`
Then report `du -sh tests/corpus` and the file count. Expected order of
magnitude: ~45 MB, ~10 000 files. A result far above that means a rule was
read as "verbatim" where it should stub.

- [ ] **Step 6: Move `corpus_test.py` and repoint it**

Copy `tests/udkm_corpus_test.py` → `tests/corpus_test.py`. The **only**
permitted edits: the imports (`xraydc.io.*` → `pxs_import.*`,
`scans_from_udkm` → `scans_from_pxs`), the `NB` constant
(`Path(__file__).resolve().parent / 'corpus'`), and adding
`@pytest.mark.slow` plus `pytest.importorskip('xraydc')` to
`test_ptau_end_to_end_cooks_srsm_and_refuses_xrss` and
`test_alignment_corpus_end_to_end`. Every assertion stays as it is.

- [ ] **Step 7: Run the corpus suite — the gate**

Run: `pytest -m corpus -q` and then `pytest -m slow -q` (the latter needs
nx5d and xraydc installed).
Expected: all 26 pass. **If any assertion disagrees with the corpus, fix
the corpus, not the test.** Record the two commands and their output in the
commit message.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "test: carry a thinned real corpus and the tool that makes it"
```

---

### Task 5: Documentation, examples, and publication

**Files:**
- Create: `../pxs-import/CLAUDE.md`, `doc/mkdocs.yml`, `doc/pages/index.md`,
  `doc/pages/api.md`, `doc/pages/howto.md`, `examples/data-prepare.py`,
  `scripts/release.sh`, `docs/superpowers/specs/*`, `docs/superpowers/plans/*`
- Source: this repo's `doc/pages/howto-udkm-import.md`, `CLAUDE.md`,
  `scripts/release.sh`, the four importer specs and four plans,
  `notebooks/202601_PtAu/data-prepare.py`

**Interfaces:**
- Consumes: the finished package from Tasks 2–4.

- [ ] **Step 1: Carry the design history over**

Copy the four `docs/superpowers/specs/2026-10-1*/2026-10-2*` importer specs,
their four plans, and this plan and its spec. Rewrite every `xraydc.io.…`,
`udkm-prepare` and `scans_from_udkm` reference to the new names. Add a line
at the top of each moved document saying it was written while the code
lived in xraydc and what its names are now.

- [ ] **Step 2: Write `CLAUDE.md`**

Carry over, rewritten for the new package: the nx5d string-resolution
section (reader side), the whole "Listing attributes" chapter, the module
table with the new names and LOC, the corpus and marker conventions
(`corpus`, `nx5d`, `slow`), the rule that `pxs_import` never imports nx5d or
xraydc, and the `MANIFEST_NAME`/`BASE_ENV_VAR` warnings from Global
Constraints.

- [ ] **Step 3: Docs site and example**

`doc/mkdocs.yml` copied with `site_name: pxs-import`, nav Home/API/How-to.
`doc/pages/howto.md` from `howto-udkm-import.md` with the new command name.
`doc/pages/api.md` with mkdocstrings entries for `reader.scans_from_pxs`,
`prepare.prepare_proposal`, `format.discover_scans` and the dataclasses.
`examples/data-prepare.py` from `notebooks/202601_PtAu/data-prepare.py`,
importing `pxs_import.prepare`. `scripts/release.sh` copied, with the
package name swapped.

- [ ] **Step 4: Verify the docs build**

Run: `mkdocs build -f doc/mkdocs.yml`
Expected: builds without warnings about missing references.

- [ ] **Step 5: Squash to a single initial commit**

```bash
git reset --soft $(git rev-list --max-parents=0 HEAD) && git commit --amend -m "feat: pxs-import, the PXS/UDKM scan importer for nx5d"
```
Verify with `git log --oneline` that exactly one commit exists.

- [ ] **Step 6: Create the GitLab project and push — ask first**

Stop and ask the user before this step runs. Then create the **public**
project `kmc3-xpp/pxs-import` (the API token is in the `GITLAB_TOKEN`
environment variable), add it as `origin`, and push `master`.

---

### Task 6: Clean up xraydc

**Files:**
- Delete: `src/xraydc/io/`, `tests/udkm_*.py`, `tests/udkm_fixtures.py`,
  `tests/conftest.py`, `doc/pages/howto-udkm-import.md`,
  `notebooks/202601_PtAu/data-prepare.py`, the eight moved specs/plans
- Modify: `pyproject.toml`, `doc/mkdocs.yml`, `CLAUDE.md`

**Interfaces:**
- Consumes: nothing. This task is deletion plus packaging.

- [ ] **Step 1: Delete the importer and its tests**

`git rm -r src/xraydc/io tests/udkm_*.py tests/udkm_fixtures.py tests/conftest.py`
and the doc page, example script and moved design documents.

- [ ] **Step 2: Update `pyproject.toml`**

Remove the `[udkm]` extra; remove `tifffile`, `silx`, `s3fs` from `test`
and `all`; remove the `[project.scripts]` table entirely; remove the
`corpus` and `nx5d` markers; `addopts = "-ra"`.

- [ ] **Step 3: Keep the import-hygiene assertion**

Create `tests/packaging_test.py` holding the one test from the deleted
`udkm_packaging_test.py` that is still about xraydc:
`test_core_import_does_not_pull_heavy_deps` — a subprocess running
`import sys, xraydc; assert not {'silx','tifffile','s3fs'} & set(sys.modules)`
must exit 0. Without it, nothing stops the dependency creeping back.

- [ ] **Step 4: Update `doc/mkdocs.yml` and `CLAUDE.md`**

Drop the how-to nav entry. Rewrite `CLAUDE.md`: module table back to the
three modules, the `io/`-specific half of the nx5d section and the whole
"Listing attributes" chapter removed, the test-count and convention lines
corrected, and a short paragraph saying the PXS importer now lives in
`kmc3-xpp/pxs-import` with the reader string it registers.

- [ ] **Step 5: Verify**

Run: `pytest -q`
Expected: the synthetic suite plus the new packaging test pass; nothing
references `xraydc.io`.
Run: `grep -rn "xraydc.io\|udkm-prepare" --include=*.py --include=*.md --include=*.toml --include=*.yml .`
Expected: hits only inside `CLAUDE.md`'s pointer paragraph and `ISSUES.md`
history, if anywhere.
Run: `python -c "import sys, xraydc; assert not {'silx','tifffile'} & set(sys.modules)"`
Expected: exits 0.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor!: move the PXS importer out to pxs-import"
```

---

### Task 7: Remove the raw data

**Files:** none in either repo. This task only deletes measurement data
from disk.

- [ ] **Step 1: Build and print the deletion list**

List, without deleting: every `notebooks/<proposal>/YYYYMMDD/` scan-day
folder, `notebooks/202504_NbO2/Pilatus tif images/`,
`notebooks/202504_NbO2/raw_data_old/`, the top-level `notebooks/20250408/`,
`20250424/`, `20151226/` day folders and the `*.zip` siblings inside them.
Print each path with its size and the total.

- [ ] **Step 2: Confirm with the user**

Show the list. Say explicitly what is **not** on it: Labbook notebooks,
`pictures/`, `export_pictures/`, `RSM_export_plots/`, `results_analysis/`,
`scans/`, `spice/`, `spice.pre-migration/`, `*.h5`, `*.ipynb`, the helper
`.py` files. Wait for a yes.

- [ ] **Step 3: Delete, then verify the corpus still carries the tests**

After deletion, run `pytest -m corpus -q` and `pytest -m slow -q` in
`pxs-import` once more. They must still pass — they now read only
`tests/corpus/`. This is the proof the deletion was safe.

---

## Notes for the executor

- Work in the new repository for Tasks 1–5 and in this one for Tasks 6–7.
  Do not start Task 6 before Task 4's gate has passed: until the corpus
  carries the tests, `src/xraydc/io/` is the only working copy of the
  importer.
- Task 4 Step 5 is the long one — thinning reads ~22 GB. Expect minutes,
  and run it from the Bash tool with a raised timeout rather than in
  pieces.
