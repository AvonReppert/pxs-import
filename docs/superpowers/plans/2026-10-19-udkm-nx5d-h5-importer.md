# UDKM → nx5d HDF5 Importer Implementation Plan

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an `pxs_import.reader` reader that `pm scan import` can drive to turn UDKM PXS scan folders into nx5d-native HDF5 (one file per scan, or one per proposal), plus the spice migration and the per-proposal preparation script around it.

**Architecture:** A pure-Python parsing layer (`format.py`) turns folders, `.dat` tables and `parameters*.txt` into plain data; a frame layer (`frames.py`) resolves the authoritative frame list and loads a `float32` stack; `reader.py` assembles a `silx.io.commonh5.File` per scan and exposes the `scans_from_pxs` generator that nx5d's `pm scan import` consumes. `spice.py` rewrites spice anchors to the new run numbers; `prepare.py` orchestrates numbering, overwrite refusal, the `pm` call and the migration, and is what the per-proposal `data-prepare.py` calls.

**Tech Stack:** Python ≥ 3.10, `numpy`, `tifffile`, `silx` (`silx.io.commonh5`), pytest. nx5d 3.2.1 (installed in `.venv`, not imported by the reader) supplies `pm`, the writers and the reading side.

**Spec:** `docs/superpowers/specs/2026-10-19-udkm-nx5d-h5-importer-design.md`

## Global Constraints

- The reader (`pxs_import/udkm*.py`) never imports `nx5d`, `h5py`, `zarr` or anything from `xraydc.signal`/`xraydc.cooking` (spec §6.5, §6.7). `prepare.py` may *shell out* to `pm`.
- Reader contract: `scans_from_pxs(source, **kwargs)` is a generator yielding `(kbase: dict, h5like)` with `kbase['scan'] == 'r0001'`-style keys, four digits (§6.5, §6.8).
- `tifffile` and `silx` live in the optional extra `udkm`; `pip install xraydc` without it must still `import xraydc` (§6.7, §11.13).
- Variable names are exactly those of spec §5.8; leaf basenames are unique within a scan (§6.3); the full table of truncated `xRSS` scans uses `<column>_full` names (§8.1).
- Frame stack is always `float32`; missing frames are NaN and flagged in `frame_missing` (§5.7).
- Required attributes on every scan node: `k-scan-type`, `k-frame-guard = 'instrument/pilatus/images'`, `k-primary:0` as a dataset **path**, `k-header:notes|integration|photon_energy|date_time|roi`, `udkm-source|date|time|datetime` (§6.2–§6.4).
- Scan discovery: directories matching `^\d{6}$` under directories matching `^\d{8}$`; classification by exact filenames `rockingcurves{sfx}.dat` → `sRSM`, `scans{sfx}.dat` → `tRSS`, `allSteps{sfx}.dat` → `xRSS` (§5).
- `ttheta` for `tRSS` is `2*theta + <2 Theta Correction>` from `parameters*.txt` (§5.2).
- `photon_energy` default `8048.0` eV, overridable (§5.8).
- Spice files are UUID-named with no extension; content is JSON; skip non-files and non-JSON (§9). The migrated `recipes` record must not contain `xRSS` (§8).
- All commands below run from the repo root with the repo venv: `.venv/Scripts/python` (Windows) — on POSIX substitute `.venv/bin/python`.

## Review Focus

1. A `^\d{6}$` directory with none of the three signature files (e.g. an aborted scan) — expected: skipped with a warning naming the folder, numbering unaffected. Pinned in Task 1.
2. A `.dat` whose last data row has fewer fields than the header (acquisition killed mid-write) — expected: that row dropped with a warning, not a crash or a misaligned table. Pinned in Task 3.
3. A `parameters*.txt` that is missing — expected: scan still converts, `notes` empty, `integration` absent, warning logged. Pinned in Task 2 and Task 5.
4. A TIFF whose shape differs from the first frame of the scan (ROI changed mid-beamtime, file from another scan) — expected: a clear error naming the file, not a broadcast error deep in numpy. Pinned in Task 4.
5. A spice anchor that sorts after every scan folder — expected: migration raises naming the anchor rather than silently dropping or mis-anchoring the record. Pinned in Task 8.

---

### Task 0: Packaging, extras and test markers

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies]`, `[tool.pytest.ini_options]`)
- Create: `src/pxs_import/__init__.py`
- Test: `tests/packaging_test.py`

**Interfaces:**
- Produces: importable package `pxs_import`; pytest markers `corpus`, `nx5d`; `.venv` with `pytest`, `tifffile`, `silx`.

- [ ] **Step 1: Write the failing test**

```python
# tests/packaging_test.py
import importlib, subprocess, sys

def test_core_import_does_not_pull_udkm_deps():
    code = ("import sys, xraydc; "
            "assert 'silx' not in sys.modules and 'tifffile' not in sys.modules")
    assert subprocess.run([sys.executable, '-c', code]).returncode == 0

def test_io_package_importable():
    assert importlib.import_module('pxs_import') is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/packaging_test.py -v`
Expected: FAIL — `No module named pytest` (venv lacks it) or `No module named 'pxs_import'`.

- [ ] **Step 3: Add the extra and markers**

In `pyproject.toml`: add `udkm = ["tifffile", "silx"]` under `[project.optional-dependencies]` and include both in `all`; set `markers = ["corpus: needs the real UDKM proposal folders under notebooks/", "nx5d: needs an installed nx5d"]` and `addopts = "-ra -m 'not xfel and not corpus'"`. Create `src/pxs_import/__init__.py` containing only a module docstring (`"""I/O helpers for xraydc: importers for raw beamtime data."""`).

- [ ] **Step 4: Install and run the tests**

Run: `.venv/Scripts/python -m pip install -e ".[udkm,test]"` then `.venv/Scripts/python -m pytest tests/packaging_test.py -v`
Expected: PASS ×2. Also `.venv/Scripts/pm --version` prints `3.2.1`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/pxs_import/__init__.py tests/packaging_test.py
git commit -m "build: add pxs_import package, [udkm] extra and corpus/nx5d pytest markers"
```

---

### Task 1: Scan discovery, classification and numbering

**Files:**
- Create: `src/pxs_import/format.py`
- Test: `tests/format_test.py`

**Interfaces:**
- Produces, in `pxs_import.format`:
  - `SIGNATURES: dict[str, str] = {'sRSM': 'rockingcurves{sfx}.dat', 'tRSS': 'scans{sfx}.dat', 'xRSS': 'allSteps{sfx}.dat'}`
  - `@dataclass(frozen=True) class ScanFolder: path: Path; date: str; time: str; scan_type: str` with properties `source -> str` (`'20260108/120821'`), `sfx -> str` (`== time`), `dat_path -> Path`, `params_path -> Path` (`parameters{sfx}.txt`).
  - `classify_scan(folder: Path) -> str | None` — scan type or `None` if no signature file.
  - `discover_scans(proposal_root: Path) -> list[ScanFolder]` — sorted by `date + time`; logs a warning per unclassifiable `^\d{6}$` directory.
  - `number_scans(scans: list[ScanFolder], scan_format: str = 'r{iscan:04d}') -> list[tuple[str, ScanFolder]]` — `[('r0001', sf), …]` in the same order.

- [ ] **Step 1: Write the failing tests**

