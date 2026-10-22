# The `alignment` scan type for the UDKM importer

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

**Status:** design, awaiting implementation. No code written.
**Date:** 2026-10-21
**Target repository:** `xraydc` only. No nx5d change is required: the new type
is carried by the existing `-R pxs_import.reader:scans_from_pxs` reader, which
nx5d resolves by string.

---

## 1. Intent and why

The UDKM importer currently understands three PXS scan types — `sRSM`, `tRSS`
and `xRSS` — all of which produce a reciprocal-space or delay-resolved
measurement. The PXS also produces a fourth kind of scan, which the importer
does not recognise at all: **alignment scans**, in which a single motor is
stepped through a range while the Pilatus counts, so that the operator can find
a peak, a sample edge or the pump–probe overlap.

Two acquisition programs write these: `PilatusScanGoniometer` (sample and
detector goniometer axes) and `PilatusScanZaber` (Zaber linear stages used for
the pump–probe spatial overlap). They write an **identical** file format; only
the motor differs.

Today those folders are discovered, fail classification, and are skipped with a
warning. The scientist therefore cannot see their frames alongside the rest of
the proposal, and cannot tell from an nx5d listing which motor an alignment run
moved.

The intended end state: alignment scans import like any other scan, their raw
frames are readable (`prop.r0012.raw`), and an `nx5d`/`pm` listing of the
proposal shows which motor was moved.

## 2. Goals

- Classify `alignment` scan folders and import them through the existing
  `scans_from_pxs` reader, with no new entry point and no nx5d change.
- Parse the `scan<HHMMSS>.dat` table **correctly**, in particular its unnamed
  leading column (section 5.2).
- Import the raw TIFF stack, so the frames are viewable like any other scan's.
- Expose the motor that was moved as a tag visible in an nx5d proposal listing.
- Keep every change inside `src/pxs_import/`.

## 3. Non-goals

- No Q-space recipe for this type. Alignment scans are not reciprocal-space
  maps; their spice `recipes` entry stays absent, and `xraydc.cooking` is not
  touched.
- No import of `averages<HHMMSS>.dat` or `averages<HHMMSS>.png` (section 6.1).
- No plotting or analysis helpers. Those belong to the later plotting work.
- No support for alignment folders outside the `YYYYMMDD/HHMMSS/` layout; none
  exist.

## 4. Anti-goals — what would make this a failure even if it ran

- **Silently shifted columns.** The failure mode described in section 5.2
  produces a complete, plausible-looking dataset in which every column holds
  its neighbour's values. An import that "works" and yields this is worse than
  one that crashes.
- **Touching `signal.py` or `cooking.py`.** The `io/` subpackage is additive
  and importable out of this repository as a unit; that property is what keeps
  the eventual move into a standalone beamline package a file move rather than
  a merge. A change that reaches into the analysis modules forfeits it.
- **Misclassifying the 47 existing `tRSS` scans** through the `scan` /`scans`
  filename collision (section 5.1).
- **A scan type per motor.** The motor vocabulary is open-ended; see section
  4.1.

### 4.1 Why the motor is a tag and not a scan type

`notebooks/20250408` — one day, one acquisition program — contains four
distinct motors:

| folder | `Stage` | folder | `Stage` |
|---|---|---|---|
| 151027, 152317, 154043, 154120, 155428, 155536 | `X Linear` | 152532, 153137, 153447, 153620, 160122, 160247 | `Omega` |
| 151346, 151850 | `Z Linear` | 160608 | `Y Linear` |

`notebooks/20250424` adds `Horizontal` and `Vertical`. So "goniometer vs.
Zaber" is not the distinction that matters to analysis — the motor varies
*within* both groups, and it is the motor that decides how a scan is later
evaluated.

nx5d maps *scan type* onto a recipe URL. A type per motor would therefore mean
an open-ended type vocabulary that must be re-declared in every proposal's
spice whenever the acquisition software gains an axis. A tag is the correct
instrument for a property that varies per scan and that the scientist wants to
filter on. **Decision by the user:** the type is named `alignment`; the motor
is a tag.

## 5. The data, and what it demands

Surveyed: 19 scan folders (15 under `notebooks/20250408`, 4 under
`notebooks/20250424`), against all 71 scan folders of the existing proposals.

Every alignment folder holds exactly four non-image files plus a contiguous
`image%05d.tif` run:

```
averages<sfx>.dat   averages<sfx>.png   parameters<sfx>.txt   scan<sfx>.dat
```

### 5.1 Classification — the full PXS procedure

This is the complete automated assignment, as it stands once the two new
sources are included. It runs in three steps, and the decisive property is
that **the motor plays no part in determining the type.**

**Step 1 — find the folders.** `discover_scans` walks the proposal root and
keeps only what matches the layout:

```
<proposal>/YYYYMMDD/HHMMSS/
```

