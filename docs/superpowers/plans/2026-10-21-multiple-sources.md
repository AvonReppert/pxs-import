# Multiple Source Folders Implementation Plan

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `pxs-prepare` assemble one proposal from several source folders
— found automatically on the three known machines — and take only the days in
a given date range.

**Architecture:** Layout knowledge (year levels, date filtering) extends
`format.discover_scans`. Site knowledge (presets, base resolution, the
three source routes) goes into a new `io/sources.py`, keeping
`prepare.py` an orchestrator. The orchestrator becomes the single
numbering authority: it resolves `scan key → folder` once and hands the
reader the finished mapping, which stops doing its own discovery.

**Tech Stack:** Python 3.11+, pathlib, argparse, pytest.

**Spec:** [`docs/superpowers/specs/2026-10-21-multiple-sources-design.md`](../specs/2026-10-21-multiple-sources-design.md)

## Global Constraints

- Everything stays in `src/pxs_import/`. `signal.py` and `cooking.py` are not
  touched; `io/` must stay liftable as a pure file move.
- No module in `io/` imports `nx5d`. No nx5d change at all.
- Source folders are opened **read-only** — no mapping file, no spice, no
  marker written into one.
- The year level is **never constructed** from a requested date. Real data has
  `PilatusPumpProbe\2025\20260108\…`.
- Year nesting is at most **one** level deep.
- `--from` / `--to` are inclusive, day granularity, compared against the
  `YYYYMMDD` directory name.
- `from` is a Python keyword: reader parameters are `start`/`end`.
- `BASE_PRESETS` and `PROGRAM_DIRS` are copied verbatim from spec §5.1.
- Test command: `.venv/Scripts/python.exe -m pytest -q`. Corpus:
  add `-m corpus` (~4 minutes, needs `notebooks/`).

## Review Focus

Five conditions the spec implies; each has a test assigned to a task.

1. **A source root that is itself a date directory** (`notebooks/20250408`).
   Route 2 must claim it; a "contains date dirs" test alone sends it to route
   3 and turns one day into the whole archive. → Task 3.
2. **A year directory whose name disagrees with the dates inside it**
   (`2025/20260108`). Must be found, since the year is enumerated and never
   computed. → Task 1.
3. **`--to` omitted** — the range must end today, not at the start date, and
   not be unbounded into the future in a way that depends on the clock being
   right. → Task 2.
4. **Two sources carrying the same `YYYYMMDD/HHMMSS` with different scan
   types.** The coalesce path must not swallow this; it is a genuine
   conflict. → Task 4.
5. **An unreadable or vanished source directory** (permission denied on a
   share, a mount that dropped mid-walk). It must name the path, not surface
   as a bare `PermissionError` from inside a comprehension. → Task 2.

---

### Task 1: Year levels in `discover_scans`

**Files:**
- Modify: `src/pxs_import/format.py` (`discover_scans`)
- Test: `tests/format_test.py`

**Interfaces:**
- Produces: `discover_scans` additionally accepts a root whose children are
  `^\d{4}$` year directories holding the date directories. Return type is
  unchanged: `list[ScanFolder]`, sorted by `date + time`.

- [ ] **Step 1: Write the failing tests**

```python
def test_discover_scans_descends_one_year_level(tmp_path):
    write_alignment(tmp_path / '2026' / '20260108' / '151027', n_rows=2)
    write_alignment(tmp_path / '2026' / '20260109' / '090000', n_rows=2)
    assert [s.source for s in discover_scans(tmp_path)] == [
        '20260108/151027', '20260109/090000']


def test_discover_scans_year_folder_may_disagree_with_the_dates_in_it(tmp_path):
    '''Review Focus 2: PilatusPumpProbe\\2025\\20260108 is real.'''
    write_alignment(tmp_path / '2025' / '20260108' / '151027', n_rows=2)
    assert [s.source for s in discover_scans(tmp_path)] == ['20260108/151027']


def test_discover_scans_mixes_year_level_and_bare_date_dirs(tmp_path):
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)
    write_alignment(tmp_path / '2026' / '20260108' / '090000', n_rows=2)
    assert len(discover_scans(tmp_path)) == 2


def test_discover_scans_does_not_descend_two_year_levels(tmp_path):
    write_alignment(tmp_path / '2026' / '2025' / '20260108' / '151027', n_rows=2)
    assert discover_scans(tmp_path) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/format_test.py -k year -q`
