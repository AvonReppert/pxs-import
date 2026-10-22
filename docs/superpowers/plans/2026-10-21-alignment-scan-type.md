# `alignment` Scan Type Implementation Plan

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the UDKM importer a fourth PXS scan type, `alignment`, so that
goniometer and Zaber alignment scans import with correct columns, readable
frames, and the moved motor visible in an nx5d listing.

**Architecture:** Four small, declarative additions to the existing per-type
tables in `io/format.py`, `io/frames.py` and `io/reader.py` — a
signature filename, a leading-unnamed-column declaration, a frame-planning
branch, a column map and primary axis, and two `k-header:` attributes. No new
module, no new entry point, nothing outside `src/pxs_import/`.

**Tech Stack:** Python 3.11+, numpy, silx `commonh5`, tifffile, pytest.

**Spec:** [`docs/superpowers/specs/2026-10-21-alignment-scan-type-design.md`](../specs/2026-10-21-alignment-scan-type-design.md)

## Global Constraints

- Nothing outside `src/pxs_import/` may be modified. `signal.py` and
  `cooking.py` are off limits (spec §4).
- No module in `io/` may import `nx5d`, and `silx`/`tifffile` stay out of
  `sys.modules` on a bare `import xraydc` — `tests/packaging_test.py`
  enforces both.
- The scan-type string is exactly `alignment`.
- The signature filename is exactly `scan{sfx}.dat`, where `{sfx}` is the
  folder's `HHMMSS` name. It must never match `scans{sfx}.dat`.
- `position` is stored raw, in whatever unit the acquisition software used.
  No steps→mm conversion anywhere (spec §5.3, §6.2).
- No truncation attribute fires for this type on real data (all 19 scans have
  `n_rows == n_tifs`); the branch is not suppressed in code (spec §5.4).
- `.gitattributes` forces LF. The `.dat` fixtures deliberately write CRLF
  *content*; write them with `newline=''` as the existing fixtures do.
- Run `pytest -m corpus` before calling the work done. It is deselected by
  default and will not announce its own breakage.

## Review Focus

Five conditions the spec implies but does not name a test for. Each has a test
assigned to the task that owns the code.

1. **A short final row in `scan<sfx>.dat`.** The acquisition is killed
   mid-write, leaving a row with fewer than five fields. `parse_dat`'s
   max-field-count logic must still drop exactly that row, with the
   synthesised leading column in play. → Task 2.
2. **Unreferenced TIFFs in the folder.** An aborted run leaves `image*.tif`
   files past the last `.dat` row. They must be reported and never opened,
   not silently included. → Task 3.
3. **`parameters<sfx>.txt` present but carrying no `Stage` key.** Distinct
   from the whole file being missing; the scan must still build, omitting
   `k-header:stage` with a warning. → Task 5.
4. **Non-ASCII in `parameters<sfx>.txt`.** Real files are Windows-1252 and
   alignment scans record degrees; a `°` in a value must survive rather than
   raise. → Task 5.
5. **A header-only `.dat` with zero data rows.** An acquisition stopped before
   the first exposure. It must fail with a message naming the scan, not with
   an opaque `StopIteration` out of `DatTable.n_rows`. → Task 2.

---

### Task 1: Classify `alignment` folders

**Files:**
- Modify: `src/pxs_import/format.py` (the `SIGNATURES` dict)
- Modify: `tests/fixtures.py` (new `write_alignment`)
- Test: `tests/format_test.py`

**Interfaces:**
- Consumes: `_write_params`, `_write_tifs` from `tests/fixtures.py`.
- Produces:
  - `SIGNATURES['alignment'] == 'scan{sfx}.dat'`
  - `write_alignment(folder: Path, n_rows: int, shape=(4, 6), n_tifs: int | None = None, stage: str | None = 'Omega', unit: str | None = 'deg', params: dict | None = None) -> Path`
    — writes `scan<sfx>.dat` (header `% Position\tPilatus\tCrystal\tNorm`,
    rows of five tab-separated floats: loop, position, pilatus, crystal,
    norm), `parameters<sfx>.txt` carrying `Stage`/`Unit` when not `None`, and
    `image{i:05d}.tif` for `i < (n_rows if n_tifs is None else n_tifs)`.
    Models `write_xrss`, including CRLF line endings via `newline=''`.
    First row's `Position` is always `-1.000000`, stepping by `+0.1`.