```python
# tests/format_test.py
import logging
from pathlib import Path
from pxs_import.format import classify_scan, discover_scans, number_scans

def _mk(root: Path, date: str, time: str, signature: str | None, extra=()):
    d = root / date / time; d.mkdir(parents=True)
    if signature: (d / signature.format(sfx=time)).write_text('% x\n1\n')
    for e in extra: (d / e.format(sfx=time)).write_text('')
    return d

def test_classify_each_type(tmp_path):
    assert classify_scan(_mk(tmp_path, '20260108', '115550', 'rockingcurves{sfx}.dat',
                             extra=['intensities{sfx}.dat'])) == 'sRSM'
    assert classify_scan(_mk(tmp_path, '20260108', '120821', 'scans{sfx}.dat')) == 'tRSS'
    assert classify_scan(_mk(tmp_path, '20260108', '100331', 'allSteps{sfx}.dat',
                             extra=['averages{sfx}.dat'])) == 'xRSS'
    assert classify_scan(_mk(tmp_path, '20260108', '101010', None)) is None

def test_discover_skips_debris_and_sorts_across_dates(tmp_path, caplog):
    _mk(tmp_path, '20260203', '120401', 'scans{sfx}.dat')
    _mk(tmp_path, '20260202', '153026', 'rockingcurves{sfx}.dat')
    _mk(tmp_path, '20260202', '135936', 'allSteps{sfx}.dat')
    (tmp_path / '20260202' / '135936Graphics').mkdir()
    (tmp_path / '20260202' / '153026.zip').write_bytes(b'PK')
    (tmp_path / 'spice').mkdir()
    _mk(tmp_path, '20260202', '140000', None)             # Review Focus 1
    with caplog.at_level(logging.WARNING):
        scans = discover_scans(tmp_path)
    assert [s.source for s in scans] == ['20260202/135936', '20260202/153026', '20260203/120401']
    assert [s.scan_type for s in scans] == ['xRSS', 'sRSM', 'tRSS']
    assert '20260202/140000' in caplog.text

def test_number_scans_is_zero_padded_and_ordered(tmp_path):
    from pxs_import.format import ScanFolder
    scans = [ScanFolder(tmp_path, '20260108', t, 'sRSM') for t in ('100331', '115550')]
    assert [k for k, _ in number_scans(scans)] == ['r0001', 'r0002']
    assert number_scans(scans)[1][1].time == '115550'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/format_test.py -v`
Expected: FAIL with `ModuleNotFoundError: pxs_import.format`.

- [ ] **Step 3: Implement the interfaces above in `src/pxs_import/format.py`**

Module-level `logger = logging.getLogger(__name__)`. `discover_scans` iterates `sorted(proposal_root.iterdir())`, keeps directories matching `^\d{8}$`, then their subdirectories matching `^\d{6}$`, classifies each, warns and skips `None`. Imports: `dataclasses`, `logging`, `pathlib`, `re` only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/format_test.py -v`
Expected: PASS ×3.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py tests/format_test.py
git commit -m "feat(udkm): discover, classify and number UDKM scan folders"
```

---

### Task 2: `parameters*.txt` parsing

**Files:**
- Modify: `src/pxs_import/format.py`
- Test: `tests/format_test.py`

**Interfaces:**
- Produces, in `pxs_import.format`:
  - `parse_parameters(path: Path) -> dict[str, str]` — keys as in the file plus `'Comments'` (space-joined unkeyed leading lines, `''` if none). UTF-8 first, `windows-1252` fallback per line. Split with `split(': ', 1)`. Missing file → `{}` and a warning.
  - `exposure_seconds(value: str) -> float` — strips a trailing `s`.
  - `two_theta_correction(params: dict) -> float` — `float(params.get('2 Theta Correction', '0'))`.
  - `parse_date_time(value: str) -> str` — `'08.01.2026 12:08:21'` → `'2026-01-08T12:08:21'`.

- [ ] **Step 1: Write the failing tests**

```python
TRSS_PARAMS = ('16nm Pt on MgO\r\n157mW\r\n930x880µm²\r\n10.2mJ/cm²\r\n\r\n \r\n\r\n'
               'Date Time: 08.01.2026 12:08:21\r\n\r\nTheta Vector: 19.48\r\n'
               '2 Theta Correction: 0.000000\r\nExposure Time: 2.000000s\r\n'
               'Tmp Path: P:\\Pilatus\\2026\\20260108\\120821\r\nROI: (0, 0); (487, 195)\r\n')

def test_parse_parameters_windows1252_comments_and_keys(tmp_path):
    p = tmp_path / 'parameters120821.txt'
    p.write_bytes(TRSS_PARAMS.encode('windows-1252'))
    d = parse_parameters(p)
    assert d['Comments'] == '16nm Pt on MgO 157mW 930x880µm² 10.2mJ/cm²'
    assert d['Tmp Path'] == 'P:\\Pilatus\\2026\\20260108\\120821'
    assert d['ROI'] == '(0, 0); (487, 195)'
    assert two_theta_correction(d) == 0.0
    assert exposure_seconds(d['Exposure Time']) == 2.0
    assert parse_date_time(d['Date Time']) == '2026-01-08T12:08:21'

def test_parse_parameters_handles_second_colon_space(tmp_path):
    p = tmp_path / 'parameters000000.txt'
    p.write_text('Date Time: 08.01.2026 12:08:21\nNote: a: b\n')
    assert parse_parameters(p)['Note'] == 'a: b'

def test_parse_parameters_missing_file_is_empty_with_warning(tmp_path, caplog):   # Review Focus 3
    with caplog.at_level(logging.WARNING):
        assert parse_parameters(tmp_path / 'parameters999999.txt') == {}
    assert 'parameters999999.txt' in caplog.text

def test_exposure_seconds_bare_number():
    assert exposure_seconds('1.000000') == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/format_test.py -v -k parameters`
Expected: FAIL with `ImportError: cannot import name 'parse_parameters'`.

- [ ] **Step 3: Implement the four functions in `src/pxs_import/format.py`**