Expected: FAIL — the first three return `[]`; the fourth already passes
(nothing is found today either) and is kept as the guard against over-eager
recursion.

- [ ] **Step 3: Add a `_YEAR_RE` and one descent level in `discover_scans`**

`_YEAR_RE = re.compile(r'^\d{4}$')` beside `_DATE_RE`. Collect date
directories from the root's own `^\d{8}$` children *and* from the children of
each `^\d{4}$` child — one level, never recursive. Update the docstring,
which currently names only the two accepted shapes.

- [ ] **Step 4: Run the whole format test file**

Run: `.venv/Scripts/python.exe -m pytest tests/format_test.py -q`
Expected: PASS, no pre-existing test regresses.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py tests/format_test.py
git commit -m "feat(udkm): discover scans behind a year directory"
```

---

### Task 2: Date filtering and the listing seam

**Files:**
- Modify: `src/pxs_import/format.py` (`discover_scans`)
- Test: `tests/format_test.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  `discover_scans(proposal_root, *, start: str | None = None, end: str | None = None, lister=None) -> list[ScanFolder]`
  - `start`/`end` are `YYYYMMDD` strings, both inclusive. `start=None` means
    no lower bound, `end=None` means no upper bound.
  - `lister` is a callable taking a `Path` and returning an iterable of
    `Path`, defaulting to `Path.iterdir`. It exists so a test can count
    directory accesses (spec §10).

The orchestrator passes `end=today` when the user omitted `--to`; the default
here stays `None` so that a direct caller is not surprised by a clock-dependent
bound.

- [ ] **Step 1: Write the failing tests**

```python
def test_discover_scans_filters_by_inclusive_date_range(tmp_path):
    for day in ('20250407', '20250408', '20250409', '20250410'):
        write_alignment(tmp_path / day / '151027', n_rows=2)
    got = discover_scans(tmp_path, start='20250408', end='20250409')
    assert [s.date for s in got] == ['20250408', '20250409']


def test_discover_scans_open_ended_range(tmp_path):
    for day in ('20250407', '20250408', '20250409'):
        write_alignment(tmp_path / day / '151027', n_rows=2)
    assert len(discover_scans(tmp_path, start='20250408')) == 2
    assert len(discover_scans(tmp_path, end='20250408')) == 2


def test_discover_scans_never_opens_a_day_outside_the_range(tmp_path):
    '''The §6.2 cost property, pinned on access rather than on the result.'''
    for day in ('20250407', '20250408', '20250409'):
        write_alignment(tmp_path / day / '151027', n_rows=2)
    seen = []
    def counting(path):
        seen.append(path)
        return path.iterdir()
    discover_scans(tmp_path, start='20250408', end='20250408', lister=counting)
    assert tmp_path / '20250407' not in seen
    assert tmp_path / '20250409' not in seen
    assert tmp_path / '20250408' in seen


def test_discover_scans_names_an_unreadable_directory(tmp_path):
    '''Review Focus 5: a share that drops mid-walk.'''
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)
    def failing(path):
        if path.name == '20250408':
            raise PermissionError(13, 'denied')
        return path.iterdir()
    with pytest.raises(OSError, match='20250408'):
        discover_scans(tmp_path, lister=failing)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/format_test.py -k "range or never_opens or unreadable" -q`
Expected: FAIL — `discover_scans() got an unexpected keyword argument 'start'`.