- [ ] **Step 1: Write `write_alignment` in `tests/fixtures.py`**

Follow `write_xrss` (same file) exactly in structure. The one difference that
matters: the header declares four names while each data row has five fields,
the first being the loop index — write `1.0` for every row's loop value.
`stage`/`unit`, when not `None`, go into the `params` dict passed to
`_write_params` so they are emitted as `Stage: <value>` / `Unit: <value>`.

- [ ] **Step 2: Write the failing classification tests**

```python
def test_alignment_folder_classifies(tmp_path):
    folder = write_alignment(tmp_path / '151027', n_rows=4)
    assert classify_scan(folder) == 'alignment'


def test_scan_and_scans_do_not_collide(tmp_path):
    '''`scan<sfx>.dat` is alignment, `scans<sfx>.dat` is tRSS. One character.'''
    alignment = write_alignment(tmp_path / '151027', n_rows=4)
    trss = write_trss(tmp_path / '151028', n_rows=4)
    assert classify_scan(alignment) == 'alignment'
    assert classify_scan(trss) == 'tRSS'
    assert not (alignment / 'scans151027.dat').exists()
    assert not (trss / 'scan151028.dat').exists()


def test_discover_scans_finds_alignment(tmp_path):
    write_alignment(tmp_path / '20250408' / '151027', n_rows=4)
    scans = discover_scans(tmp_path)
    assert [s.scan_type for s in scans] == ['alignment']
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/format_test.py -k "alignment or collide" -v`
Expected: FAIL — `classify_scan` returns `None`.

- [ ] **Step 4: Add the `SIGNATURES` entry**

Add `'alignment': 'scan{sfx}.dat'` to `SIGNATURES` in
`src/pxs_import/format.py`, **last** in the dict. Extend the dict's
docstring comment to say that `scan` and `scans` are distinct exact names and
that `averages<sfx>.dat` is shared with `xRSS` and so is not a signature.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/format_test.py -v`
Expected: PASS, and no pre-existing test in that file regresses.

- [ ] **Step 6: Commit**

```bash
git add src/pxs_import/format.py tests/fixtures.py tests/format_test.py
git commit -m "feat(udkm): classify alignment scan folders"
```

---

### Task 2: Parse the unnamed leading column

**Files:**
- Modify: `src/pxs_import/format.py` (`parse_dat`, new module constant)
- Test: `tests/format_test.py`

**Interfaces:**
- Consumes: `write_alignment` (Task 1).
- Produces: `_LEADING_LOOP_TYPES: tuple[str, ...] = ('alignment',)` — scan
  types whose `.dat` header omits a name for the leading loop-index column.
  `parse_dat(path, 'alignment')` returns a `DatTable` whose `columns` has the
  five keys `Loop`, `Position`, `Pilatus`, `Crystal`, `Norm`.

This is the task the spec calls its first anti-goal: get it wrong and every
column silently holds its neighbour's values (spec §5.2).

- [ ] **Step 1: Write the failing tests**

```python
def test_alignment_columns_are_not_shifted(tmp_path):
    '''The header names four columns; the rows have five. Values, not shapes.'''
    folder = write_alignment(tmp_path / '151027', n_rows=3)
    table = parse_dat(folder / 'scan151027.dat', 'alignment')
    assert list(table.columns) == ['Loop', 'Position', 'Pilatus', 'Crystal', 'Norm']
    assert table.columns['Loop'].tolist() == [1.0, 1.0, 1.0]
    assert table.columns['Position'].tolist() == [-1.0, -0.9, -0.8]
    assert table.profile is None
    assert table.image_names is None


def test_alignment_short_final_row_is_dropped(tmp_path):
    '''Review Focus 1: a killed acquisition leaves a half-written last row.'''
    folder = write_alignment(tmp_path / '151027', n_rows=3)
    dat = folder / 'scan151027.dat'
    dat.write_text(dat.read_text() + '2.000000\t0.500000\r\n', newline='')
    table = parse_dat(dat, 'alignment')
    assert table.n_rows == 3