Anything else — zip files, `<time>Graphics` directories, `spice/` — falls
through silently, never having matched either regex. A root whose own name
matches `^\d{8}$` is treated as a single date directory, which is what makes
`notebooks/20250408` importable as it stands.

**Step 2 — type from the signature file.** For each scan folder,
`sfx = <HHMMSS>`, and the first signature that exists wins:

```python
SIGNATURES = {
    'sRSM':      'rockingcurves{sfx}.dat',
    'tRSS':      'scans{sfx}.dat',
    'xRSS':      'allSteps{sfx}.dat',
    'alignment': 'scan{sfx}.dat',      # new
}
```

No match means the folder is skipped with a warning naming it
`YYYYMMDD/HHMMSS`.

This is unambiguous because the full file sets do not overlap. Surveyed by
running `discover_scans` over every proposal under `notebooks/` — 134 scans in
total, of which **none is left unclassified** once `alignment` is added:

| type | signature | other files in the folder | folders |
|---|---|---|---|
| `sRSM` | `rockingcurves{sfx}.dat` | `intensities`, `parameters`, 4 × `.png` | 55 |
| `tRSS` | `scans{sfx}.dat` | `parameters` | 47 |
| `xRSS` | `allSteps{sfx}.dat` | `averages.dat`, `averages.png`, `parameters` | 13 |
| `alignment` | `scan{sfx}.dat` | `averages.dat`, `averages.png`, `parameters` | 19 |

Count these with `discover_scans`, not with `find`: a plain depth-limited
filesystem walk disagrees with it on `202504_NbO2`, and `discover_scans` is
what the importer actually sees.

Split by proposal, because only some of these are reachable from the corpus
tests today:

| proposal | sRSM | tRSS | xRSS | alignment | in `corpus_test.py` |
|---|---|---|---|---|---|
| `202601_PtAu` | 5 | 6 | 4 | – | yes |
| `202602_FeAu` | 18 | 11 | 9 | – | yes |
| `202608_DyCo5Pt` | 16 | 2 | 0 | – | yes |
| `202504_NbO2` | 16 | 28 | 0 | – | **no** |
| `20250408`, `20250424` | – | – | – | 19 | not yet |

The 71 scans of the first three are the ones the existing corpus assertions
pin; `202504_NbO2` is present on disk but untested (see section 6.3).

No folder holds two signatures, so dict iteration order never decides anything.
`averages{sfx}.dat` is shared between `xRSS` and `alignment`, which is exactly
why it is not a signature.

**Trap:** `tRSS` is `scans`, `alignment` is `scan`. One character apart, and a
slip here silently re-types 25 existing scans. The test in section 7 pins the
two against each other.

**Step 3 — motor, only after the type is fixed.** Goniometer and Zaber folders
both yield the type `alignment`. They are told apart afterwards, from
`parameters<sfx>.txt`:

| source program | `Stage` values | type | tag |
|---|---|---|---|
| `PilatusScanGoniometer` | `Omega`, `X Linear`, `Y Linear`, `Z Linear` | `alignment` | the motor |
| `PilatusScanZaber` | `Horizontal`, `Vertical` | `alignment` | the motor |

The program name itself is **not** evaluated. It appears in `Tmp Path` and
`Data Path` and is retained as an attribute, so the provenance is recoverable,
but it steers nothing.

### 5.2 Parsing — the unnamed leading column

Both programs write the same table:

```
% Position	Pilatus	Crystal	Norm          ← 4 declared names
1.000000	-1.000000	388023.000000	1031.350606	376.228024    ← 5 fields
```

The first field is the **loop index** and is not declared in the header. Every
existing type declares every column (`xRSS`'s `allSteps` names `Loop`
explicitly as its third column), so `parse_dat` assumes `len(header_fields)`
equals the data width for a non-profile type.