- [ ] **Step 3: Add the three parameters**

Route every directory read through `lister`. Apply the `start`/`end`
comparison to date-directory **names** (plain string comparison on `YYYYMMDD`
is correct and is what the spec allows) *before* descending into any of them.
Wrap each `lister` call so an `OSError` is re-raised naming the path it
failed on.

- [ ] **Step 4: Run the whole format test file**

Run: `.venv/Scripts/python.exe -m pytest tests/format_test.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py tests/format_test.py
git commit -m "feat(udkm): filter discovery by date range before descending"
```

---

### Task 3: Source resolution — presets, base, the three routes

**Files:**
- Create: `src/pxs_import/sources.py`
- Create: `tests/sources_test.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces, in `pxs_import.sources`:
  - `BASE_PRESETS: tuple[str, ...]` and `PROGRAM_DIRS: tuple[str, ...]`,
    verbatim from spec §5.1.
  - `resolve_base(explicit: str | Path | None = None, *, environ=None, exists=None) -> Path | None`
    — `explicit` beats `environ['UDKM_BASE']` beats the first preset that
    exists. `exists` is an injectable predicate (default `Path.exists`), so a
    test can assert the probe order without touching a filesystem. Returns
    `None` when nothing is found.
  - `program_dirs(base: Path, *, exists=None) -> list[Path]` — the
    `PROGRAM_DIRS` entries that exist under `base`, in `PROGRAM_DIRS` order.
  - `resolve_sources(proposal_root: Path, *, explicit: list[Path] | None = None, base: Path | None = None, environ=None, exists=None) -> tuple[list[Path], str]`
    — returns the resolved absolute source paths and which route produced
    them: `'explicit'`, `'positional'` or `'base'`. Raises `SourcesNotFound`
    (a `RuntimeError` subclass) naming the probed base paths when no route
    yields anything.
  - `is_date_dir(path: Path) -> bool` — `^\d{8}$` on `path.name`.
  - `has_date_dirs(path: Path, *, lister=None) -> bool` — true if any child
    matches `^\d{8}$` or `^\d{4}$`.

Route 2's predicate is `is_date_dir(p) or has_date_dirs(p)` — Review Focus 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_resolve_base_prefers_explicit_then_env_then_preset():
    seen = []
    def exists(p):
        seen.append(str(p)); return str(p) == BASE_PRESETS[1]
    assert resolve_base('/given', environ={}, exists=exists) == Path('/given')
    assert resolve_base(None, environ={'UDKM_BASE': '/env'}, exists=exists) == Path('/env')
    assert resolve_base(None, environ={}, exists=exists) == Path(BASE_PRESETS[1])


def test_resolve_base_probes_only_platform_shaped_presets():
    '''A UNC probe on POSIX, or a POSIX probe on Windows, is wasted and slow.'''
    seen = []
    def exists(p):
        seen.append(str(p)); return False
    resolve_base(None, environ={}, exists=exists)
    assert all(s in BASE_PRESETS for s in seen)
    assert len(seen) < len(BASE_PRESETS)


def test_resolve_base_returns_none_when_nothing_exists():
    assert resolve_base(None, environ={}, exists=lambda p: False) is None


def test_program_dirs_keeps_declared_order_and_skips_absent(tmp_path):
    for name in ('PilatusScanZaber', 'PilatusTheta2Theta'):
        (tmp_path / name).mkdir()
    assert [p.name for p in program_dirs(tmp_path)] == [
        'PilatusTheta2Theta', 'PilatusScanZaber']


def test_explicit_sources_win(tmp_path):
    src = tmp_path / 'elsewhere' / '20250408' / '151027'
    write_alignment(src, n_rows=2)
    ws = tmp_path / 'ws' / '20250408' / '151027'
    write_alignment(ws, n_rows=2)
    got, route = resolve_sources(ws.parent.parent, explicit=[src.parent.parent])
    assert route == 'explicit' and got == [src.parent.parent.resolve()]


def test_positional_that_is_itself_a_date_dir_is_the_source(tmp_path):
    '''Review Focus 1: notebooks/20250408 must not fall through to the base.'''
    day = tmp_path / '20250408'
    write_alignment(day / '151027', n_rows=2)
    got, route = resolve_sources(day, base=tmp_path / 'nonexistent')
    assert route == 'positional' and got == [day.resolve()]


def test_positional_holding_date_dirs_is_the_source(tmp_path):
    write_alignment(tmp_path / 'prop' / '20250408' / '151027', n_rows=2)
    got, route = resolve_sources(tmp_path / 'prop')
    assert route == 'positional'


def test_empty_workspace_falls_through_to_the_base(tmp_path):
    ws = tmp_path / 'prop'; ws.mkdir()
    base = tmp_path / 'base'
    (base / 'PilatusTheta2Theta').mkdir(parents=True)
    got, route = resolve_sources(ws, base=base)
    assert route == 'base' and got == [(base / 'PilatusTheta2Theta').resolve()]


def test_nothing_found_raises_naming_the_probed_paths(tmp_path):
    ws = tmp_path / 'prop'; ws.mkdir()
    with pytest.raises(SourcesNotFound) as e:
        resolve_sources(ws, environ={}, exists=lambda p: False)
    assert BASE_PRESETS[0] in str(e.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/sources_test.py -q`