def test_alignment_header_only_dat_is_rejected(tmp_path):
    '''Review Focus 5: stopped before the first exposure.'''
    folder = write_alignment(tmp_path / '151027', n_rows=0)
    with pytest.raises(ValueError, match='151027'):
        parse_dat(folder / 'scan151027.dat', 'alignment')
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/format_test.py -k alignment -v`
Expected: the first fails with four columns named `Position, Pilatus, Crystal,
Norm` holding shifted values; the third fails with `StopIteration` or
`IndexError` rather than `ValueError`.

- [ ] **Step 3: Declare the leading column and use it in `parse_dat`**

Add the constant next to `_PROFILE_SCAN_TYPES`, then prepend the synthesised
name before `scalars_n` is computed — the placement is the whole fix:

```python
header_fields = lines[0][2:].split('\t')
if scan_type in _LEADING_LOOP_TYPES:
    header_fields = ['Loop'] + header_fields
scalars_n = len(header_fields) - 1 if has_profile else len(header_fields)
```

Raise `ValueError(f'{path}: no data rows')` when `raw_rows` is empty, before
the per-row loop. Update `parse_dat`'s `Args:` entry for `scan_type` and its
`Raises:` section.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/format_test.py -v`
Expected: PASS. The `expected`-field-count logic is untouched, so every
existing type's tests must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py tests/format_test.py
git commit -m "fix(udkm): parse the alignment table's unnamed loop column"
```

---

### Task 3: Plan frames by index

**Files:**
- Modify: `src/pxs_import/frames.py` (`plan_frames`, new module constant)
- Test: `tests/frames_test.py`

**Interfaces:**
- Consumes: `write_alignment` (Task 1), `parse_dat` (Task 2).
- Produces: `_INDEXED_FRAME_TYPES: tuple[str, ...] = ('xRSS', 'alignment')` —
  types whose frames are addressed by row index rather than by an
  `ImageName` column.

- [ ] **Step 1: Write the failing tests**

```python
def test_alignment_frames_planned_by_index(tmp_path):
    folder = write_alignment(tmp_path / '151027', n_rows=4)
    table = parse_dat(folder / 'scan151027.dat', 'alignment')
    plan = plan_frames(folder, table, 'alignment')
    assert plan.n_frames == 4
    assert not plan.truncated
    assert [f.name for f in plan.files] == [f'image{i:05d}.tif' for i in range(4)]


def test_alignment_unreferenced_tifs_are_reported_not_loaded(tmp_path, caplog):
    '''Review Focus 2: an aborted run leaves TIFFs past the last .dat row.'''
    folder = write_alignment(tmp_path / '151027', n_rows=4, n_tifs=7)
    table = parse_dat(folder / 'scan151027.dat', 'alignment')
    plan = plan_frames(folder, table, 'alignment')
    assert plan.n_frames == 4
    assert len(plan.unreferenced) == 3
    assert 'unreferenced' in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/frames_test.py -k alignment -v`
Expected: FAIL — `plan_frames` takes the `image_names` branch and raises
`TypeError` iterating `None`.

- [ ] **Step 3: Route `alignment` down the index branch**

Replace `plan_frames`'s `if scan_type == 'xRSS':` with a membership test
against `_INDEXED_FRAME_TYPES`, and update the function's docstring, which
currently names `xRSS` as the only such type and lists the three valid
`scan_type` values.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/frames_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/frames.py tests/frames_test.py
git commit -m "feat(udkm): plan alignment frames by row index"
```

---

### Task 4: Variables and primary axis

**Files:**
- Modify: `src/pxs_import/reader.py` (`COLUMN_VARIABLES`, `PRIMARY_PATH`)
- Test: `tests/build_test.py`

**Interfaces:**
- Consumes: Tasks 1–3, and `_build(folder, scan_type, **kw)` — the helper
  already at the top of `tests/build_test.py`, which assembles a
  `ScanFolder`, parses, plans frames, loads the stack and calls
  `build_scan_h5like`. Use it; do not add a second helper beside it.