Read the file in binary, decode per line (`utf-8`, fallback `windows-1252`), `strip()`; empty lines are skipped and do **not** reset comment collection; once the first key has been seen, unkeyed lines are appended to the previous key with a space (nx5d behaviour, §5.4). `parse_date_time` uses `datetime.strptime(value, '%d.%m.%Y %H:%M:%S').isoformat()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/format_test.py -v`
Expected: PASS ×7.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py tests/format_test.py
git commit -m "feat(udkm): tolerant parameters*.txt parser"
```

---

### Task 3: `.dat` table parsing

**Files:**
- Modify: `src/pxs_import/format.py`
- Test: `tests/format_test.py`
- Create: `tests/fixtures.py` (synthetic scan folder writers, reused by later tasks)

**Interfaces:**
- Produces, in `pxs_import.format`:
  - `@dataclass class DatTable: columns: dict[str, numpy.ndarray]` (verbatim header name → `(N,)` float64), `profile: numpy.ndarray | None` (`(N, W)` float64), `image_names: list[str] | None`, `start_times: list[str] | None`, `end_times: list[str] | None`; property `n_rows -> int`.
  - `parse_dat(path: Path, scan_type: str) -> DatTable`. For `sRSM`/`tRSS` the last declared column is the profile and `%`-lines after the header carry `StartTime`/`EndTime`/`ImageName` per row; `xRSS` has no profile and no per-row metadata. `NaN` parses as NaN. A data row with fewer fields than `len(header)` is dropped with a warning.
- Produces, in `tests/fixtures.py`:
  - `write_srsm(folder: Path, n_rows: int, width: int = 6, shape=(4, 6), images: bool = True, extra_tifs: int = 0, missing: tuple[int, ...] = (), params: dict | None = None) -> Path`
  - `write_trss(folder, n_rows, width=6, shape=(4, 6), theta=19.48, correction=0.0, images=True, extra_tifs=0, missing=(), params=None) -> Path`
  - `write_xrss(folder, n_rows, shape=(4, 6), n_tifs: int | None = None, params=None) -> Path`
  - Each writes the signature `.dat` with the real header lines of spec §5.1–§5.3, `parameters{sfx}.txt` (Windows-1252, with `Date Time`, `Exposure Time`, `ROI`, `2 Theta Correction` where applicable, and `Comments` lines from `params.get('Comments')`), and `image{i:05d}.tif` as `int32` arrays of `shape` filled with `i` via `tifffile.imwrite`, skipping indices in `missing`. `folder.name` is the `sfx`. `extra_tifs` adds unreferenced files after the last referenced index.

- [ ] **Step 1: Write the fixture module and the failing tests**

```python
def test_parse_srsm_table(tmp_path):
    d = write_srsm(tmp_path / '20260108' / '115550', n_rows=3, width=6, images=False)
    t = parse_dat(d / 'rockingcurves115550.dat', 'sRSM')
    assert set(t.columns) == {'Loop', 'Theta / deg.', '2Theta + Corr / deg.', 'Crystal', 'Crystal Calib'}
    assert t.profile.shape == (3, 6) and t.n_rows == 3
    assert t.image_names == ['image00000.tif', 'image00001.tif', 'image00002.tif']

def test_parse_trss_table(tmp_path):
    d = write_trss(tmp_path / '20260108' / '120821', n_rows=2, width=6, images=False)
    t = parse_dat(d / 'scans120821.dat', 'tRSS')
    assert len(t.columns) == 9 and 'Intensities [ph/s]' not in t.columns
    assert t.profile.shape == (2, 6) and len(t.start_times) == 2

def test_parse_xrss_table_keeps_nan(tmp_path):
    d = write_xrss(tmp_path / '20260108' / '100331', n_rows=2)
    p = d / 'allSteps100331.dat'
    p.write_text(p.read_text().replace('10.078973', 'NaN', 1))
    t = parse_dat(p, 'xRSS')
    assert t.profile is None and t.image_names is None and len(t.columns) == 7
    assert numpy.isnan(t.columns['Position Real']).sum() == 1

def test_parse_dat_drops_short_trailing_row(tmp_path, caplog):           # Review Focus 2
    d = write_xrss(tmp_path / '20260108' / '100331', n_rows=3)
    p = d / 'allSteps100331.dat'
    p.write_text(p.read_text().rstrip('\n').rsplit('\t', 3)[0] + '\n')
    with caplog.at_level(logging.WARNING):
        t = parse_dat(p, 'xRSS')
    assert t.n_rows == 2 and 'row 3' in caplog.text
```

The fixture's `write_xrss` must emit `10.078973` as the first `Position Real` value so the NaN substitution above has a target.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/format_test.py -v -k parse_`
Expected: FAIL with `ImportError: cannot import name 'parse_dat'`.

- [ ] **Step 3: Implement `DatTable` and `parse_dat`**

Header = first `%` line split on `\t` after stripping `% `. For `sRSM`/`tRSS` the second `%` line is the metadata header and every subsequent `%` line belongs to the *next* data row. Scalars = `len(header) - 1` columns when a profile exists, else `len(header)`. Convert with `numpy.array(fields, dtype=float)` (handles `NaN`). Only `numpy` and stdlib imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/format_test.py -v`
Expected: PASS ×11.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py tests/format_test.py tests/fixtures.py
git commit -m "feat(udkm): parse rockingcurves/scans/allSteps tables; synthetic scan fixtures"
```

---

### Task 4: Frame plan and `float32` stack loading

**Files:**
- Create: `src/pxs_import/frames.py`
- Test: `tests/frames_test.py`

**Interfaces:**
- Consumes: `DatTable`, `ScanFolder` from Task 1/3.
- Produces, in `pxs_import.frames`:
  - `@dataclass class FramePlan: files: list[Path | None]` (one entry per kept row; `None` = referenced but missing), `n_rows: int`, `truncated: bool`, `unreferenced: list[Path]`; property `n_frames -> int`.
  - `plan_frames(folder: Path, table: DatTable, scan_type: str) -> FramePlan`. `sRSM`/`tRSS`: `files[i] = folder / table.image_names[i]`, `None` if absent; `truncated=False`. `xRSS`: `files = [folder/'image{i:05d}.tif' for i in range(n_img)]` where `n_img` is the length of the contiguous run from index 0; `truncated = n_img < n_rows`. `unreferenced` = `*.tif` present but not planned (warning with count, never loaded).
  - `load_stack(plan: FramePlan) -> tuple[numpy.ndarray, numpy.ndarray]` — `(stack float32 (N,H,W), missing bool (N,))`; NaN where `files[i] is None`; warning naming each missing file; `ValueError` naming the file whose shape differs from the first loaded frame.

- [ ] **Step 1: Write the failing tests**

```python
# tests/frames_test.py
import logging, numpy, pytest, tifffile
from pxs_import.format import parse_dat
from pxs_import.frames import plan_frames, load_stack
from fixtures import write_srsm, write_trss, write_xrss

def test_unreferenced_tifs_are_ignored(tmp_path, caplog):
    d = write_trss(tmp_path/'20260203'/'120401', n_rows=4, extra_tifs=3)
    plan = plan_frames(d, parse_dat(d/'scans120401.dat', 'tRSS'), 'tRSS')
    assert plan.n_frames == 4 and len(plan.unreferenced) == 3 and not plan.truncated

def test_missing_referenced_frames_become_nan(tmp_path, caplog):
    d = write_trss(tmp_path/'20260212'/'180247', n_rows=5, shape=(3, 4), missing=(1, 3))
    plan = plan_frames(d, parse_dat(d/'scans180247.dat', 'tRSS'), 'tRSS')
    with caplog.at_level(logging.WARNING):
        stack, missing = load_stack(plan)
    assert stack.dtype == numpy.float32 and stack.shape == (5, 3, 4)
    assert missing.tolist() == [False, True, False, True, False]
    assert numpy.isnan(stack[1]).all() and stack[2, 0, 0] == 2.0
    assert 'image00001.tif' in caplog.text and 'image00003.tif' in caplog.text

def test_xrss_truncation_uses_contiguous_run(tmp_path):
    d = write_xrss(tmp_path/'20260213'/'125733', n_rows=10, n_tifs=4)
    plan = plan_frames(d, parse_dat(d/'allSteps125733.dat', 'xRSS'), 'xRSS')
    assert plan.truncated and plan.n_frames == 4 and plan.n_rows == 10
    assert load_stack(plan)[0].shape[0] == 4

def test_shape_mismatch_names_the_file(tmp_path):                      # Review Focus 4
    d = write_srsm(tmp_path/'20260819'/'140224', n_rows=3, shape=(3, 4))
    tifffile.imwrite(d/'image00002.tif', numpy.zeros((5, 5), dtype=numpy.int32))
    plan = plan_frames(d, parse_dat(d/'rockingcurves140224.dat', 'sRSM'), 'sRSM')
    with pytest.raises(ValueError, match='image00002.tif'):
        load_stack(plan)
```