Expected: FAIL — `ModuleNotFoundError: pxs_import.sources`.

- [ ] **Step 3: Write `src/pxs_import/sources.py`**

Module docstring states the two layering rules this subpackage lives by (no
`nx5d` import; sources are read-only). For the platform filter: probe a
preset only if its shape matches this platform — a drive letter or a `\\`
prefix on Windows, a leading `/` elsewhere. Before probing a path that is not
local, log at INFO which one is being tried, so a stalled UNC probe is
explicable (spec §5.1).

- [ ] **Step 4: Run the new tests**

Run: `.venv/Scripts/python.exe -m pytest tests/sources_test.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/sources.py tests/sources_test.py
git commit -m "feat(udkm): resolve source folders from presets, base or workspace"
```

---

### Task 4: Merging, collisions and one numbering

**Files:**
- Modify: `src/pxs_import/format.py` (`ScanFolder`)
- Modify: `src/pxs_import/sources.py`
- Test: `tests/sources_test.py`

**Interfaces:**
- Consumes: Task 3.
- Produces:
  - `ScanFolder` gains a fifth field `root: Path | None = None` — the source
    root it was discovered under. Defaulted, so every existing positional
    construction in code and tests keeps working.
  - `discover_all(sources: list[Path], *, start=None, end=None, lister=None) -> list[ScanFolder]`
    in `sources` — discovers each source in order, stamps `root`, merges,
    sorts by `date + time`, and resolves duplicates.
  - `ScanTypeConflict(RuntimeError)` — same `source` key, different
    `scan_type`.

Duplicate rule (spec §7, decided by the user): same `source` key and same
`scan_type` → the earlier source in `sources` order wins, and a
`logger.warning` names both folder paths. Same key, different type →
`ScanTypeConflict` naming both.

- [ ] **Step 1: Write the failing tests**