- Produces:
  - `COLUMN_VARIABLES['alignment'] = {'Loop': 'loop', 'Position': 'position', 'Pilatus': 'counts', 'Crystal': 'crystal_v', 'Norm': 'norm'}`
  - `PRIMARY_PATH['alignment'] = 'measurement/position'`

`loop` is already in `INTEGER_VARIABLES`; `position`, `counts`, `crystal_v`
and `norm` are absent from `GROUP_OF` and so land in `measurement`. No change
to either table.

- [ ] **Step 1: Write the failing tests**

```python
def test_alignment_layout_and_primary_axis(tmp_path):
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4)
    h5 = _build(d, 'alignment')
    numpy.testing.assert_allclose(h5['measurement/position'][()],
                                   [-1.0, -0.9, -0.8, -0.7])
    assert h5['measurement/loop'].dtype == numpy.int64
    assert h5['instrument/pilatus/images'].shape == (4, 4, 6)
    for name in ('counts', 'crystal_v', 'norm'):
        assert h5[f'measurement/{name}'].shape == (4,)
    assert h5.attrs['k-scan-type'] == 'alignment'
    assert h5.attrs['k-primary:0'] == 'measurement/position'


def test_alignment_short_run_writes_no_full_duplicates(tmp_path):
    '''Every real alignment scan stops early; that is not truncation.'''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4)
    h5 = _build(d, 'alignment')
    assert 'measurement_full' not in h5
    for attr in ('truncated', 'rows', 'frames'):
        assert attr not in h5.attrs
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/build_test.py -k alignment -v`
Expected: FAIL with `KeyError: 'alignment'` from `COLUMN_VARIABLES`.

- [ ] **Step 3: Add the two table entries**

Add both entries to `src/pxs_import/reader.py`. Note in a comment on the
`alignment` column map that `Loop` is the name `parse_dat` synthesises for the
undeclared leading column, so a reader of this table is not left looking for
it in the file's header.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/build_test.py -v`
Expected: PASS. `plan.truncated` is `False` because `n_rows == n_frames`, so
the `measurement_full` branch is never entered — no code guards it, the test
proves it.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py tests/build_test.py
git commit -m "feat(udkm): map alignment columns and primary axis"
```

---

### Task 5: The motor tag

**Files:**
- Modify: `src/pxs_import/reader.py` (`build_scan_h5like`, root attributes)
- Test: `tests/build_test.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: root attributes `k-header:stage` and `k-header:unit`, written only
  for `scan_type == 'alignment'` and only when the corresponding
  `parameters*.txt` key is present.

Placement matters: these go with the other `k-header:` writes, **before** the
generic `parameters*.txt` sweep, so the bare `Stage`/`Unit` attributes the
sweep writes are unaffected and neither can displace the other.

- [ ] **Step 1: Write the failing tests**

```python
def test_alignment_publishes_motor_and_unit(tmp_path):
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4,
                        stage='X Linear', unit='mm')
    h5 = _build(d, 'alignment')
    assert h5.attrs['k-header:stage'] == 'X Linear'
    assert h5.attrs['k-header:unit'] == 'mm'
    assert h5.attrs['Stage'] == 'X Linear'          # bare sweep still runs


def test_alignment_without_stage_still_builds(tmp_path, caplog):
    '''Review Focus 3: the key is absent, not the whole file.'''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4,
                        stage=None, unit=None)
    with caplog.at_level(logging.WARNING):
        h5 = _build(d, 'alignment')
    assert 'k-header:stage' not in h5.attrs
    assert 'k-header:unit' not in h5.attrs
    assert 'stage' in caplog.text.lower()


def test_alignment_parameters_survive_non_ascii(tmp_path):
    '''Review Focus 4: real files are Windows-1252 and record degrees.'''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4,
                        stage='Omega', unit='deg',
                        params={'Start Position': '-10.000000°'})
    h5 = _build(d, 'alignment')
    assert h5.attrs['k-header:stage'] == 'Omega'
    assert h5.attrs['Start Position'] == -10.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/build_test.py -k "motor or without_stage or non_ascii" -v`
Expected: FAIL with `KeyError: 'k-header:stage'`.