Add `tests/conftest.py` with `sys.path.insert(0, str(Path(__file__).parent))` so `fixtures` imports (the repo has no conftest yet; this is its only content).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/frames_test.py -v`
Expected: FAIL with `ModuleNotFoundError: pxs_import.frames`.

- [ ] **Step 3: Implement `FramePlan`, `plan_frames`, `load_stack` in `src/pxs_import/frames.py`**

`load_stack` reads the first existing file to fix `(H, W)`, preallocates `numpy.full((N,H,W), numpy.nan, dtype=numpy.float32)`, and fills with `tifffile.imread(f).astype(numpy.float32)`. Imports: `numpy`, `tifffile`, `logging`, `pathlib`, `dataclasses`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/frames_test.py -v`
Expected: PASS ×4.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/frames.py tests/frames_test.py tests/conftest.py
git commit -m "feat(udkm): authoritative frame plan and float32 stack with NaN gaps"
```

---

### Task 5: Build the per-scan h5like group

**Files:**
- Create: `src/pxs_import/reader.py`
- Test: `tests/build_test.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces, in `pxs_import.reader`:
  - `COLUMN_VARIABLES: dict[str, dict[str, str]]` — per scan type, verbatim column → variable name exactly as spec §5.8 (`'Theta / deg.' → 'theta'`, `'Crystal [ph/s]' → 'crystal_calib'`, `'Position Real' → 'delay'`, …). Profile column → `'curve'`.
  - `INTEGER_VARIABLES = {'loop', 'loop_delay', 'loop_theta', 'repeats', 'image_number'}` → written as `int64`.
  - `GROUP_OF: dict[str, str]` — `theta`, `ttheta` → `instrument/motors`; `images`, `curve` → `instrument/pilatus`; `photon_energy` → `instrument/source`; everything else → `measurement`.
  - `PRIMARY_PATH = {'sRSM': 'instrument/motors/theta', 'tRSS': 'measurement/delay', 'xRSS': 'measurement/delay_setpoint'}`
  - `build_scan_h5like(scan: ScanFolder, table: DatTable, params: dict, plan: FramePlan, stack: numpy.ndarray, missing: numpy.ndarray, *, photon_energy: float = 8048.0) -> silx.io.commonh5.File`
    - datasets per §6.3, all `index`-aligned arrays trimmed to `plan.n_frames`; `measurement/frame_missing` (bool); `instrument/source/photon_energy` filled constant.
    - `tRSS`: `ttheta = 2*theta + two_theta_correction(params)`.
    - if `plan.truncated`: `measurement_full/<var>_full` for every scalar column at full `n_rows`; attrs `udkm-truncated=True`, `udkm-rows`, `udkm-frames`.
    - root attrs: `k-scan-type`, `k-frame-guard='instrument/pilatus/images'`, `k-primary:0=PRIMARY_PATH[type]`, `k-header:notes` (`params.get('Comments','')`), `k-header:integration` (`exposure_seconds`, omitted if key absent), `k-header:photon_energy`, `k-header:date_time` (ISO, omitted if absent), `k-header:roi` (omitted if absent), `udkm-source`, `udkm-date`, `udkm-time`, `udkm-datetime` (ISO, omitted if absent).

- [ ] **Step 1: Write the failing tests**

```python
# tests/build_test.py
import numpy
from pxs_import.format import ScanFolder, parse_dat, parse_parameters
from pxs_import.frames import plan_frames, load_stack
from pxs_import.reader import build_scan_h5like
from fixtures import write_srsm, write_trss, write_xrss

def _build(folder, scan_type, **kw):
    sf = ScanFolder(folder, folder.parent.name, folder.name, scan_type)
    table = parse_dat(sf.dat_path, scan_type)
    plan = plan_frames(folder, table, scan_type)
    stack, missing = load_stack(plan)
    return build_scan_h5like(sf, table, parse_parameters(sf.params_path), plan, stack, missing, **kw)

def test_trss_layout_attrs_and_derived_ttheta(tmp_path):
    d = write_trss(tmp_path/'20260108'/'120821', n_rows=3, theta=19.48, correction=0.5,
                   params={'Comments': ['16nm Pt on MgO', '10.2mJ/cm²'], 'Exposure Time': '2.000000s'})
    h5 = _build(d, 'tRSS')
    assert h5['instrument/pilatus/images'].shape == (3, 4, 6)
    assert h5['instrument/pilatus/images'].dtype == numpy.float32
    assert h5['instrument/pilatus/curve'].shape == (3, 6)
    numpy.testing.assert_allclose(h5['instrument/motors/ttheta'][()], 2*19.48 + 0.5)
    for name in ('loop_delay', 'delay', 'loop_theta', 'crystal_v', 'crystal_calib',
                 'cntrate', 'repeats', 'image_number', 'frame_missing'):
        assert h5[f'measurement/{name}'].shape == (3,), name
    assert h5['measurement/loop_delay'].dtype.kind == 'i'
    a = h5.attrs
    assert a['k-scan-type'] == 'tRSS' and a['k-frame-guard'] == 'instrument/pilatus/images'
    assert a['k-primary:0'] == 'measurement/delay'
    assert a['k-header:notes'] == '16nm Pt on MgO 10.2mJ/cm²' and a['k-header:integration'] == 2.0
    assert a['k-header:photon_energy'] == 8048.0 and a['udkm-source'] == '20260108/120821'
    assert a['k-header:date_time'].startswith('20') and 'T' in a['k-header:date_time']
    assert 'measurement_full' not in h5

def test_srsm_reads_ttheta_and_has_empty_notes(tmp_path):
    h5 = _build(write_srsm(tmp_path/'20260108'/'115550', n_rows=2), 'sRSM', photon_energy=8041.0)
    assert h5['instrument/motors/ttheta'].shape == (2,) and h5.attrs['k-header:notes'] == ''
    assert h5.attrs['k-primary:0'] == 'instrument/motors/theta'
    assert float(h5['instrument/source/photon_energy'][0]) == 8041.0

def test_xrss_truncated_scan_keeps_full_table_with_suffixed_names(tmp_path):
    h5 = _build(write_xrss(tmp_path/'20260213'/'125733', n_rows=10, n_tifs=4), 'xRSS')
    assert h5['instrument/pilatus/images'].shape[0] == 4
    for name in ('delay_setpoint', 'delay', 'loop', 'counts', 'crystal_v', 'norm', 'crystal_calib'):
        assert h5[f'measurement/{name}'].shape == (4,)
        assert h5[f'measurement_full/{name}_full'].shape == (10,)
    assert h5.attrs['udkm-truncated'] is True and h5.attrs['udkm-rows'] == 10 and h5.attrs['udkm-frames'] == 4
    assert h5.attrs['k-primary:0'] == 'measurement/delay_setpoint'

def test_missing_parameters_file_still_builds(tmp_path):                   # Review Focus 3
    d = write_srsm(tmp_path/'20260108'/'115550', n_rows=2)
    (d/'parameters115550.txt').unlink()
    h5 = _build(d, 'sRSM')
    assert h5.attrs['k-header:notes'] == '' and 'k-header:integration' not in h5.attrs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/build_test.py -v`