```python
def test_discover_all_numbers_chronologically_across_sources(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '120000', n_rows=2)
    write_alignment(b / '20250408' / '110000', n_rows=2)
    write_alignment(a / '20250409' / '090000', n_rows=2)
    got = discover_all([a, b])
    assert [s.source for s in got] == [
        '20250408/110000', '20250408/120000', '20250409/090000']
    assert [number_scans(got)[i][0] for i in range(3)] == ['r0001', 'r0002', 'r0003']


def test_discover_all_stamps_the_source_root(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '120000', n_rows=2)
    write_alignment(b / '20250409' / '090000', n_rows=2)
    assert {s.root for s in discover_all([a, b])} == {a.resolve(), b.resolve()}


def test_discover_all_coalesces_identical_duplicates_with_a_warning(tmp_path, caplog):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '151027', n_rows=2)
    write_alignment(b / '20250408' / '151027', n_rows=2)
    with caplog.at_level(logging.WARNING):
        got = discover_all([a, b])
    assert [s.source for s in got] == ['20250408/151027']
    assert got[0].root == a.resolve()          # first source wins
    assert 'a' in caplog.text and 'b' in caplog.text


def test_discover_all_refuses_a_type_conflict(tmp_path):
    '''Review Focus 4: same key, different scan type, is not a duplicate.'''
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '151027', n_rows=2)
    write_trss(b / '20250408' / '151027', n_rows=2)
    with pytest.raises(ScanTypeConflict, match='20250408/151027'):
        discover_all([a, b])


def test_discover_all_applies_the_date_range_to_every_source(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250407' / '120000', n_rows=2)
    write_alignment(b / '20250409' / '090000', n_rows=2)
    assert discover_all([a, b], start='20250408') == [
        s for s in discover_all([b])]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/sources_test.py -k "discover_all" -q`
Expected: FAIL — `discover_all` is not defined.

- [ ] **Step 3: Add `root` to `ScanFolder` and implement `discover_all`**

`root` is the fifth field with a `None` default. `discover_scans` sets it to
the root it was called with. Sorting is by `(date, time)`; with duplicates
resolved the sort is total.

- [ ] **Step 4: Run both suites**

Run: `.venv/Scripts/python.exe -m pytest tests/sources_test.py tests/format_test.py -q`
Expected: PASS. The `ScanFolder` change must not disturb existing
constructions.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/format.py src/pxs_import/sources.py tests/sources_test.py
git commit -m "feat(udkm): merge several sources into one chronological numbering"
```

---

### Task 5: The reader materialises a resolved mapping

**Files:**
- Modify: `src/pxs_import/reader.py` (`scans_from_pxs`)
- Test: `tests/reader_test.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `scans_from_pxs` gains `resolved: str | None = None`. When given,
  it is a JSON array of `[scan_key, absolute_folder_path, scan_type]` triples;
  the reader builds its `ScanFolder` list from it and performs **no**
  discovery, no numbering and no date filtering. When absent, behaviour is
  exactly as today.

JSON rather than a delimited string: Windows paths carry `\`, drive letters
and UNC prefixes, and `json` is in the standard library. The positional
`source` argument is ignored when `resolved` is given (spec §7.1) — it is the
data-free workspace in two of the three routes.

- [ ] **Step 1: Write the failing tests**

```python
def test_reader_uses_the_resolved_mapping_and_ignores_the_positional(tmp_path):
    a = tmp_path / 'a' / '20250408' / '151027'
    write_alignment(a, n_rows=2)
    workspace = tmp_path / 'ws'; workspace.mkdir()
    resolved = json.dumps([['r0007', str(a), 'alignment']])
    got = list(scans_from_pxs(workspace, resolved=resolved))
    assert [k['scan'] for k, _ in got] == ['r0007']
    assert got[0][0]['udkm_source'] == '20250408/151027'


def test_reader_without_resolved_still_discovers(tmp_path):
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)
    got = list(scans_from_pxs(tmp_path))
    assert [k['scan'] for k, _ in got] == ['r0001']