- [ ] **Step 3: Write the two attributes**

In `build_scan_h5like`, alongside the existing `k-header:integration` write:
for `alignment` only, set `k-header:stage` from `params['Stage']` and
`k-header:unit` from `params['Unit']`, each only if present. When `Stage` is
missing, log a warning naming `scan.source`, then carry on. Values are written
verbatim — no normalisation (spec §8).

Extend the comment block above the generic sweep to say why these two keys get
a second, prefixed copy: only `k-header:*` reaches nx5d's listing, and the
motor is the one thing a reader needs to tell two alignment scans apart.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/build_test.py -v && pytest -q`
Expected: PASS, whole default suite green.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py tests/build_test.py
git commit -m "feat(udkm): publish the alignment motor as a listing tag"
```

---

### Task 6: Pin the real corpus, and document the type

**Files:**
- Modify: `tests/corpus_test.py`
- Modify: `CLAUDE.md` (the scan-type and `io/` descriptions)
- Test: `tests/corpus_test.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: corpus coverage for `notebooks/20250408` (15 scans) and
  `notebooks/20250424` (4 scans). These are bare date directories, which
  `discover_scans` accepts directly — do **not** rearrange them into a
  proposal root (spec §6.3).

`202504_NbO2` is deliberately **not** added to the corpus proposal list
(spec §6.4).

- [ ] **Step 1: Write the corpus tests**

Follow the file's existing `needs`/`skipif` pattern so the tests skip cleanly
when the folders are absent.

```python
@pytest.mark.corpus
@pytest.mark.parametrize('date_dir,n', [('20250408', 15), ('20250424', 4)])
def test_alignment_corpus_classifies_and_parses(date_dir, n):
    scans = discover_scans(NB / date_dir)
    assert len(scans) == n
    assert {s.scan_type for s in scans} == {'alignment'}
    for folder in scans:
        table = parse_dat(folder.dat_path, 'alignment')
        assert list(table.columns)[:2] == ['Loop', 'Position']


@pytest.mark.corpus
def test_alignment_corpus_stage_vocabulary():
    '''Both acquisition programs, and the motors each of them writes.'''
    seen = collections.defaultdict(set)
    for date_dir in ('20250408', '20250424'):
        for folder in discover_scans(NB / date_dir):
            params = parse_parameters(folder.params_path)
            seen[date_dir].add((params['Stage'], params['Unit']))
    assert seen['20250408'] == {('X Linear', 'mm'), ('Y Linear', 'mm'),
                                 ('Z Linear', 'mm'), ('Omega', 'deg')}
    assert seen['20250424'] == {('Horizontal', 'steps'), ('Vertical', 'steps')}


@pytest.mark.corpus
def test_alignment_corpus_rows_match_frames():
    '''Every alignment scan stops early, and never mid-frame.'''
    for date_dir in ('20250408', '20250424'):
        for folder in discover_scans(NB / date_dir):
            table = parse_dat(folder.dat_path, 'alignment')
            n_tifs = len(list(folder.path.glob('image*.tif')))
            assert table.n_rows == n_tifs, folder.source
```

- [ ] **Step 2: Run the corpus suite**

Run: `pytest -m corpus -v`
Expected: PASS, including the 13 pre-existing corpus tests — the per-type
attribute vocabulary assertions for the 71 scans of `202601_PtAu`,
`202602_FeAu` and `202608_DyCo5Pt` must be unchanged by this work.

- [ ] **Step 3: Update `CLAUDE.md`**

Three places name the scan types or the `io/` module responsibilities:
the `io/format.py` row of the architecture table, the "Listing
attributes" section (which enumerates what carries a `k-header:` prefix — add
`stage` and `unit`, and say they are `alignment`-only), and the test-count
line in **Conventions**, which must match the suite's new totals.

- [ ] **Step 4: Verify both suites and the layering guard**

Run: `pytest -q && pytest -m corpus -q`
Expected: both green, `tests/packaging_test.py` included.

- [ ] **Step 5: Commit**

```bash
git add tests/corpus_test.py CLAUDE.md
git commit -m "test(udkm): pin the alignment corpus; document the type"
```