Expected: FAIL with `ModuleNotFoundError: pxs_import.reader`.

- [ ] **Step 3: Implement the constants and `build_scan_h5like` in `src/pxs_import/reader.py`**

Use `silx.io.commonh5.File()` as root, `create_group(path)` and `group.create_dataset(name, data=array)`. Set attrs through `node.attrs[key] = value`. Import `silx` inside the function body or at module top of this module only (the module is opt-in). Enumerate the leaf names written and `assert` they are unique before returning (guards §6.3).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/build_test.py -v`
Expected: PASS ×4.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py tests/build_test.py
git commit -m "feat(udkm): assemble nx5d-shaped h5like scan groups"
```

---

### Task 6: `scans_from_pxs` reader and mapping table

**Files:**
- Modify: `src/pxs_import/reader.py`
- Test: `tests/reader_test.py`

**Interfaces:**
- Produces, in `pxs_import.reader`:
  - `scans_from_pxs(source, *, photon_energy: float = 8048.0, scan_format: str = 'r{iscan:04d}', mapping_file: str | None = None)` — generator. `source` may be `str` or `Path` (`pm` passes a string). Yields `({'scan': 'r0001', 'iscan': 1, 'udkm_source': '20260108/100331', 'scan_type': 'xRSS'}, h5like)`. Before the first yield it logs the full mapping at INFO and, if `mapping_file` is given, writes it via `write_mapping_table`. Holds no reference to a yielded `h5like` after yielding.
  - `write_mapping_table(mapping: list[tuple[str, ScanFolder]], path: Path) -> None` — CSV with header `scan,source,type`, rows like `r0001,20260108/100331,xRSS`.
  - `format_mapping_table(mapping) -> str` — lines `r0001 <- 20260108/100331 (xRSS)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/reader_test.py
import csv
from pxs_import.reader import scans_from_pxs, format_mapping_table
from fixtures import write_srsm, write_trss, write_xrss

def _proposal(root):
    write_xrss(root/'20260202'/'135936', n_rows=3)
    write_trss(root/'20260203'/'120401', n_rows=2)
    write_srsm(root/'20260202'/'153026', n_rows=2)
    (root/'20260202'/'153026.zip').write_bytes(b'PK')
    return root

def test_reader_numbers_chronologically_across_dates(tmp_path):
    out = list(scans_from_pxs(str(_proposal(tmp_path)), mapping_file=str(tmp_path/'map.csv')))
    keys = [k for k, _ in out]
    assert [k['scan'] for k in keys] == ['r0001', 'r0002', 'r0003']
    assert [k['udkm_source'] for k in keys] == ['20260202/135936', '20260202/153026', '20260203/120401']
    assert [k['scan_type'] for k in keys] == ['xRSS', 'sRSM', 'tRSS']
    assert out[2][1].attrs['k-scan-type'] == 'tRSS' and out[2][1]['instrument/pilatus/images'].shape[0] == 2
    rows = list(csv.DictReader(open(tmp_path/'map.csv')))
    assert rows[1] == {'scan': 'r0002', 'source': '20260202/153026', 'type': 'sRSM'}

def test_photon_energy_override_reaches_scan(tmp_path):
    _proposal(tmp_path)
    (_, h5), = scans_from_pxs(tmp_path/'20260203', photon_energy=8041.0)   # one scan in that date
    assert h5.attrs['k-header:photon_energy'] == 8041.0

def test_format_mapping_table_line():
    from pathlib import Path
    from pxs_import.format import ScanFolder
    line = format_mapping_table([('r0001', ScanFolder(Path('x'), '20260108', '100331', 'xRSS'))])
    assert line.strip() == 'r0001 <- 20260108/100331 (xRSS)'
```

(`scans_from_pxs` applied to a single date folder works because `discover_scans` of a `^\d{8}$` directory's parent is the normal case; make `discover_scans` also accept a date directory itself by checking whether `proposal_root.name` matches `^\d{8}$`. Add that one branch in Task 6 and keep Task 1's tests green.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/reader_test.py -v`
Expected: FAIL with `ImportError: cannot import name 'scans_from_pxs'`.

- [ ] **Step 3: Implement the generator and the two table helpers**

Per scan: `parse_dat` → `plan_frames` → `load_stack` → `parse_parameters` → `build_scan_h5like`, then `yield`, then `del` the local. Use `logger.info` for the table (one call with the whole text) and `logger.warning` for `plan.unreferenced` and `plan.truncated` (count + folder).

- [ ] **Step 4: Run all unit tests**

Run: `.venv/Scripts/python -m pytest tests -v -k udkm`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py src/pxs_import/format.py tests/reader_test.py
git commit -m "feat(udkm): scans_from_pxs reader for pm scan import, with mapping table"
```

---

### Task 7: End-to-end through `pm scan import` and nx5d `Proposal`

**Files:**
- Test: `tests/nx5d_test.py`

**Interfaces:**
- Consumes: `scans_from_pxs` via `-R pxs_import.reader:scans_from_pxs`; nx5d `Proposal` from `nx5d`.
- Produces: proof that both layouts of spec §6.1 load with unmodified nx5d; a helper `run_pm_import(source: Path, url: str, *, writer: str | None = None, extra_reader_kw: dict = {}) -> subprocess.CompletedProcess` reused by Task 9's tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/nx5d_test.py
import subprocess, sys, pytest, numpy
nx5d = pytest.importorskip('nx5d')
pytestmark = pytest.mark.nx5d
from fixtures import write_srsm, write_trss, write_xrss

PM = [sys.executable, '-m', 'nx5d.pmtool.pm']

def run_pm_import(source, url, *, writer=None, extra_reader_kw={}):
    cmd = PM + ['scan', 'import', str(source), '-R', 'pxs_import.reader:scans_from_pxs',
                *[f'{k}={v}' for k, v in extra_reader_kw.items()], '-u', url]
    if writer: cmd += ['-W', writer]
    return subprocess.run(cmd, capture_output=True, text=True)

def _proposal(root):
    write_trss(root/'20260108'/'120821', n_rows=3, correction=0.0, params={'Comments': ['16nm Pt on MgO']})
    write_srsm(root/'20260108'/'115550', n_rows=2)
    write_xrss(root/'20260108'/'100331', n_rows=5, n_tifs=3)
    return root

def _check(prop):
    assert sorted(prop.ls()) == ['r0001', 'r0002', 'r0003']     # ls() returns the key list
    raw = prop.r0003.raw                      # the tRSS scan
    for v in ('loop_delay', 'delay', 'loop_theta', 'theta', 'ttheta', 'crystal_v', 'crystal_calib',
              'cntrate', 'repeats', 'image_number', 'curve', 'images', 'frame_missing'):
        assert v in raw and raw[v].sizes['index'] == 3, v
    numpy.testing.assert_allclose(raw.ttheta.values, 2 * raw.theta.values)
    assert prop.r0003.summary['type'] == 'tRSS' and prop.r0003.summary['num_frames'] == 3
    assert prop.r0003.summary['notes'] == '16nm Pt on MgO' and prop.r0003.summary['integration'] == 2.0
    assert 'Bad header spec' not in str(prop.r0003.summary.get('description', ''))
    x = prop.r0001.raw                        # the truncated xRSS scan
    assert x.counts.sizes['index'] == 3 and x.counts_full.shape == (5,)
    assert prop.r0002.summary['notes'] == ''

def test_per_scan_files(tmp_path):
    src = _proposal(tmp_path/'P'); out = tmp_path/'P'/'scans'
    r = run_pm_import(src, f'file:///{out.as_posix()}/{{scan}}.h5')
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in out.glob('*.h5')) == ['r0001.h5', 'r0002.h5', 'r0003.h5']
    _check(nx5d.Proposal(url=f'file:///{out.as_posix()}/{{scan}}.h5'))