def test_reader_rejects_a_resolved_entry_whose_folder_is_gone(tmp_path):
    resolved = json.dumps([['r0001', str(tmp_path / 'missing'), 'alignment']])
    with pytest.raises(RuntimeError, match='missing'):
        list(scans_from_pxs(tmp_path, resolved=resolved))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/reader_test.py -k resolved -q`
Expected: FAIL — unexpected keyword argument `resolved`.

- [ ] **Step 3: Implement the `resolved` branch**

Reconstruct each `ScanFolder` from the triple: `Path(folder)`, its
`parent.name` as `date`, its `name` as `time`, the given `scan_type`. Keep
the existing per-scan `RuntimeError` wrapping, which already names the scan.
Update the docstring, including the `only=` interaction: `only` still filters,
and is simply unnecessary when `resolved` already names one scan.

- [ ] **Step 4: Run the reader tests**

Run: `.venv/Scripts/python.exe -m pytest tests/reader_test.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py tests/reader_test.py
git commit -m "feat(udkm): let the reader materialise a resolved scan mapping"
```

---

### Task 6: Wire it into `prepare_proposal` and the CLI

**Files:**
- Modify: `src/pxs_import/prepare.py` (`prepare_proposal`, `_s3_import_loop`, `main`)
- Test: `tests/prepare_test.py`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces: `prepare_proposal` gains `sources: list[Path] | None = None`,
  `base: Path | None = None`, `start: str | None = None`,
  `end: str | None = None`. New CLI flags `--source` (repeatable), `--base`,
  `--from`, `--to` (both `YYYY-MM-DD`, normalised to `YYYYMMDD` before use).
  Both `pm` invocations gain `resolved=<json>`.

Behaviour this task owns:
- Route 3 without `start` → exit non-zero (spec §5).
- The workspace directory is created when it does not exist.
- New source paths are `.resolve()`d where `proposal_root` already is
  (`prepare.py:343-345`).

- [ ] **Step 1: Write the failing tests**

```python
def test_base_route_without_from_is_refused(tmp_path):
    ws = tmp_path / 'prop'; ws.mkdir()
    base = tmp_path / 'base'; (base / 'PilatusTheta2Theta').mkdir(parents=True)
    with pytest.raises(SystemExit):
        prepare_proposal(ws, base=base, pm=['pm'], runner=_recording_runner())


def test_date_range_reaches_both_pm_paths(tmp_path):
    '''The resolved mapping, not a source path, is what pm is told.'''
    ...  # build two sources, call prepare_proposal with start/end and a
         # recording runner; assert the command carries `resolved=` and that
         # the decoded JSON holds exactly the in-range scans


def test_workspace_is_created_when_absent(tmp_path):
    ...  # proposal_root does not exist; after the call it does


def test_from_to_are_normalised_from_iso(tmp_path):
    ...  # --from 2025-04-08 reaches discover_all as '20250408'
```

Fill the three sketched bodies following this file's existing recording-runner
pattern; their assertions are named in the comments.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/prepare_test.py -k "base_route or date_range or workspace_is_created or normalised" -q`
Expected: FAIL — unexpected keyword arguments.

- [ ] **Step 3: Implement**

Replace the `discover_scans(proposal_root)` call at `prepare.py:354`
with source resolution plus `discover_all`. Add `resolved=<json>` to the
single-call command (`:395-399`) and to `_s3_import_loop`'s per-scan command
(`:228-232`); in the S3 loop the resolved list is the single scan being
imported, which makes `only=` redundant there.