Fed this table unchanged it would compute `scalars_n = 4`, take `values[:4]`,
and zip them against the four declared names
([`format.py:446-456`](../../../src/pxs_import/format.py#L446-L456)).
Result:

| declared name | value it would receive | correct value |
|---|---|---|
| `Position` | loop index | position |
| `Pilatus` | position | Pilatus counts |
| `Crystal` | Pilatus counts | crystal monitor |
| `Norm` | — (dropped) | norm |

No warning, no exception, a full-length dataset of wrong numbers. This is
anti-goal #1 and the single most important thing the implementation must get
right.

The fix is a declared property of the type, not an inference: `parse_dat`
learns that `alignment` has one unnamed leading column, synthesises the name
`Loop` for it, and so arrives at five scalar columns. The existing
maximum-field-count logic for truncated rows is unaffected and still applies.

### 5.3 Variables

`COLUMN_VARIABLES['alignment']`, following `xRSS`'s names for the columns it
shares:

| column | variable | note |
|---|---|---|
| *(unnamed, synthesised `Loop`)* | `loop` | `int64` via `INTEGER_VARIABLES` |
| `Position` | `position` | the primary axis |
| `Pilatus` | `counts` | as `xRSS` |
| `Crystal` | `crystal_v` | as `xRSS` |
| `Norm` | `norm` | as `xRSS` |

`PRIMARY_PATH['alignment'] = 'measurement/position'`.

The dataset is named `position` for every motor, not after the motor itself —
the motor's identity lives in the tag (section 5.5). Otherwise the variable
layout would change shape per scan and no downstream code could address it
generically. **Builder's call, recorded here**; the alternative (naming the
dataset after `Stage`) is rejected for that reason.

### 5.4 Frames

No new code. Alignment folders use the `image{i:05d}.tif` convention and carry
no per-row `ImageName`, which is exactly the branch `plan_frames` already takes
for `xRSS` ([`frames.py:90`](../../../src/pxs_import/frames.py#L90)).
The new type only has to be routed down it.

One row per frame — and in **all 19** folders the row count equals the TIFF
count exactly.

That matters, because the existing `truncated` flag
([`frames.py:94`](../../../src/pxs_import/frames.py#L94), consumed at
[`reader.py:192-205`](../../../src/pxs_import/reader.py#L192-L205)) means "fewer
TIFFs on disk than `.dat` rows". On alignment data that condition is never
true, so routing this type down the existing branch yields `truncated=False`
throughout, and `truncated`/`rows`/`frames` are simply never written. Nothing
breaks; the flag just never fires.

A *different* notion of incompleteness does apply, and it is the normal case:
measured against `parameters*.txt` (`Start`/`End`/`Step Width` × `NbofLoops`),
**19 of 19 alignment scans stopped early** — `151027` ran 24 rows of 105,
`151346` 105 of 505. The operator stops the scan once the peak is visible.

**Decision by the user: that second notion is not recorded at all.** It would
be true for every alignment scan ever taken and so carries no information, and
it is recomputable from the `loop` column together with `NbofLoops`,
`Start Position`, `End Position` and `Step Width`, all of which are kept. No
`loops_done`/`loops_expected` attributes, no new flag.

Consequence: a short run is *not* truncated in the existing sense, so it does
not take the `measurement_full/<var>_full` branch of `build_scan_h5like`.

Note precisely why. **Nothing suppresses that branch for this type**, and it
should not: `alignment` shares the `xRSS` frame-planning path, so a scan whose
acquisition died between the row write and the TIFF flush — fewer TIFFs than
`.dat` rows — *will* set `truncated` and keep the full table, which is the
informative behaviour. The branch simply never fires on real data, because all
19 scans have `n_rows == n_tifs`. Treat "no truncation attribute" as an
observation about the corpus, not a guarantee the code makes; a test pins the
behaviour in both directions.

### 5.5 Attributes and the motor tag

`parameters<sfx>.txt` is already handled by the generic sweep: leading unkeyed
lines become `Comments`, and every `Key: value` pair is written as its own
bare-named attribute, numerically parsed where unambiguous. The alignment
vocabulary is:

```
Date Time · Stage · Unit · Start Position · End Position · Step Width
Exposure Time · File Name · Tmp Path · Data Path · ROI · NbofLoops
```

All of it survives the existing parser as-is: `ROI` matches the region shape,
`Exposure Time: 1.000000s` the suffixed float, `NbofLoops: 5` the bare integer,
`File Name: 151027` is held as a string by `IDENTIFIER_KEYS`. Note the
vocabulary drift from `xRSS`, which spells the same concepts
`Selected Stage` / `Stage Unit` / `# of Loops` — no collision, but the
implementation must read `Stage`, not `Selected Stage`, for this type.

**The tag.** Per
[`CLAUDE.md`](../../../CLAUDE.md), only attributes prefixed `k-header:` are
harvested into nx5d's scan summary and rendered in a listing's `others`
column; a bare attribute is readable off the scan but invisible in a listing.
`Stage` and `Unit` therefore already arrive as attributes today and are
*already* invisible. Making the motor a visible tag means adding, for this type
only:

```
k-header:stage  = <the Stage value, e.g. 'Omega'>
```

The existing bare `Stage`/`Unit` attributes stay as they are.

**When `Stage` is absent.** `test_missing_parameters_file_still_builds` pins
that a scan with no `parameters*.txt` still builds, so an alignment scan can
reach this point with no `Stage` to publish. The attribute is then omitted and
a warning logged naming the scan — not a hard failure. Rationale: the scan's
frames and table are still perfectly good data, and `k-header:integration` is
already treated this way. **Builder's call, recorded here.** Section 7 carries
the matching negative test.

## 6. Scope decisions

Four questions that came up during design and are now settled. Each records
who decided, so a later reader can tell a constraint from a preference.

### 6.1 `averages<sfx>.dat` is deliberately not imported

It holds one row per *intended* position (21 rows for −1…1 in 0.1 steps),
whereas `scan.dat` holds one row per *acquired* frame — a different and
shorter grid. It is derived data, recomputable from `scan.dat`, and its header
declares two names for three columns (the third column's meaning is not
established). Importing it would mean carrying a second, differently-shaped
axis for no gain. **Decision:** skip it; revisit only if the scientist asks for
the averaged curve specifically.

### 6.2 `k-header:unit` — resolved: add it

Earlier drafts defaulted to leaving it out, to keep the `others` column
readable. That is reversed, because `position` is stored raw (section 5.3) and
the raw numbers are not comparable across motors: a goniometer `Omega` runs
−10…24 in `deg`, a Zaber `Horizontal` 60500…61900 in `steps`. The motor name
alone does not tell the reader which. Two short tokens in `others` are worth
that.

```
k-header:stage = 'Omega'      k-header:unit = 'deg'
```

**Builder's call, recorded here.** It stays one attribute each; no composite
string.

### 6.3 Corpus fixtures and `202504_NbO2`

`notebooks/20250408` and `notebooks/20250424` are bare date directories, not
proposal roots. `discover_scans` handles that shape explicitly (a root whose
own name matches `^\d{8}$` is treated as a single date directory), so they are
importable as they stand. The corpus tests should cover both, so that the
goniometer and Zaber spellings of `Stage` are each exercised.

### 6.4 Not merged into `202504_NbO2` — and the renumbering question it raises

`202504_NbO2` contains date directories `20250408` and `20250424` — the same
two dates as the standalone alignment folders, and on `20250424` the times
interleave with the scans already there. They are evidently one beamtime split
across two top-level folders.

**Decision by the user: they stay separate.** The alignment folders remain
their own import targets, and this design grows no renumbering story.

What that decision buys, and what it defers, is worth stating because it does
not stay bought:

- Bought now: merging would make `number_scans` renumber chronologically, so
  every existing NbO2 `r`-number after `20250424/125106` would shift.
  `202504_NbO2` has no `spice/` directory today, so nothing is anchored and
  the damage would in fact be zero — but the work of designing and testing
  the shift is real, and it is not what this vorhaben is for.
- Deferred: the same collision recurs on any future beamtime where alignment
  scans sit among the measurements, which is the normal case from now on.
  `migrate_spice` cannot absorb it — it migrates `date/time → r-number`, not
  `r-number → r-number`. This belongs to the "multiple source folders"
  vorhaben, where chronological ordering across sources is the subject
  anyway, not here.

Consequently `202504_NbO2` is also **not** added to `corpus_test.py` as
part of this work. Its 44 scans (16 `sRSM`, 28 `tRSS`) remain unpinned, as
they are today; adding them would change the exact-set assertions in
`test_numeric_companions_are_all_or_nothing_per_key` for no benefit this
vorhaben needs.

## 7. Testing

Default suite (synthetic, `tmp_path`):

- A synthetic alignment folder classifies as `alignment`.
- **Collision test:** a folder containing `scans<sfx>.dat` still classifies as
  `tRSS`, and one containing `scan<sfx>.dat` as `alignment`, asserted against
  each other in one test so the plural cannot drift.
- **Column test:** a table with a known five-field row parses to five columns
  with `Position` holding the position, not the loop index. This is the
  regression guard for section 5.2 and should assert values, not just shapes.
- A short run (fewer rows than `NbofLoops × positions`, i.e. every real
  alignment scan) imports cleanly, with the frame count matching the row count,
  **no** `measurement_full/*_full` duplicates and no truncation attribute of
  any kind.
- `k-header:stage` and `k-header:unit` carry the `Stage` / `Unit` values.
- A scan with no `parameters*.txt` still builds, omits both `k-header:`
  entries, and logs a warning (section 5.5).

Corpus suite (`pytest -m corpus`, real data):

- All 19 folders classify as `alignment` and parse without warnings.
- The `Stage` vocabulary is pinned across both proposals.
- The existing per-type attribute vocabulary assertions still hold for the 71
  pre-existing scans — this change must not alter them.

`pytest -m corpus` is deselected by default and will not announce its own
breakage; it must be run explicitly before this work is called done.

## 8. Left to the builder

- Exact mechanism for declaring "one unnamed leading column" in
  `format.py` (a per-type constant alongside `_PROFILE_SCAN_TYPES`, or an
  explicit expected-names table). Either is fine; it must be declarative and
  per-type, never inferred from field counts at runtime.
- Whether `Stage` is additionally normalised (e.g. lower-cased, spaces to
  underscores) for the tag value, or carried verbatim. Verbatim is the default,
  consistent with the rest of the attribute sweep.