def test_single_file_requires_writer_hint_and_loads(tmp_path):
    src = _proposal(tmp_path/'P'); h5 = tmp_path/'P'/'P.h5'
    assert run_pm_import(src, f'file:///{h5.as_posix()}#{{scan}}').returncode != 0
    r = run_pm_import(src, f'file:///{h5.as_posix()}#{{scan}}', writer='hdf5')
    assert r.returncode == 0, r.stderr
    _check(nx5d.Proposal(url=f'file:///{h5.as_posix()}#{{scan}}'))
```

- [ ] **Step 2: Run tests**

Run: `.venv/Scripts/python -m pytest tests/nx5d_test.py -v`
Expected: either PASS (nothing to implement) or a failure that points at a mismatch between the reader output and what nx5d expects. Fix the reader in `src/pxs_import/reader.py` only; do not touch nx5d.

- [ ] **Step 3: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: all PASS; the two nx5d tests run (nx5d is installed in `.venv`).

- [ ] **Step 4: Commit**

```bash
git add tests/nx5d_test.py src/pxs_import/reader.py
git commit -m "test(udkm): end-to-end import through pm scan import for both layouts"
```

---

### Task 8: Spice migration

**Files:**
- Create: `src/pxs_import/spice.py`
- Test: `tests/spice_test.py`

**Interfaces:**
- Consumes: `number_scans` output from Task 1 (as `mapping: dict[str, str]`, source → key).
- Produces, in `pxs_import.spice`:
  - `load_records(spice_dir: Path) -> list[tuple[Path, dict]]` — regular files only, JSON-parsable only; others skipped with a DEBUG log. Never descends into subdirectories.
  - `target_key_for(anchor: str, mapping: dict[str, str]) -> str` — exact match, else the key of the first source sorting `>= anchor`; raises `ValueError(f'anchor {anchor!r} is after every scan')` if none. Anchors already matching `^r\d{4}$` and `''` are returned unchanged.
  - `@dataclass class SpiceChange: path: Path; handle: str; old_anchor: str; new_anchor: str`
  - `migrate_spice(spice_dir: Path, mapping: dict[str, str], *, dry_run: bool = False, backup: bool = True) -> list[SpiceChange]` — rewrites only the `anchor` field in place (`json.dump(..., indent=2)`), copies the directory to `spice_dir.with_name(spice_dir.name + '.pre-migration')` once before the first write (skips if that exists), returns the changes. Zero changes → no backup, no writes.
  - `fix_recipes(spice_dir: Path, *, dry_run: bool = False) -> bool` — removes the `xRSS` key from the `data` of every record with `handle == 'recipes'`; returns whether anything changed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/spice_test.py
import json, uuid, pytest
from pxs_import.spice import load_records, target_key_for, migrate_spice, fix_recipes

def _rec(d, handle, anchor, data=None, rev=1):
    p = d / str(uuid.uuid4())
    p.write_text(json.dumps({'handle': handle, 'anchor': anchor, 'revision': rev, 'data': data or {}}))
    return p

@pytest.fixture
def spice(tmp_path):
    d = tmp_path/'spice'; d.mkdir(); (d/'.ipynb_checkpoints').mkdir()
    (d/'notes.txt').write_text('not json')
    _rec(d, 'exp_info', '20260108/143930', rev=5); _rec(d, 'exp_info', '20260108/143930', rev=4)
    _rec(d, 'qargs', '20260108/120821'); _rec(d, 'offsets', '')
    _rec(d, 'recipes', '', {'sRSM': 'pymod:///xraydc.cooking#sRSM', 'xRSS': 'pymod:///xraydc.cooking#TRSS_2d_fix'})
    return d

MAPPING = {'20260108/100331': 'r0001', '20260108/120821': 'r0002', '20260108/143930': 'r0003'}

def test_load_records_skips_dirs_and_non_json(spice):
    assert len(load_records(spice)) == 5

def test_target_key_for(spice):
    assert target_key_for('20260108/143930', MAPPING) == 'r0003'
    assert target_key_for('20260108/130000', MAPPING) == 'r0003'
    assert target_key_for('', MAPPING) == '' and target_key_for('r0007', MAPPING) == 'r0007'
    with pytest.raises(ValueError, match='20260108/150000'):                 # Review Focus 5
        target_key_for('20260108/150000', MAPPING)

def test_migrate_rewrites_anchors_once_with_backup(spice):
    changes = migrate_spice(spice, MAPPING)
    assert sorted((c.handle, c.new_anchor) for c in changes) == [('exp_info', 'r0003'), ('exp_info', 'r0003'), ('qargs', 'r0002')]
    assert (spice.parent/'spice.pre-migration').is_dir()
    assert {r['anchor'] for _, r in load_records(spice)} == {'', 'r0002', 'r0003'}
    assert migrate_spice(spice, MAPPING) == []          # idempotent

def test_migrate_dry_run_writes_nothing(spice):
    assert len(migrate_spice(spice, MAPPING, dry_run=True)) == 3
    assert not (spice.parent/'spice.pre-migration').exists()
    assert '20260108/143930' in {r['anchor'] for _, r in load_records(spice)}

def test_fix_recipes_removes_xrss(spice):
    assert fix_recipes(spice) is True
    recipes = [r for _, r in load_records(spice) if r['handle'] == 'recipes'][0]['data']
    assert 'xRSS' not in recipes and 'sRSM' in recipes
    assert fix_recipes(spice) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/spice_test.py -v`
Expected: FAIL with `ModuleNotFoundError: pxs_import.spice`.

- [ ] **Step 3: Implement the module**

stdlib only (`json`, `shutil`, `re`, `dataclasses`, `logging`, `pathlib`). Preserve every other field of each record verbatim.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/spice_test.py -v`
Expected: PASS ×5.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/spice.py tests/spice_test.py
git commit -m "feat(udkm): migrate spice anchors to run numbers; drop xRSS recipe"
```

---

### Task 9: Preparation orchestrator and CLI

**Files:**
- Create: `src/pxs_import/prepare.py`
- Test: `tests/prepare_test.py`
- Modify: `pyproject.toml` (`[project.scripts]`)