- [ ] **Step 4: Run the prepare tests and the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/prepare.py tests/prepare_test.py
git commit -m "feat(udkm): --source, --base, --from and --to for pxs-prepare"
```

---

### Task 7: Source column, and renumbering detection

**Files:**
- Modify: `src/pxs_import/reader.py` (`write_mapping_table`)
- Modify: `src/pxs_import/prepare.py`, `src/pxs_import/s3.py`
- Test: `tests/prepare_test.py`, `tests/s3_test.py`

**Interfaces:**
- Consumes: Task 6.
- Produces:
  - `scan-mapping.csv` gains a fourth column `root`; header becomes
    `scan,source,type,root`.
  - S3 manifest entries gain `'root'`. Entries written before this change
    lack it and must keep working.
  - `read_mapping_table(path: Path) -> dict[str, str]` in `reader.py` —
    `source → scan key` from an existing CSV, `{}` if absent. Tolerates the
    three-column form.
  - `renumbered(previous: dict[str, str], mapping) -> list[tuple[str, str, str]]`
    — `(source, old key, new key)` for every scan whose number moved.

`prepare_proposal` reads the CSV before overwriting it and, if `renumbered`
is non-empty, raises naming the first few — unless `force` is set, in which
case it warns and continues (spec §8.1; the `force` interaction is the one
§11 left open and is settled here, because `--force` already means "I know
this overwrites").

- [ ] **Step 1: Write the failing tests**

```python
def test_mapping_table_records_the_source_root(tmp_path):
    ...  # write_mapping_table over two roots; header and a row asserted


def test_read_mapping_table_tolerates_the_old_three_column_form(tmp_path):
    path = tmp_path / 'scan-mapping.csv'
    path.write_text('scan,source,type\nr0001,20250408/151027,alignment\n')
    assert read_mapping_table(path) == {'20250408/151027': 'r0001'}


def test_renumbered_reports_moved_scans():
    previous = {'20250408/151027': 'r0001', '20250408/160000': 'r0002'}
    ...  # a mapping where 160000 became r0003
    assert renumbered(previous, mapping) == [('20250408/160000', 'r0002', 'r0003')]


def test_reimport_with_shifted_numbers_is_refused_without_force(tmp_path):
    ...  # import, insert an earlier scan, re-import -> raises naming the scan


def test_manifest_entry_without_root_still_skips(tmp_path):
    ...  # a manifest written in the old format is still honoured
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/prepare_test.py tests/s3_test.py -k "source_root or three_column or renumbered or shifted or without_root" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Keep `format_mapping_table`'s printed form unchanged — it is read by people,
and the root is already visible in the warnings.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py src/pxs_import/prepare.py src/pxs_import/s3.py tests/
git commit -m "feat(udkm): record the source root and detect renumbering"
```

---

### Task 8: Corpus coverage and documentation

**Files:**
- Modify: `tests/corpus_test.py`
- Modify: `CLAUDE.md`, `doc/pages/howto.md`
- Test: `tests/corpus_test.py`

**Interfaces:**
- Consumes: Tasks 1–7.

- [ ] **Step 1: Write the corpus test**

```python
@needs
@pytest.mark.parametrize('start,end,n', [('20250408', '20250408', 30),
                                         ('20250409', '20250410', ...)])
def test_date_range_over_nbo2(start, end, n):
    '''A real proposal holding all four programs' output.'''
    got = discover_scans(NB / '202504_NbO2', start=start, end=end)
    assert len(got) == n
    assert all(start <= s.date <= end for s in got)
```

Determine the expected counts by running `discover_scans` over
`notebooks/202504_NbO2` per day; do not guess them. Guard with the existing
`needs`.

- [ ] **Step 2: Run the corpus suite**

Run: `.venv/Scripts/python.exe -m pytest -m corpus -q`
Expected: PASS, including the 19 pre-existing corpus tests.

- [ ] **Step 3: Update the documentation**

`CLAUDE.md`: a row for `io/sources.py` in the architecture table, the
re-measured LOC figures for every `io/` file the work touched, and the test
counts. `doc/pages/howto.md`: the new flags, the preset table
from spec §5.1, and the `scan-mapping.csv` fourth column.

- [ ] **Step 4: Verify both suites and the layering guard**

Run: `.venv/Scripts/python.exe -m pytest -q && .venv/Scripts/python.exe -m pytest -m corpus -q`
Expected: both green, `tests/packaging_test.py` included.

- [ ] **Step 5: Commit**

```bash
git add tests/corpus_test.py CLAUDE.md doc/pages/howto.md
git commit -m "test(udkm): date-range corpus coverage; document multi-source import"
```