**Interfaces:**
- Consumes: `discover_scans`, `number_scans`, `write_mapping_table`, `format_mapping_table`, `migrate_spice`, `fix_recipes`.
- Produces, in `pxs_import.prepare`:
  - `@dataclass class PrepareResult: mapping: list[tuple[str, ScanFolder]]; target_url: str; command: list[str]; spice_changes: list[SpiceChange]`
  - `target_url_for(proposal_root: Path, *, single_file: bool, output_root: Path | None = None) -> str` — `file:///<out>/scans/{scan}.h5` or `file:///<out>/<proposal_root.name>.h5#{scan}` where `out = output_root or proposal_root` (`as_posix()`, three slashes).
  - `existing_output(proposal_root: Path, *, single_file: bool, output_root: Path | None = None) -> Path | None` — the `scans/` dir if it contains any `*.h5`, or the `.h5` file if it exists; else `None`.
  - `prepare_proposal(proposal_root: Path, *, single_file: bool = False, force: bool = False, dry_run: bool = False, photon_energy: float = 8048.0, output_root: Path | None = None, spice_dir: Path | None = None, pm: list[str] = ('pm',), runner=subprocess.run) -> PrepareResult`. `output_root` defaults to `proposal_root`, `spice_dir` to `proposal_root/'spice'`. Order: discover+number → if `existing_output` and not `force`: raise `FileExistsError(path)`; if `force`: `shutil.rmtree`/`unlink` it → write `<out>/scan-mapping.csv` and print `format_mapping_table` → build `command = [*pm, 'scan', 'import', str(proposal_root), '-R', 'pxs_import.reader:scans_from_pxs', f'photon_energy={photon_energy}', '-u', url, '-P'] (+ ['-W', 'hdf5'] if single_file) (+ ['-d'] if dry_run)` → `runner(command, check=True)` → if `spice_dir.is_dir()`: `fix_recipes(spice_dir, dry_run=dry_run)` then `migrate_spice(spice_dir, {sf.source: key}, dry_run=dry_run)` → print the matching `Proposal(url=..., spice=...)` line.
  - `main(argv: list[str] | None = None) -> int` — argparse: positional `proposal_root`; `--single-file`, `--force`, `--dry-run`, `--photon-energy FLOAT` (default `8048.0`), `--output-root PATH`, `--spice-dir PATH`. Registered as console script `pxs-prepare = "pxs_import.prepare:main"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/prepare_test.py
import pytest
from pxs_import.prepare import target_url_for, existing_output, prepare_proposal, main
from fixtures import write_srsm, write_trss

def _proposal(root):
    write_srsm(root/'20260108'/'115550', n_rows=2); write_trss(root/'20260203'/'120401', n_rows=2)
    return root

def test_target_urls(tmp_path):
    root = tmp_path/'202601_PtAu'
    assert target_url_for(root, single_file=False).endswith('/202601_PtAu/scans/{scan}.h5')
    assert target_url_for(root, single_file=True).endswith('/202601_PtAu/202601_PtAu.h5#{scan}')
    assert target_url_for(root, single_file=False).startswith('file:///')
    assert target_url_for(root, single_file=True, output_root=tmp_path/'out').endswith('/out/202601_PtAu.h5#{scan}')

def test_prepare_builds_command_and_mapping(tmp_path):
    root = _proposal(tmp_path/'P'); calls = []
    res = prepare_proposal(root, runner=lambda cmd, **kw: calls.append(cmd), pm=['pm'])
    assert calls[0][:3] == ['pm', 'scan', 'import'] and '-W' not in calls[0]
    assert 'photon_energy=8048.0' in calls[0] and calls[0][calls[0].index('-u')+1] == res.target_url
    assert [k for k, _ in res.mapping] == ['r0001', 'r0002'] and (root/'scan-mapping.csv').exists()

def test_prepare_single_file_adds_writer_hint(tmp_path):
    root = _proposal(tmp_path/'P'); calls = []
    prepare_proposal(root, single_file=True, runner=lambda cmd, **kw: calls.append(cmd))
    assert calls[0][calls[0].index('-W')+1] == 'hdf5' and '#{scan}' in calls[0][calls[0].index('-u')+1]

def test_prepare_refuses_existing_output_unless_forced(tmp_path):
    root = _proposal(tmp_path/'P'); (root/'scans').mkdir(); (root/'scans'/'r0001.h5').write_bytes(b'')
    with pytest.raises(FileExistsError):
        prepare_proposal(root, runner=lambda cmd, **kw: None)
    prepare_proposal(root, force=True, runner=lambda cmd, **kw: None)
    assert existing_output(root, single_file=False) is None

def test_main_dry_run_exit_code(tmp_path, monkeypatch):
    root = _proposal(tmp_path/'P')
    monkeypatch.setattr('pxs_import.prepare.subprocess.run', lambda cmd, **kw: None)
    assert main([str(root), '--dry-run', '--photon-energy', '8041']) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py -v`
Expected: FAIL with `ModuleNotFoundError: pxs_import.prepare`.

- [ ] **Step 3: Implement the module and register the script**

`runner` defaults to `subprocess.run`; `main` passes `pm=[sys.executable, '-m', 'nx5d.pmtool.pm']` when `shutil.which('pm')` is `None`. Add `[project.scripts] pxs-prepare = "pxs_import.prepare:main"` to `pyproject.toml` and reinstall (`.venv/Scripts/python -m pip install -e ".[udkm,test]"`).

- [ ] **Step 4: Run the tests and a real dry run**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py tests/nx5d_test.py -v`
Expected: PASS. Then, with a synthetic proposal created by the fixtures into a temp directory, `.venv/Scripts/pxs-prepare <dir> --dry-run` exits 0 and prints the mapping table and `Dry run -- no changes will be made.` from `pm`.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/prepare.py tests/prepare_test.py pyproject.toml
git commit -m "feat(udkm): pxs-prepare orchestrator with --single-file and overwrite guard"
```

---

### Task 10: Per-proposal script and documentation

**Files:**
- Create: `notebooks/202601_PtAu/data-prepare.py` (force-added: `notebooks/*` is gitignored)
- Modify: `doc/pages/api.md`, `README.md`, `CLAUDE.md` (architecture table and data-flow section)

**Interfaces:**
- Consumes: `pxs_import.prepare.prepare_proposal`.

- [ ] **Step 1: Write the script**

`notebooks/202601_PtAu/data-prepare.py` — ≤ 25 lines, in the style of `notebooks/XFEL23/data-prepare.py`: a module docstring stating proposal, beamtime date and layout choice; `PROPOSAL_ROOT = Path(__file__).resolve().parent`; `SINGLE_FILE = False`; `PHOTON_ENERGY = 8048.0`; an `if __name__ == '__main__':` block that parses `--force`/`--single-file`/`--dry-run` and calls `prepare_proposal(PROPOSAL_ROOT, single_file=..., force=..., dry_run=..., photon_energy=PHOTON_ENERGY)`.

- [ ] **Step 2: Run it as a dry run against the real proposal**

Run: `.venv/Scripts/python notebooks/202601_PtAu/data-prepare.py --dry-run`
Expected: exit 0; mapping table of 15 lines starting `r0001 <- 20260108/100331 (xRSS)`; `pm` reports the dry run; no `scans/` created, spice untouched.

- [ ] **Step 3: Document**

- `doc/pages/api.md`: add a section "Importing UDKM PXS data" listing `pxs_import.reader.scans_from_pxs`, `pxs_import.spice.migrate_spice`, `pxs_import.prepare.prepare_proposal` via mkdocstrings, and the two `pm scan import` invocations from spec §6.1.1.
- `README.md`: one short subsection under installation: `pip install xraydc[udkm]`, the `pxs-prepare` command, and that nx5d must be installed for `pm`.
- `CLAUDE.md`: add the `io/` modules to the architecture table (LOC approximate) and a sentence in "Relationship to nx5d" that the reader is resolved by nx5d's `pm` through `-R pxs_import.reader:scans_from_pxs` and must not import nx5d.

- [ ] **Step 4: Build docs and run the suite**

Run: `.venv/Scripts/python -m pytest -q` then `.venv/Scripts/python -m mkdocs build -f doc/mkdocs.yml --strict` (skip the docs build with a note if `mkdocs` is not installed).
Expected: tests PASS; docs build without warnings.

- [ ] **Step 5: Commit**

```bash
git add -f notebooks/202601_PtAu/data-prepare.py
git add doc/pages/api.md README.md CLAUDE.md
git commit -m "docs(udkm): per-proposal data-prepare script, API docs, README and CLAUDE.md"
```

---

### Task 11: Corpus regression tests (spec §11 criteria 8–13d)

**Files:**
- Test: `tests/corpus_test.py`

**Interfaces:**
- Consumes: everything above; the real folders under `notebooks/202601_PtAu`, `notebooks/202602_FeAu`, `notebooks/202608_DyCo5Pt`.

- [ ] **Step 1: Write the tests (marked `corpus`, skipped when the folders are absent)**

```python
# tests/corpus_test.py
import pytest, numpy, shutil
from pathlib import Path
from pxs_import.format import discover_scans, number_scans, parse_dat
from pxs_import.frames import plan_frames, load_stack
from pxs_import.spice import load_records, migrate_spice
pytestmark = pytest.mark.corpus
NB = Path(__file__).resolve().parents[1] / 'notebooks'
needs = pytest.mark.skipif(not (NB/'202602_FeAu'/'20260203').exists(), reason='corpus not present')

@needs
@pytest.mark.parametrize('proposal,n,types', [('202601_PtAu', 15, (5, 6, 4)),
                                              ('202602_FeAu', 38, (18, 11, 9)),
                                              ('202608_DyCo5Pt', 18, (16, 2, 0))])
def test_all_scans_classify(proposal, n, types):
    scans = discover_scans(NB/proposal)
    assert len(scans) == n
    assert tuple(sum(s.scan_type == t for s in scans) for t in ('sRSM', 'tRSS', 'xRSS')) == types

@needs
def test_120401_excludes_unreferenced_frames():
    d = NB/'202602_FeAu'/'20260203'/'120401'
    plan = plan_frames(d, parse_dat(d/'scans120401.dat', 'tRSS'), 'tRSS')
    assert plan.n_frames == 1364 and len(plan.unreferenced) == 751

@needs
def test_180247_nan_fills_seven_gaps():
    d = NB/'202602_FeAu'/'20260212'/'180247'
    plan = plan_frames(d, parse_dat(d/'scans180247.dat', 'tRSS'), 'tRSS')
    stack, missing = load_stack(plan)
    assert stack.shape[0] == 172 and missing.sum() == 7 and stack.dtype == numpy.float32

@needs
def test_125733_truncates_to_489():
    d = NB/'202602_FeAu'/'20260213'/'125733'
    plan = plan_frames(d, parse_dat(d/'allSteps125733.dat', 'xRSS'), 'xRSS')
    assert plan.truncated and plan.n_frames == 489 and plan.n_rows == 1729

@needs
def test_dyco5pt_spice_migration_is_noop(tmp_path):
    spice = tmp_path/'spice'; shutil.copytree(NB/'202608_DyCo5Pt'/'spice', spice)
    mapping = {sf.source: k for k, sf in number_scans(discover_scans(NB/'202608_DyCo5Pt'))}
    assert migrate_spice(spice, mapping) == [] and not (tmp_path/'spice.pre-migration').exists()

@needs
def test_ptau_spice_migration_rewrites_eleven(tmp_path):
    spice = tmp_path/'spice'; shutil.copytree(NB/'202601_PtAu'/'spice', spice)
    mapping = {sf.source: k for k, sf in number_scans(discover_scans(NB/'202601_PtAu'))}
    changes = migrate_spice(spice, mapping)
    assert len(changes) == 11
    assert {c.new_anchor for c in changes if c.old_anchor == '20260108/143930'} == {mapping['20260108/143930']}

@needs
def test_ptau_end_to_end_cooks_srsm_and_refuses_xrss(tmp_path):
    nx5d = pytest.importorskip('nx5d')
    from pxs_import.prepare import prepare_proposal
    spice = tmp_path/'spice'; shutil.copytree(NB/'202601_PtAu'/'spice', spice)   # never touch the real spice
    res = prepare_proposal(NB/'202601_PtAu', output_root=tmp_path, spice_dir=spice)  # reads TIFFs in place
    prop = nx5d.Proposal(url=res.target_url, spice=f'file:///{spice.as_posix()}')
    srsm = next(k for k, sf in res.mapping if sf.scan_type == 'sRSM')
    cooked = getattr(prop, srsm).cooked
    assert {'qx', 'qy', 'qz'} <= set(cooked.dims) | set(cooked.coords)
    xrss = next(k for k, sf in res.mapping if sf.scan_type == 'xRSS')
    with pytest.raises(RuntimeError):
        getattr(prop, xrss).cooked
    notes = getattr(prop, next(k for k, sf in res.mapping if sf.source == '20260108/120821')).summary['notes']
    assert notes == '16nm Pt on MgO 157mW 930x880µm² 10.2mJ/cm²'
```

- [ ] **Step 2: Run the corpus tests explicitly**

Run: `.venv/Scripts/python -m pytest tests/corpus_test.py -m corpus -v`
Expected: PASS. The end-to-end test reads the 2.2 GB PtAu corpus in place and writes ~15 scan files to `tmp_path`; allow several minutes. If `cooked` fails inside `xraydc.cooking` rather than in the importer, record the traceback in the commit message and mark that single test `xfail(strict=False)` with the reason — the spec's §3 puts the recipe layer out of scope.

- [ ] **Step 3: Confirm the default run still skips them**

Run: `.venv/Scripts/python -m pytest -q`
Expected: corpus tests reported as deselected; everything else PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/corpus_test.py
git commit -m "test(udkm): corpus regression tests for the three surveyed proposals"
```

---

## Self-review notes

- **Spec coverage:** §5 discovery/classification → T1; §5.4 → T2; §5.1–5.3, 5.8 → T3, T5; §5.5, §5.7, dtype → T4, T5; §6.2–6.4 attrs and `k-primary` paths → T5, verified by nx5d in T7; §6.5 reader contract → T6, T7; §6.1.1 single file and `-W hdf5` → T7, T9; §6.7 extras → T0; §6.8 script and `--single-file` → T9, T10; §7 numbering, mapping table, overwrite refusal → T1, T6, T9; §8 `xRSS` recipe removal → T8; §8.1 `_full` names → T5, T7; §9 migration incl. UUID files and checkpoints → T8; §11 criteria 8–13d → T7, T11. §6.6 S3 is deferred by the spec itself and has no task.
- **Names used consistently across tasks:** `ScanFolder(path, date, time, scan_type)`, `DatTable`, `FramePlan`, `plan_frames`, `load_stack`, `build_scan_h5like`, `scans_from_pxs`, `number_scans`, `write_mapping_table`, `format_mapping_table`, `migrate_spice`, `fix_recipes`, `target_key_for`, `prepare_proposal`, `target_url_for`, `existing_output`, `run_pm_import`.
- **Review Focus** items 1–5 are pinned in T1, T3, T2+T5, T4, T8 respectively.
