# UDKM → nx5d HDF5 importer

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

**Status:** design, awaiting implementation. Format spec validated against 71 scans in
three proposals (§5.6); no implementation has been written. Code references were
re-verified on 2026-10-19 against the local `../nx5d` checkout at `master` = `99c2ff6`
(clean, level with `origin/master`).
**Date:** 2026-10-19
**Target repository:** `xraydc` initially, as a plug-in reader module (§6.5). The intent
is to upstream it into `nx5d` later, after discussion with that project's maintainer;
§6.7 describes how to keep that move a near-no-op. No nx5d fork is required to build or
use this.

---

## 1. Intent and why

Laboratory plasma X-ray source ("PXS") diffraction data from the UDKM group is written
to disk as a flat folder tree:

```
<proposal>/YYYYMMDD/HHMMSS/
    <type><HHMMSS>.dat        tab-separated table, one row per scan point
    parameters<HHMMSS>.txt    free-text scan metadata
    image00000.tif …          one detector frame per scan point
```

nx5d can already *read* this layout directly from the filesystem
(`nx5d/xrd/reader.py`, class `ProjectRepository`). That path is not what we want, for two
reasons:

1. **Addressing.** It keys scans by date and time — `proposal.r260108120821`. Every other
   beamline the group works with addresses scans by a plain, chronological integer run
   number. The analyst wants the same here.
2. **Aggregation.** A single proposal frequently spans *several* date folders. The
   filesystem repository treats the date folder as part of the scan key, so there is no
   natural proposal-wide, chronologically ordered run numbering.

The fix is an **ingest step**: convert the flat folders into nx5d-native per-scan HDF5
files once, then let the standard `nx5d.Proposal` machinery serve them. This mirrors how
the group already works at other facilities (see `notebooks/XFEL23/data-prepare.py` in
`xraydc-2`, and nx5d's own `kmc3-from` CLI).

---

## 2. Goals

1. A **reader module shipped in `xraydc`**, `scans_from_pxs`, that turns one or more
   `YYYYMMDD/` folders into nx5d-shaped scans. It is driven by nx5d's existing
   `pm scan import` CLI rather than by a bespoke converter binary (§6.5) — no new CLI is
   written, and no nx5d fork is needed.
2. Scans numbered **chronologically across the whole proposal**, including across multiple
   date folders, as `r0001`, `r0002`, …
3. The result opens with unmodified nx5d:
   ```python
   from nx5d import Proposal
   prop = Proposal(url='file:///…/202601_PtAu/scans/{scan}.h5',
                   spice='file:///…/202601_PtAu/spice/')
   prop.r0008.raw      # xarray.Dataset
   prop.r0008.cooked   # Q-mapped via the xraydc recipe
   ```
4. Variable names in `.raw` match the motor names already used in the proposal's spice
   `exp_info` records (`theta`, `ttheta`), plus `delay` and `images`, so existing notebook
   cells and recipes work unchanged.
5. Two-theta for `tRSS` scans reconstructed **correctly** rather than guessed.
6. The proposal's existing spice records re-anchored from `"20260108/143930"` to the new
   scan keys.
7. The **same reader** can additionally publish a proposal to S3 for remote access,
   without a second implementation (§6.6).

## 3. Non-goals

- Replacing or modifying `nx5d/xrd/reader.py`'s filesystem `ProjectRepository`. It keeps
  working; we simply do not use it.
- Any change to `xraydc`'s `QMapper` or recipe layer. The importer's job ends when
  `prop.rNNNN.raw` returns a correct Dataset.
- Supporting UDKM data from other setups (MOKE, TG, femtospex). Scope is the PXS
  diffraction folders described above.
- A graphical or interactive tool. This is a batch script run once per beamtime.

## 4. Anti-goals

Things that would make this a failure even if it technically ran:

- **Making `xraydc` depend on `nx5d`.** `xraydc` deliberately stands alone
  (see its `README.md` and `CLAUDE.md`). The importer lives in `xraydc` as an optional
  module (§6.7) and touches nx5d only through the `pm scan import` reader contract
  (§6.5); it must never import `nx5d` at module level, and `xraydc`'s core must never
  import the importer.
- **Silently cooking scans that cannot be cooked.** `xRSS` scans carry no angle
  information at all (§5.3). They must be marked so that a recipe never silently produces
  a meaningless Q-map from them.
- **Losing the link back to the original folder.** Run numbers are recomputed on every
  conversion (§7); if that renumbering ever leaves a scan untraceable to its source
  folder, the data becomes unciteable against the e-lab notebook.
- **Requiring the analyst to hand-edit HDF5 or spice records** to get a proposal open.

---

## 5. Source format

Three scan flavours exist, distinguished by which `.dat` file is present. nx5d's existing
reader already uses exactly this signature test, and its scan-type names are reused here
because the proposal's spice `recipes` record is already keyed by them.

**The signature files are not the only `.dat` files, and scan folders are not the only
entries.** Measured over the same 71 scans:

| Entry | Count | Where | Handling |
|---|---|---|---|
| `intensities<HHMMSS>.dat` | 39 | every `sRSM` folder | Ignore. Header is `% Theta / °  2Theta + Corr / °  Intensities`, Windows-1252 encoded (`°` is not valid UTF-8). |
| `averages<HHMMSS>.dat` | 13 | every `xRSS` folder | Ignore for import (per-position means of the `allSteps` table). |
| `<HHMMSS>.zip` | 10 | `202601_PtAu/20260108/` | Zip archives beside the scan folders of the same name. Not scans. |
| `135936Graphics/` | 1 | `202602_FeAu/20260202/` | A directory that is not a scan. |

Consequently: discover scans as **directories** whose basename matches `^\d{6}$` under a
`^\d{8}$` date directory, never by globbing `*.dat` or `*/*/`; and classify by the three
exact filename patterns below (`rockingcurves{sfx}.dat`, `scans{sfx}.dat`,
`allSteps{sfx}.dat`, where `sfx` is the folder name), as `nx5d/xrd/reader.py` already does.

### 5.1 `sRSM` — `rockingcurves<HHMMSS>.dat`

```
% Loop	Theta / deg.	2Theta + Corr / deg.	Crystal	Crystal Calib	Rocking Curve
% StartTime	EndTime	ImageName
% 11:55:59.493	11:56:01.006	image00000.tif
1.000000	18.500000	37.000000	0.134368	774457.208069	0.000000	0.000000 …
```

Header declares 6 column names; data rows carry **492** fields. The first five are
scalars; the remaining 487 belong to the final declared column (`Rocking Curve`) and are a
detector-width line profile. A per-point `%`-prefixed metadata line gives the frame's
start time, end time and file name.

Both angles are present in the table. This type is directly cookable.

### 5.2 `tRSS` — `scans<HHMMSS>.dat`

```
% Loop_Delay	Delay	Loop_Theta	Theta	Crystal [V]	Crystal [ph/s]	Pilatus [ph/s]	Repeats	ImageNumber	Intensities [ph/s]
% 12:08:22.466	12:08:24.983	image00000.tif
1.000000	-5.000000	1.000000	19.480000	0.128643	1563459.311439	1380.500000	0.000000	0.000000	0.000000 …
```

10 declared columns, **496** data fields: 9 scalars plus a 487-wide `Intensities [ph/s]`
profile. Per-point metadata line as above.

**There is no two-theta column.** nx5d's existing reader handles this by setting
`ttheta = theta * 2` and emitting
`logger.warning('msg="ACHTUNG, incomplete theta reconstruction"')`
(`nx5d/xrd/reader.py`, `_build_single`). That is a guess, and it is wrong whenever the
detector arm carries an offset.

The correct value is available: `parameters<HHMMSS>.txt` records

```
Theta Vector: 19.48
2 Theta Correction: 0.000000
```

so the importer must write

```
ttheta = 2 * theta + two_theta_correction
```

taking the correction from the parameters file.

**Scope of this fix, measured.** Across all 19 `tRSS` scans in the three proposals
surveyed (§5.6), `2 Theta Correction` is present in every parameters file and its value is
`0.000000` in every one. So `ttheta = 2*theta` is numerically correct for all data
currently in hand, and this change fixes no existing number. It is still the right thing
to do — it reads the recorded value instead of assuming it, and the assumption breaks
silently the first time the detector arm carries an offset — but it should be understood
as removing a latent trap, not as correcting present results.

### 5.3 `xRSS` — `allSteps<HHMMSS>.dat`

```
% Position set	Position Real	Loop	Pilatus	Crystal	Norm	Crystal Calib
10.000000	NaN	1.000000	5.000000	0.120620	51.218880	0.097620
```

7 declared columns, 7 data fields, **no trailing profile and no per-point metadata line**.
Frame file names are implied by row index (`image{row:05d}.tif`).

**No angle information exists anywhere for this type** — not in the table, and not in
`parameters<HHMMSS>.txt`, which records only the delay stage, exposure, ROI and
calibration. These are delay traces taken at a fixed, unrecorded angle, used for finding
time-zero from counts alone.

Consequently `xRSS` scans **cannot be Q-mapped**. Note that the proposal's current spice
`recipes` record maps `xRSS` to `pymod:///xraydc.cooking#TRSS_2d_fix`, which would fail or
produce nonsense. See §8 for the required handling.

### 5.4 `parameters<HHMMSS>.txt`

Free text; leading unkeyed lines are sample/fluence comments, followed by `Key: value`
pairs. Keys differ per scan type. nx5d's `paramsof()` already implements a tolerant
reader: try UTF-8, fall back to `windows-1252`, split on `': '`, accumulate unkeyed
leading lines into a `Comments` entry. Reuse that logic.

**Windows-1252 is the rule, not the exception.** Measured over all 71 parameters files:
69 require the `windows-1252` fallback and only 2 are valid UTF-8 (both in
`202608_DyCo5Pt`). The fallback is load-bearing — a reader that assumes UTF-8 fails on
97 % of the corpus. Two non-ASCII characters occur, both inside comment text:

| Codepoint | Character | Occurrences | Appears in |
|---|---|---|---|
| U+00B2 | `²` | 47 | `cm²`, `µm²` in fluence and spot-size notes |
| U+00B5 | `µ` | 17 | `µm²` in spot-size notes |

Both round-trip correctly through the fallback.

**`at_key` continuation never triggers.** `paramsof()` appends any unkeyed line that
follows a key onto that key's value. Across all 71 files this never happens — comments
always precede the first `Key: value` line. The behaviour is harmless here, but do not
rely on it to collect comments; it would silently corrupt a key if the acquisition
software ever emitted a trailing comment block.

**Latent fragility: a comment containing `": "` is swallowed as a key.** The split is on
colon-space, so an operator note like `Sample: MV217` would become a parameter named
`Sample` and vanish from the notes column. No comment anywhere in the corpus currently
contains a colon at all, so this is latent rather than live — but it is the most likely
way the notes column silently loses content in future beamtimes. If hardening is wanted,
restrict key-splitting to lines appearing *after* the first recognised key, or to a known
key vocabulary (§5.4 lists the complete set per type). A related detail: `paramsof()`
does `k, v = txt.split(': ')`, which raises `ValueError` on a line containing `": "`
twice. No value in the corpus does, but use `split(': ', 1)` in the lifted copy.

**Every key is universal within its scan type.** Surveyed across all 71 scans, each key
below appears in 100 % of that type's parameters files — there are no optional keys to
guard against:

| Type | Keys |
|---|---|
| `sRSM` | `Date Time`, `Start Position`, `End Position`, `2 Theta Correction`, `Step Width`, `Number of Loops`, `Exposure Time`, `File Name`, `Tmp Path`, `Data Path`, `ROI`, `Calib` |
| `tRSS` | `Date Time`, `Delay Vector`, `Delay Loop`, `Delay Order`, `Theta Vector`, `Theta Loop`, `Theta Order`, `2 Theta Correction`, `Inner Parameter`, `Exposure Time`, `Calibration`, `File Name`, `Tmp Path`, `Data Path`, `ROI` |
| `xRSS` | `Date Time`, `Selected Stage`, `Stage Unit`, `Start Position`, `End Position`, `Step Width`, `# of Loops`, `Velocity`, `Acceleration`, `Exposure Time`, `ROI`, `Pump Region`, `Norm Region`, `Filename`, `Tmp Path`, `Data Path`, `Calibration` |

**Key spellings are inconsistent between types** and must not be normalised by guesswork:

- `File Name` (`sRSM`, `tRSS`) vs `Filename` (`xRSS`)
- `Calib` (`sRSM`) vs `Calibration` (`tRSS`, `xRSS`)
- `Number of Loops` (`sRSM`) vs `# of Loops` (`xRSS`) vs `Delay Loop` + `Theta Loop` (`tRSS`)

**Comment presence is type-dependent**, which matters for §6.2.1:

| Type | Scans with comments |
|---|---|
| `sRSM` | **0 / 39** |
| `tRSS` | 17 / 19 |
| `xRSS` | 13 / 13 |

So an empty notes field on an `sRSM` scan is correct and expected, not a parsing failure.

**`sRSM` comments are not unsupported — merely never written.** All 39 `sRSM` files begin
with exactly two blank lines before `Date Time:`, i.e. the comment slot exists and is
empty. The 2 `tRSS` files that carry no comments have the identical leading shape
(`blank=2, comment=0`). Since `paramsof()` takes only a path and never branches on scan
type, an `sRSM` file that *did* carry a comment would be parsed exactly like a `tRSS` one.
Implement comment extraction uniformly for all three types; do not special-case `sRSM`
or skip it as an optimisation.

Broken down by proposal, the same fact is starker:

| Proposal | Scans with notes |
|---|---|
| `202601_PtAu` | 10 / 15 |
| `202602_FeAu` | 20 / 38 |
| `202608_DyCo5Pt` | **0 / 18** |

`202608_DyCo5Pt` contains no comments at all — it is 16 `sRSM` scans (which never carry
them) plus the only 2 `tRSS` scans in the corpus whose operator left the field blank. Its
notes column will be entirely empty, and that is a property of the data, not a bug to
chase. Any test asserting "notes are non-empty" must not use that proposal.

**`Exposure Time` needs unit-stripping, and the suffix is type-dependent:**

| Type | Value format | Example |
|---|---|---|
| `sRSM` | trailing `s` | `1.000000s` |
| `tRSS` | trailing `s` | `2.000000s` |
| `xRSS` | bare number | `1.000000` |

All 71 scans carry the key, so `k-header:integration` can always be populated — but the
parser must strip a trailing `s` before converting to float, or 58 of 71 scans will fail
to parse.

### 5.5 Known trap: the `images` name collision

nx5d's `_default_indicators` maps **both** `'Rocking Curve'` and `'Intensities [ph/s]'` to
the name `images`. The same reader then assigns the TIFF stack to `images` as well, so the
1-D line profile from the `.dat` file is silently overwritten by the 3-D frame stack.

The importer must not reproduce this. Use distinct names:

| Source | Name to write |
|---|---|
| TIFF frame stack | `images` |
| trailing 487-wide profile from the `.dat` | `curve` |

### 5.6 Corpus validation

The format description above was checked against every scan in three proposals before
this design was accepted:

| Proposal | Date folders | Scans | `sRSM` | `tRSS` | `xRSS` |
|---|---|---|---|---|---|
| `202601_PtAu` | 1 | 15 | 5 | 6 | 4 |
| `202602_FeAu` | **5** | 38 | 18 | 11 | 9 |
| `202608_DyCo5Pt` | 2 | 18 | 16 | 2 | 0 |
| **total** | | **71** | 39 | 19 | 13 |

Results:

- **The three-signature taxonomy is complete.** All 71 scans classify; none are ambiguous
  or unrecognised.
- **Column structure is invariant.** Every `sRSM` file has 6 declared columns and a
  487-wide trailing profile; every `tRSS` file has 10 and 487; every `xRSS` file has 7 and
  no profile. No exceptions.
- **Detector geometry is uniform.** Every first frame sampled is `(195, 487)` `int32`,
  matching the `imageSize: [195, 487]` in the proposals' spice `exp_info`, and confirming
  that the 487-wide trailing column is a detector-width line profile.
- **Multi-date proposals are real**, not hypothetical: `202602_FeAu` spans five date
  folders with scans interleaved across them. Proposal-wide chronological numbering (§7)
  is therefore load-bearing, not a convenience.

### 5.7 Frame sourcing — do not glob the TIFFs

This is the most important implementation constraint discovered during validation.

Across the 58 `sRSM`/`tRSS` scans, the `.dat` files reference **16 335** frames. Compared
against what is actually on disk:

| | Count | Share |
|---|---|---|
| referenced and present | 16 321 | 99.91 % |
| referenced but **missing from disk** | 14 | 0.09 % |
| **present but never referenced** | 753 | — |

Both discrepancies are real and must be handled, and they pull in opposite directions:

1. **Unreferenced frames exist on disk.** 753 of them, 751 concentrated in
   `202602_FeAu/20260203/120401` (1 364 rows, 2 115 TIFFs) — frames written during
   aborted loops or after the `.dat` was closed. **A converter that builds its frame stack
   by globbing `*.tif` would silently ingest 751 junk frames into that scan and misalign
   every row against its angles.** The `ImageName` column in the per-point metadata line
   is authoritative; the glob is not. This directly contradicts the `TiffSeriesH5`
   approach used by `_kmc3_from_tiff`, which is glob-driven — see §10.
2. **Referenced frames are occasionally absent.** 14 frames across 5 scans. These are
   genuine gaps mid-scan, so the row must be kept and the frame filled with NaN (nx5d's
   existing reader already does this and warns; preserve that behaviour).

   **dtype consequence, decided.** The TIFFs are `int32` (§5.6) and NaN does not exist in
   an integer array. Rather than let the stack dtype depend on whether a scan happens to
   have a gap, **always write the frame stack as `float32`**. The Pilatus counter is
   20-bit (max 1 048 575), which `float32` represents exactly, so this is lossless and
   the same 4 bytes per pixel as `int32`. Additionally write an `index`-aligned boolean
   `frame_missing` variable so the gaps remain machine-detectable without scanning for
   NaN.

Affected scans, for test fixtures:

```
202601_PtAu/20260108/131040     tRSS  refs=499   missing=0  unreferenced=1
202602_FeAu/20260203/120401     tRSS  refs=1364  missing=0  unreferenced=751
202602_FeAu/20260212/135237     tRSS  refs=820   missing=1  unreferenced=0
202602_FeAu/20260212/174520     tRSS  refs=82    missing=2  unreferenced=0
202602_FeAu/20260212/180247     tRSS  refs=172   missing=7  unreferenced=0
202602_FeAu/20260212/182802     tRSS  refs=277   missing=1  unreferenced=0
202608_DyCo5Pt/20260819/140224  sRSM  refs=16    missing=0  unreferenced=1
202608_DyCo5Pt/20260819/143408  sRSM  refs=135   missing=3  unreferenced=0
```

**`xRSS` is different and more dangerous.** It has no `ImageName` column at all — frame
names are implied by row index (`image{row:05d}.tif`). In 10 of 13 `xRSS` scans that
mapping is exactly 1:1. In 3 it is not, because the measurement was interrupted after the
table had been written:

```
202602_FeAu/20260212/160832   rows=219   tif=217
202602_FeAu/20260212/162948   rows=190   tif=182
202602_FeAu/20260213/125733   rows=1729  tif=489     <-- 72 % of frames absent
```

`allSteps` logs every step of every loop (rows = positions × loops; `125733` is 221
positions × 8 loops), so an interrupted run leaves a long tail of rows with no frame. The
existing nx5d reader would allocate a 1 729-frame stack that is 72 % NaN. The converter
must instead detect truncation, write only the frames that exist, and record the full
table separately — see §8.

### 5.8 Column → variable mapping

**Every leading scalar column is imported as a data variable.** None are dropped, and each
is written as a 1-D array on the `index` dimension with **exactly the same length as the
frame stack**, so that each point's angles, counts and loop indices line up with its
detector image. This is what makes `.sel()`, `.groupby()` and the recipe layer work on
these datasets at all.

Column names below are verbatim from the `.dat` header lines (tab-separated, punctuation
and case significant). Target names follow nx5d's existing `_default_indicators` where one
exists, so that upstreaming (§6.7) does not rename anything.

#### `sRSM` (`rockingcurves*.dat`) — 5 scalars + 1 profile

| Column (verbatim) | Variable | Unit | Notes |
|---|---|---|---|
| `Loop` | `loop` | — | Loop counter, 1-based |
| `Theta / deg.` | `theta` | deg | Goniometer axis; **must** match the `goniometerAxes` key in spice `exp_info` |
| `2Theta + Corr / deg.` | `ttheta` | deg | Detector axis, correction already applied in the file |
| `Crystal` | `crystal_v` | V | Reference-crystal diode reading |
| `Crystal Calib` | `crystal_calib` | photons | Calibrated reference intensity |
| `Rocking Curve` | `curve` | photons | 487-wide profile, `(index, 487)` — see §5.5 |

#### `tRSS` (`scans*.dat`) — 9 scalars + 1 profile

| Column (verbatim) | Variable | Unit | Notes |
|---|---|---|---|
| `Loop_Delay` | `loop_delay` | — | Loop counter over the delay axis |
| `Delay` | `delay` | ps | Pump-probe delay; the `groupby` key used by the recipes |
| `Loop_Theta` | `loop_theta` | — | Loop counter over the theta axis |
| `Theta` | `theta` | deg | Goniometer axis |
| `Crystal [V]` | `crystal_v` | V | Reference-crystal diode voltage |
| `Crystal [ph/s]` | `crystal_calib` | photons/s | Calibrated reference intensity |
| `Pilatus [ph/s]` | `cntrate` | photons/s | Detector count rate |
| `Repeats` | `repeats` | — | |
| `ImageNumber` | `image_number` | — | |
| `Intensities [ph/s]` | `curve` | photons/s | 487-wide profile, `(index, 487)` |
| *(derived)* | `ttheta` | deg | **Not a column.** Computed per §5.2 as `2*theta + 2 Theta Correction`, written at the same length as the rest |

#### `xRSS` (`allSteps*.dat`) — 7 scalars, no profile

| Column (verbatim) | Variable | Unit | Notes |
|---|---|---|---|
| `Position set` | `delay_setpoint` | ps | Commanded stage position; never NaN |
| `Position Real` | `delay` | ps | Stage readback; **may be NaN** |
| `Loop` | `loop` | — | |
| `Pilatus` | `counts` | counts | Raw detector counts — the trace used for time-zero |
| `Crystal` | `crystal_v` | V | |
| `Norm` | `norm` | — | Normalisation signal |
| `Crystal Calib` | `crystal_calib` | photons | |

#### Constants added to every scan type

The PXS is a copper plasma target, so the photon energy is fixed by Cu Kα and is recorded
nowhere in the scan folder — not in any `.dat` file and not in `parameters*.txt`. The
converter supplies it as a default:

| Variable | Value | Unit | Notes |
|---|---|---|---|
| `photon_energy` | `8048.0` | eV | Cu Kα₁. Constant for the PXS; always written |

- Exposed both as an `index`-aligned variable and as `k-header:photon_energy`, so it shows
  up in scan listings.
- Must be an **overridable argument** of the reader —
  `scans_from_pxs(..., photon_energy=8048.0)` — so a future source (different anode, or a
  monochromated beamline) is a parameter change rather than a code change. From the
  command line this is `-R pxs_import.reader:scans_from_pxs photon_energy=8048.0`;
  `pm` parses trailing `key=value` tokens of `-R` through `opcast`, so the value arrives
  as a float (`nx5d/pmtool/scan.py`, `_handle_subhelp_or_parse`).
- The name `photon_energy` is deliberate: it is what `XFEL23/data-prepare.py` maps the
  facility's `ENERGY` motor onto, so the two beamtimes agree.
- **This is not the value Q-mapping uses.** `QMapper` reads `beamEnergy` from the spice
  `exp_info` record, where all three PXS proposals already carry `8048.0`. The variable
  written here is provenance and scan-table metadata. If the two ever disagree, spice
  wins — and that disagreement is worth warning about at conversion time.

Notes on the above:

- **`Crystal` vs `Crystal Calib` in `sRSM` is inferred.** The `tRSS` header labels its
  units explicitly (`Crystal [V]`, `Crystal [ph/s]`); the `sRSM` header does not. The
  units given here assume the same pair by analogy. Worth confirming with whoever
  maintains the acquisition software before relying on the calibrated values; the names
  are correct either way.
- **`Position Real` can be NaN.** Across all 71 scans only 3 values of 4 573 are NaN
  (0.1 %), all in `xRSS`, and `Position set` is never NaN. Keep NaN as NaN rather than
  filling it — but note that any future `groupby('delay')` over an `xRSS` scan would need
  `delay_setpoint` instead.
- **Every other column is clean.** All 5 069 `sRSM` and 11 266 `tRSS` scalar values across
  the corpus parse as finite numbers; there are no sentinel values or blanks to handle.
- `Loop`/`Repeats`/`ImageNumber` are written as integers where they parse as such; the
  files store them in float notation (`1.000000`).

### 5.9 Relation to the XFEL beamtime's naming

`notebooks/XFEL23/data-prepare.py` does perform a variable renaming:

```python
pri_field = {
    'SSRY':              'omega',
    'PHI-RX':            'phi',
    'PPL_PHASE_SHIFTER': 'delay',
    'ENERGY':            'photon_energy',
}[rt['primary']]
```

It maps **facility motor names** onto **canonical nx5d variable names**, and feeds the
result to `k-primary:0`, the primary scan axis. §5.8 above is the same operation for the
PXS: acquisition-software column labels on the left, canonical names on the right.

The governing rule is *not* "use these four names everywhere". It is:

> A scan's variable names must equal the keys of that proposal's spice `exp_info`
> `goniometerAxes` / `detectorAxes`.

That is what lets a recipe find its angles at all. The names therefore differ per
instrument, because the goniometers differ:

| | goniometer axes | detector axis | beam energy |
|---|---|---|---|
| EuXFEL MID (2023) | `omega`, `chi`, `phi` | `twotheta` | 11 043 eV |
| UDKM PXS | `theta` | `ttheta` | 8 048 eV |

Verified: all three PXS proposals (`202601_PtAu`, `202602_FeAu`, `202608_DyCo5Pt`) record
`goniometerAxes: {theta: …}`, `detectorAxes: {ttheta: …}` and `beamEnergy: 8048.0` in every
`exp_info` record they contain. nx5d's own `ExperimentTemplate` for UDKM
(`nx5d/xrd/reader.py`) uses the same pair.

So `theta`/`ttheta` is correct here, and renaming to `omega`/`twotheta` for cosmetic
consistency with the XFEL beamtime would invalidate every existing PXS spice record for no
benefit. Where the two instruments *can* agree, this design follows the XFEL script:
`delay` for the pump-probe axis and `photon_energy` for the beam energy.

---

## 6. Target HDF5 layout

### 6.1 Decided: one file per scan by default, one file per proposal as an option

The analyst chose **one `.h5` per scan** as the default, laid out as:

```
202601_PtAu/
  scans/
    r0001.h5
    r0002.h5
    …
  spice/
```

opened as `file:///…/202601_PtAu/scans/{scan}.h5`.

Why this and not a single proposal-wide file: `nx5d/repo/universal.py` (`ProposalFactory`,
~lines 306-323) dispatches on where `{scan}` sits in the URL. In the **path** it yields
`FsProposal`; in the **fragment** (`…/PtAu.h5#{scan}`) it yields `AstorProposal`. Both
forms read and write through camagick ("astor"): `Scan.__init__` obtains its loader from
`camagick.storage.base.find_loader`, and `nx5d.tools.write_to_hdf5` is
`write_to_astor(kind='hdf5')`. camagick is therefore not a differentiator — it is a hard
nx5d dependency either way. The per-file form is chosen because it matches the group's
proven XFEL23 workflow, allows re-converting one scan without rewriting everything,
keeps glob filtering over scan files, and reaches an identical `prop.rNNNN` API.

The *contents* of a scan group are identical either way, so this decision is cheap to
revisit later — it becomes a URL change plus a merge step.

#### 6.1.1 Option: one `.h5` file per proposal

The single-file layout is a **supported, selectable alternative**, not a later retrofit:

```
202601_PtAu/
  202601_PtAu.h5        groups r0001, r0002, …
  spice/
```

opened as `file:///…/202601_PtAu/202601_PtAu.h5#{scan}`.

It needs no reader change. `write_to_astor` splits the target on `#`, opens the file
before it in append mode and creates the fragment as the scan group
(`nx5d/tools.py`, ~lines 135-146), so `pm scan import` with a fragment URL writes every
scan into the one file. nx5d opens it through `AstorProposal`, the same path the `kmc3`
bookmarks use in production for their one-container-per-proposal zarr stores.

The two invocations differ only in the URL and one flag:

```bash
# default: one file per scan
pm -u 'file:///…/202601_PtAu/scans/{scan}.h5' scan import <proposal> \
   -R pxs_import.reader:scans_from_pxs -W frame_guard=None

# option: one file per proposal
pm -u 'file:///…/202601_PtAu/202601_PtAu.h5#{scan}' scan import <proposal> \
   -R pxs_import.reader:scans_from_pxs -W hdf5 frame_guard=None
```

Rules for the single-file form:

- **`-W hdf5` is optional, not mandatory.** nx5d splits the target URL on `#` into
  `target_outer_inner` *before* substituting `{scan}`, so auto-detection sees the clean
  outer path `…/202601_PtAu.h5` and resolves `write_to_h5` on its own, regardless of the
  hint (§6.5). The hint is still passed explicitly in `--single-file` mode, for clarity
  and as a guard against a future nx5d version changing that splitting behaviour.
- **An existing target file counts as a non-empty output** for the §7 overwrite rule.
  The writer's default `overwrite_data=False` leaves existing datasets untouched, so a
  renumbered re-import onto an old file would silently keep stale groups beside new ones.
  The script must refuse unless forced, and when forced must delete the file first rather
  than write into it.
- **No concurrent imports** against the same file. Per-scan re-conversion still works,
  because groups are per scan, but each run opens the whole file for writing.
- Spice migration (§9), attributes (§6.2-§6.4) and the mapping table (§7) are identical
  in both layouts.

**Both local layouts are produced by the same reader.** The S3 target (§6.6) uses the
fragment form as well — a single zarr container addressed by `#{scan}` — because that is
what works on object storage. Changing only the write destination switches between all
three, which is exactly the cheapness this section anticipated.

### 6.2 Required attributes

From `nx5d/repo/universal.py` (~lines 71-212) and the specification comment in
`nx5d/xrd/kmc3.py` (`_kmc3_meta_infer`, ~lines 729-741):

| Attribute | Required | Meaning |
|---|---|---|
| `k-scan-type` | **yes** | Recipe dispatch key. One of `sRSM`, `tRSS`, `xRSS`. |
| `k-frame-guard` | **yes** | Path, relative to the scan node, of the dataset authoritative for frame count. |
| `k-header:<name>` | no | Scalar metadata surfaced in scan listings. |
| `k-primary:<N>` | no | Primary scan axes. Each entry must be either a `k-header:<name>` key **or a dataset path relative to the scan node** (e.g. `measurement/delay`). A bare variable name that is neither fails — see §6.2.1. |

`k-frame-guard` is not optional in practice: when it is absent, nx5d falls back to
`strategy='sane'` (`nx5d/repo/universal.py`, `_get_data`), which **raises** on any array
whose first dimension differs from the auto-detected index length
(`nx5d/xrd/base.py`, `_build_single`). A perfectly uniform scan might load by luck, but
the §8.1 `/measurement_full/` arrays never match the frame count, and the guard is what
makes `num_frames` in listings honest. Always write it.

### 6.2.1 Scan listing metadata — preserving the notes column

**Confirmed: the old behaviour is real, and it still exists on the filesystem path.**
`DatasetFactory.paramsof()` collects the unkeyed leading lines of `parameters*.txt` into a
`Comments` entry, and `DataScan._get_summary()` (`nx5d/xrd/reader.py`) surfaces it:

```python
def _get_summary(self):
    po = self._loader.paramsof(self._scan_path)
    return {
        'type': self._loader.typeof(self._scan_path),
        'description': po.get('Comments', ''),
        'steps': len(glob.glob(f'{self._scan_path}/*tif')),
    }
```

That is exactly the listing in the reference screenshot: `steps` is the TIFF count (1217,
882, 18, 2618, … — these match the measured counts for those scans), and the notes column
is the comment block. The column was later renamed `description`, and `integration` was
dropped; the mechanism is unchanged.

**The trap when moving to HDF5.** In the `universal.py` path — the one this design
targets — `description` is **reserved for error reporting**:

```python
summary = {
    'type':        self._get_scan_type(scan_node),
    'num_frames':  self._get_num_frames(scan_node),
    'scan_dims':   scan_dims,
    'description': err          # <- "Bad header spec: …", not scan notes
}
summary.update(header)
```
— `nx5d/repo/universal.py`, `_get_summary`

So writing the comments into anything called `description` would either be overwritten or
would masquerade as an error. Everything else in the summary comes from `header`, i.e.
from `k-header:<name>` attributes (`_get_header_items` strips the 9-character prefix).

**Therefore the converter must write the notes as a `k-header:` attribute.** Required set,
on every scan:

| Attribute | Source | Notes |
|---|---|---|
| `k-header:notes` | `Comments` from `parameters*.txt` | Empty string for `sRSM` — correct, not a failure (§5.4) |
| `k-header:integration` | `Exposure Time` | Present in all 71 scans; fills the column that was blank in the old listing |
| `k-header:photon_energy` | constant `8048.0` | §5.8 |
| `k-header:date_time` | `Date Time`, re-formatted to ISO 8601 | The source string is day-first (`08.01.2026 12:08:21`) and does **not** sort as text. It is display metadata only; the chronological sort key is the folder path (§7). |
| `k-header:roi` | `ROI` | |

`steps` needs no equivalent: the HDF5 path derives `num_frames` from `k-frame-guard`
automatically, and per §5.7 that number is the *referenced* frame count, which is more
truthful than the old `glob('*tif')` — for `202602_FeAu/20260203/120401` the old listing
would have said 2 115 where only 1 364 frames belong to the scan.

Set `k-primary:0` to the scan's primary axis so the listing's `primary` and `pri. values`
columns populate as they do for the KMC3 and XFEL repositories — **but as a dataset path,
not a bare name**:

| Type | `k-primary:0` |
|---|---|
| `sRSM` | `instrument/motors/theta` |
| `tRSS` | `measurement/delay` |
| `xRSS` | `measurement/delay_setpoint` |

Why: `_get_summary` resolves each primary first against `k-header:` keys and otherwise as
`scan_node[name]`, a child lookup relative to the scan node (`nx5d/repo/universal.py`,
`_dims_from_primary_spec`). A bare `delay` is neither, so the lookup raises, `scan_dims`
is emptied and the listing's description column shows `Bad header spec: k-primary:0 =
"delay"`. The data-loading path then calls `mangle_s2d('delay')`, which translates a
*source path* into a variable name and also fails. The XFEL23 script gets away with a
bare `omega` only because it also writes `k-header:omega`; the path form avoids that
coupling and is what `mangle_s2d` wants.

### 6.3 Dataset naming

`nx5d/xrd/base.py` (`DatasetFactory._discover_candidates`, ~lines 365-410) walks
all subgroups and names each xarray variable after the dataset's **leaf basename**,
discarding the path. Dimension 0 becomes `index` when its length matches the frame count;
trailing dimensions are auto-named (`<tag>_1`, `<tag>_2`).

**Leaf basenames must be unique within a scan.** When the same leaf appears under two
groups, `_discover_instr` (~lines 413-440) renames **every** copy with a path-derived
suffix and keeps none under the plain name. Two datasets both called `counts` would
surface as `counts_measurement` and `counts_measurement_`, and `run.raw.counts` would
not exist. This constrains §8.1.

Therefore write:

```
r0008.h5
  /instrument/pilatus/images     (N, H, W)   frame stack
  /instrument/pilatus/curve      (N, 487)    trailing profile from the .dat
  /instrument/motors/theta       (N,)
  /instrument/motors/ttheta      (N,)        read for sRSM, derived for tRSS
  /measurement/delay             (N,)        tRSS and xRSS only
  /measurement/loop              (N,)
  /measurement/crystal_v         (N,)
  /measurement/crystal_calib     (N,)
  /measurement/cntrate           (N,)        tRSS
  /measurement/counts            (N,)        xRSS
  /instrument/source/photon_energy (N,)      constant 8048.0 eV, all types
  …every remaining column of §5.8, each (N,)
  /measurement/frame_missing     (N,)        bool, §5.7
  attrs:
    k-scan-type   = 'tRSS'
    k-frame-guard = 'instrument/pilatus/images'
    k-primary:0   = 'measurement/delay'
```

giving `run.raw.images`, `run.raw.theta`, `run.raw.ttheta`, `run.raw.delay` — which is
what the existing notebook cells and the spice `exp_info`
(`goniometerAxes: {theta: 'y-'}`, `detectorAxes: {ttheta: 'y-'}`) already expect.

Verified compatible with `xraydc`: `QMapper._select_image_name`
(`xraydc/signal.py:894`) selects the detector variable **by shape**, not by dimension
name, so nx5d's auto-named trailing dimensions are harmless.

### 6.4 Provenance attributes

Every scan file additionally carries, as attributes on the scan node:

| Attribute | Example |
|---|---|
| `udkm-source` | `20260108/120821` |
| `udkm-date` | `20260108` |
| `udkm-time` | `120821` |
| `udkm-datetime` | ISO 8601, parsed from the `Date Time:` line |

These are the mitigation for §7. They are not consumed by nx5d; they exist so that any
scan can be traced to its source folder and to the e-lab notebook regardless of what
number it currently holds.

### 6.5 Delivery: a reader module driven by `pm scan import`

**No bespoke CLI is needed.** nx5d ships a generic ingest tool, `pm`
(`nx5d.pmtool.pm:main`, registered under `[project.scripts]`). Its `scan import`
subcommand takes a source, a **reader** and a **writer**, and the reader resolver supports
external modules by `<module>:<function>`:

```python
if ':' in stype:
    import importlib
    base, func = stype.split(':')
    modbase_obj = importlib.import_module(base)
    return getattr(modbase_obj, func)
```
— `nx5d/pmmodel/scan.py`, `PmScanImport._get_reader`

So a reader living in `xraydc` is a first-class citizen with **zero nx5d modifications**:

```bash
pm -u 'file:///path/to/202601_PtAu/scans/{scan}.h5' scan import /path/to/202601_PtAu \
   -R pxs_import.reader:scans_from_pxs -W frame_guard=None
```

(`-u`, like every other global `pm` option, must precede the `scan import` subcommand —
it is parsed by a plain parent `argparse.ArgumentParser`, not a subparser that would
accept it afterwards; `-R`/`-W` are `scan import`'s own options and follow it.)

This replaces the `udkm-from` CLI proposed in the earlier draft. We write one function;
nx5d supplies argument parsing, scan globbing, dry-run (`-d`), progress (`-P`), and all
writers.

**Reader contract.** `scans_from_<type>(source, **kwargs)` is a generator yielding
`(kbase, h5like)` pairs:

- `kbase` — a dict of keys, which must contain `'scan'`: the target scan key, `'r0001'`.
- `h5like` — an h5py-like group (`silx.io.commonh5`) exposing `.attrs`, subgroups and
  datasets, i.e. exactly the layout of §6.3.

`scans_from_tiff` (`nx5d/pmmodel/scan.py:179`) is the closest template. Note that it calls
`gobj._unload_data()` after each yield to avoid holding every frame in memory — essential
here, where a single scan reaches 2 618 frames.

- **Every `pm scan import` invocation must pass `-W frame_guard=None`.** `write_to_astor`'s
  default, `frame_guard='auto'`, reads the `k-frame-guard` attribute off the *target*
  group to decide whether to store frames — but on first write the target is still empty,
  so it finds nothing and refuses with `RefuseFrameStorage`, leaving an empty file behind.
  Passing `frame_guard=None` disables that check on write; nx5d's reading side is
  unaffected, since it still gets `k-frame-guard` copied from the source scan's own
  attributes (§6.2).
- In nx5d 3.2.1, `prop.ls()` returns a dict of columns — `key`, `type`, `num_frames`,
  `primary`, `pri. values`, `secondaries`, `others`, `description` — rather than a list
  of per-scan records, so e.g. `prop.ls()['key']` lists every scan key.
- The `_full`-suffixed arrays of §8.1 (the untruncated `xRSS` columns) surface in
  `prop.rNNNN.raw` as **coordinates**, not ordinary data variables — because their length
  differs from the truncated stack's `index` dimension, xarray gives each its own
  dimension (`counts_full`, etc.) rather than sharing `index`. They are still reachable
  as `raw.counts_full`.

**Writers already exist:** `nx5d.tools.write_to_hdf5` / `write_to_zarr` /
`write_to_astor`, selected from the target URL's extension. We write none of them.
Auto-detection takes the text after the URL's last `.`, so `…/{scan}.h5` resolves to
`write_to_h5` on its own. A fragment URL such as `…/{proposal}.zarr#{scan}` works the same
way: nx5d splits the target URL on `#` into `target_outer_inner` before substituting
`{scan}`, so auto-detection sees the clean `…/{proposal}.zarr` outer path and resolves
`write_to_zarr` without help. `-W zarr` is therefore optional, not mandatory, for fragment
targets — pass it explicitly as the first `-W` token anyway, for clarity (§6.6, §11.14).

**The reader must stay writer-agnostic.** This is what makes the S3 target (§6.6) a
later, free addition rather than a retrofit. The reader yields `(kbase, h5like)` and must
never reference HDF5, zarr, compression, chunking or any output concern — those are
writer arguments supplied through `-W` at the command line. Concretely: no `h5py` import,
no hardcoded `compression='lzf'` (which exists in HDF5 and not in zarr), no assumptions
about the target being a local path. Honour this and switching targets is a different
`-u` URL on the same command; break it and S3 becomes a second implementation.

### 6.6 Remote target: S3 via zarr

S3 publication works and nx5d already does it in production — but **as zarr, not HDF5**.
The built-in `kmc3pub` bookmark (`nx5d/xrd/kmc3.py`) defines:

```python
kmc3_s3_public_urls = {
    'url':       's3://kmc3pub-data/{proposal}/{proposal}.zarr#{scan}',
    'spice_url': 's3://kmc3pub-spice/{proposal}/spice'
}
```

Note the shape: **one zarr store per proposal**, scans as groups inside it, addressed by
the `#{scan}` fragment — the `AstorProposal` path from §6.1. That is the correct choice on
object storage: thousands of small HDF5 files behave badly over S3, and HDF5 over object
storage is awkward, whereas zarr is built for it.

Consequences:

- Remote publication additionally needs `s3fs` and working S3 credentials. camagick
  (astor) is used by both targets (§6.1); the two differ only in the write destination
  — `-W zarr` for the fragment-form URL is optional, not needed, since auto-detection
  already resolves it (§6.5).
- Scan group contents are unchanged, so §6.2-§6.4 apply verbatim to both.
- Spice goes to a **separate bucket or prefix**, not inside the zarr store.
- Credentials and endpoint come from a repository *bookmark*: `internal_repositories` in
  `nx5d/pmmodel/base.py` holds an `environment` dict (AWS key, secret, endpoint URL,
  region) next to the URLs, selected with `pm -r <bookmark>`. The existing `kmc3pub`
  entry points at the Uni Potsdam RZ S3 endpoint, so the group plausibly already has
  infrastructure and accounts there.

**Open item for the analyst, not the builder.** `kmc3pub` is a *read-only public* bookmark
whose credentials are committed to nx5d's source. Publishing a UDKM proposal needs a
**writable** bucket with its own credentials, which must not be committed anywhere. Two
options: (a) pass `-u` URLs explicitly and supply `AWS_*` from the environment, needing no
nx5d change; or (b) add a bookmark that reads credentials from env vars, which does.
Option (a) is the recommended start.

**Sequencing:** get the local HDF5 target working and the notebook running against it
first; add S3 afterwards as a second `pm scan import` invocation using the same reader.
Nothing in the reader changes between them.

### 6.7 Keeping the upstream move cheap

The code lands in `xraydc` now and should move into `nx5d` later, after discussion with
that project's maintainer. The built-in resolver does:

```python
obj = getattr(this_module, f'scans_from_{stype}')
```

So **if the function is named `scans_from_pxs`** and holds to the §6.5 contract,
upstreaming is: move the function into `nx5d/pmmodel/scan.py`, after which
`-R pxs_import.reader:scans_from_pxs` becomes simply `-R udkm`. Nothing else changes — not
the writers, not the URLs, not the spice, not the notebooks.

To keep that true while it lives in `xraydc`:

- Keep `scans_from_pxs` free of `xraydc` imports. It is an I/O function and must not
  reach into `QMapper` or the recipe layer. The dependency arrow stays one-way.
- Give it its own module, `pxs_import/reader.py`, with no import-time side effects.
- Depend only on what nx5d already depends on: `numpy`, `tifffile`, and `silx` (for the
  h5like objects).

**Cost to flag:** this adds `tifffile` and `silx` to `xraydc`, whose dependency list is
deliberately lean (`xarray`, `scipy`, `dask`, `xrayutilities`, `tqdm`) and whose README
advertises standing alone. Make them an **optional extra** — `pip install xraydc[udkm]` —
so a plain `pip install xraydc` is unaffected. `silx` in particular is a heavy transitive
dependency to impose on users who only want `QMapper`.

Note the reverse direction is *not* free: `-R pxs_import.reader:…` imports the `xraydc`
package, and `xraydc/__init__.py` eagerly imports `signal` and `cooking`, hence
`xrayutilities`, `xarray`, `dask` and `tqdm`. The reader does not use any of them, but
they load. That is acceptable (nx5d already depends on `xarray` and `dask`); it just
means "no import-time side effects" holds for the module itself, not for the package it
sits in.

### 6.8 Per-proposal preparation script

Follow the existing convention from the EuXFEL 2023 beamtime: each proposal carries its
own small preparation script at

```
notebooks/<proposal>/data-prepare.py
```

named and placed exactly as `notebooks/XFEL23/data-prepare.py` is, so that anyone who has
seen one beamtime's layout recognises the next one's.

Division of labour, mirroring that script:

- **The script holds what is proposal-specific**: source and target paths, the S3 bucket
  if used, and any per-beamtime quirks. It is read top-to-bottom as a record of how that
  proposal was prepared.
- **The script exposes the layout choice of §6.1 as a switch**, `--single-file`
  (default off). It only changes what the script passes to `pm scan import`:

  | | `-u` | `-W` |
  |---|---|---|
  | default | `file:///…/<proposal>/scans/{scan}.h5` | *(auto-detected)* |
  | `--single-file` | `file:///…/<proposal>/<proposal>.h5#{scan}` | `hdf5` |

  The script also prints the matching `Proposal(url=…)` line at the end of a run, so
  the notebook opens whichever layout was actually written. The reader and the spice
  migration are unaware of the switch.
- **The reusable logic stays in `pxs_import.reader`** and is invoked from it. The XFEL script
  could afford to be entirely self-contained because its `.h5` files already existed and
  it only stamped attributes onto them; ours has to build the files, which is too much
  logic to copy per proposal.

**Shared attribute vocabulary.** The `k-*` attributes this design writes (§6.2) are the
same ones the XFEL script sets by hand —

```python
h5.attrs['k-frame-guard'] = 'is_pumped'
h5.attrs['k-scan-type']   = stype
h5.attrs['k-header:intg'] = rt['time']
h5.attrs['k-primary:0']   = pri_field
```

— so the two beamtimes produce scans that are introspectable in the same way.

**One deliberate divergence:** `XFEL23/data-prepare.py` keys runs as `f'r{rid:03d}'`
(three digits), because it inherited facility run numbers. This design uses **four
digits**, `r{N:04d}`, matching nx5d's own `kmc3` convention
(`scan_format='r{iscan2:04d}'`). Padding width must stay fixed within a proposal for the
lexicographic reasons in §7, but it does not need to match across beamtimes, and the
analyst has chosen four here.

---

## 7. Scan numbering

**Decided: pure chronological renumbering.** On every run, the converter collects all
matching `YYYYMMDD/HHMMSS/` folders, sorts by the `YYYYMMDD` + `HHMMSS` string (which
sorts correctly as plain text), and numbers them `r0001 … rNNNN` from scratch. No manifest
is persisted.

The analyst chose this over a persisted manifest, accepting the trade-off below.

**Known consequence, documented deliberately:** adding a folder that sorts *earlier* than
existing ones — a forgotten date folder, or data recovered later — shifts every subsequent
number. Notebook references and spice anchors pinned to old numbers then point at
different scans, silently.

Required mitigations, which make this recoverable rather than dangerous:

1. The provenance attributes of §6.4 on every scan.
2. The converter **prints the full mapping table** (`r0001 ← 20260108/100331 (xRSS)`, …) on
   every run and writes it next to the output as a plain-text or CSV listing. This is a
   report, not an input — it is never read back.
3. The converter **refuses to overwrite** a non-empty output unless explicitly forced, so
   a renumbering cannot quietly half-apply over an older set. "Non-empty output" means a
   `scans/` directory containing any `.h5`, or, with `--single-file`, an existing
   `<proposal>.h5` (§6.1.1). When forced, remove the old output first; never write into
   it, because the writer's `overwrite_data=False` default keeps stale datasets.

**Zero-padding is not cosmetic.** Scan keys must be `r0001`-style for two independent
reasons:

- `sanitize_handle` (`nx5d/repo/base.py` ~line 90) prefixes any handle starting with a
  digit, so a bare integer key `1` becomes the attribute `prop._1`.
- spice anchors are resolved by **plain string comparison** over a reverse-sorted list
  (`nx5d/spice.py` ~lines 526-531). Unpadded keys sort wrongly (`'10' < '2'`), which
  would mis-resolve `exp_info` and `offsets` without any error.

---

## 8. Handling un-cookable `xRSS` scans

`xRSS` scans must be converted and loadable — the analyst uses them for time-zero
determination from counts — but must not be Q-mapped.

Convert them normally (delay, counts, frame stack, no angles), set
`k-scan-type = 'xRSS'`, and leave the handling of the recipe mismatch to §9: the migrated
spice `recipes` record should **not** map `xRSS` to a Q-mapping recipe. Currently it maps
to `TRSS_2d_fix`, which is wrong.

What nx5d does is known (`nx5d/repo/base.py`, `DataScanBase._cook`, ~lines 701-717):
if `recipes[self.type]` is missing it falls back to `recipes['__default__']`, and if that
is missing too it raises `RuntimeError('xRSS: no recipe in [...] and no __default__
mentioned')`. If the value is `null`/`None`, however, `_cook` substitutes a no-op and
`.cooked` **silently returns `None`**.

Decision: **remove the `xRSS` key** from the migrated `recipes` record and do not add a
`__default__`. Never map `xRSS` to `null`. This yields the loud failure required by §11.6.

### 8.1 Truncated `xRSS` scans

Per §5.7, three of thirteen `xRSS` scans have fewer frames on disk than table rows,
because the run was interrupted. The converter must handle this explicitly rather than
NaN-padding to the table length:

- Let `N_rows` be the number of data rows and `N_img` the count of contiguous
  `image{i:05d}.tif` present from index 0.
- Write the frame stack with **`N_img`** frames, not `N_rows`.
- **Trim every §5.8 variable to the first `N_img` rows**, so the whole scan group shares
  one `index` dimension. The rule in §5.8 — scalar columns have exactly the same length as
  the frame stack — holds for truncated scans too, and takes precedence.
- Set `k-frame-guard` to the image stack, so nx5d's frame count is honest.
- **Do not discard the remaining rows.** Write the complete, untruncated table to a
  separate sibling group, `/measurement_full/`, which is *not* frame-guarded and whose
  arrays are length `N_rows`. These scans exist to give a counts trace for time-zero
  fitting, and for `202602_FeAu/20260213/125733` that trace is 1 729 points long while
  only 489 frames survive — truncating it to 489 would throw away the useful part of the
  measurement.
- **Give the full-table datasets distinct leaf names**, `<column>_full`:
  `/measurement_full/counts_full`, `/measurement_full/delay_full`, … . Per §6.3, nx5d
  names variables by leaf basename and renames *all* duplicates, so a second `counts`
  under `/measurement_full/` would make `run.raw.counts` disappear from the trimmed group
  too and break the `k-primary:0` lookup. With the suffix, `.raw` carries `counts` (489,
  `index`) alongside `counts_full` (1 729, own dimension `counts_full`, which is what
  `strategy='loose'` assigns to non-index arrays).
- Record `udkm-truncated = True`, `udkm-rows = N_rows`, `udkm-frames = N_img` as
  attributes, and warn at conversion time.

This arrangement keeps `.raw` clean — one `index`, everything aligned, nothing ragged —
while preserving the full trace for the analysis that needs it. It also removes any
dependence on how `nx5d/xrd/base.py` `_build_single` handles arrays of mismatched length,
since within each group they now never mismatch.

---

## 9. Spice migration

The proposal's existing spice records live in `202601_PtAu/spice/` as one file per
record, with handles `exp_info`, `offsets`, `qargs`, `recipes`. **The files are named by
UUID and carry no extension** (e.g. `spice/0d0d7734-d62b-4cf9-8090-08ccc114251d`); their
*content* is JSON. A migration that filters on `*.json` processes nothing. Select entries
with `os.path.isfile()` and parse each as JSON, skipping anything that fails to parse.
Several records are anchored to date/time scan keys:

```json
{"handle": "exp_info", "anchor": "20260108/143930", "revision": 5, "data": {…}}
```

Anchors resolve by string comparison against the current scan key, selecting the most
recent record at or before that point. Once scans are renumbered, every non-empty anchor
must be rewritten to the corresponding `rNNNN` key, using the same chronological mapping
the converter computes.

Records with an empty anchor (`""`) are proposal-wide defaults and need no change.

This is a one-off migration. It may be a mode of the converter or a separate small script;
that is the builder's call. It must not be a manual edit.

### 9.1 What the migration actually faces

Surveyed across the three proposals:

| Proposal | Records | Date/time-anchored | Migration |
|---|---|---|---|
| `202601_PtAu` | 15 | 11 (`20260108/115550` ×4, `/120821` ×2, `/143930` ×4, `/145450` ×1) | required |
| `202602_FeAu` | 9 | 5 (all `20260202/153026`) | required |
| `202608_DyCo5Pt` | 4 | 0 — every anchor is `""` | no-op |

Every anchor above exists as a scan folder, so each maps 1:1 onto an `rNNNN` key. (Were an
anchor ever *not* a folder, the correct target is the first scan sorting at or after it,
since anchors apply to scans ≥ anchor.) Multiple revisions share an anchor; rewrite the
anchor only, never the revision.

So the migration must run, but it is small, and one proposal needs nothing. A proposal
with no anchored records must be detected and left alone rather than rewritten.

**Robustness requirement.** All three `spice/` folders contain a `.ipynb_checkpoints`
**directory** alongside the records, which raises on a naive `open()` of every entry in
the folder. The migration must skip non-file and non-JSON entries rather than abort.
Expect the same debris in any proposal folder that has been opened in Jupyter.

---

## 10. Implementation notes and prior art

- **`scans_from_tiff` is the template to copy** (`nx5d/pmmodel/scan.py:179`). It is a
  reader of exactly the kind we are writing: it wraps `TiffSeriesH5`, yields
  `(kbase, h5like)` pairs with `scan_format='r{iscan2:04d}'` and
  `target_fmt='{scan}/instrument/{instrument}/data'`, and unloads each group's frames
  after yielding. Mirror its structure, its `rNNNN` formatting and its memory discipline.
  `_kmc3_from_tiff` (`nx5d/xrd/kmc3.py:353`) is the same logic wrapped in the older
  `kmc3-from` CLI, and is now only of historical interest — `pm scan import` supersedes it
  for our purposes.

  **But do not adopt the frame discovery of either.** Both select frames by globbing
  (`scan_glob="*/*/*.tif"`). Per §5.7 that is wrong for UDKM data, where 753 unreferenced
  TIFFs sit alongside the real ones. For `sRSM`/`tRSS` the frame list must come from the
  `.dat` file's `ImageName` column; for `xRSS` it is the contiguous index run.

  **Do not reuse `TiffSeriesH5` / `FileSeriesDataset` for the stack either.** Although
  `FileSeriesDataset.add_frame(index, file)` accepts an explicit list, its
  `_load_data()` (`nx5d/h5like/fileseries.py`, ~lines 33-50) fills a missing index by
  **duplicating the next existing frame** and grows the array with `np.append` beyond its
  preallocated length. That contradicts the NaN-fill of §5.7 and would mis-shape the
  stack. Build the stack directly with `numpy` + `tifffile` and wrap it in a plain
  `silx.io.commonh5` `Dataset`; keep `_unload_data()`-style release of the array after
  each yield.
- `nx5d.h5like.fileseries` imports from `silx.io.commonh5`; silx is already an nx5d
  dependency, so the h5like objects cost nothing extra wherever nx5d is installed.
- **Reuse, do not re-derive, the `.dat` parsing.** `nx5d/xrd/reader.py` already has working
  logic for the indicator/data line structure, the per-point metadata lines, the
  encoding-tolerant parameters reader, and missing-frame handling (it substitutes a
  NaN-filled array of the expected shape and warns). Lift it, then fix §5.2 and §5.5.
- `opcast` (`nx5d/tools.py`) is an 8-line `asteval`-backed opportunistic string→number
  cast. It drags `zarr` and `camagick` into the import chain via `nx5d/tools.py`'s
  module-level imports. The reader must not import it anyway (§6.7: no `nx5d` imports);
  use a local `float()`-with-fallback for the purely numeric `.dat` fields.
- **nx5d must be installed, but not forked or modified.** `pm` is an nx5d entry point, so
  a current nx5d is a *development and runtime* prerequisite even though no nx5d source
  changes. At time of writing the local checkout at `../nx5d` is on `master` at
  `99c2ff6`, clean and level with `origin/master`. There is no `develop` branch — v3 is
  already merged to master, so "latest develop" means master. `pm scan import`'s
  external-reader support (`PmScanImport._get_reader`, `nx5d/pmmodel/scan.py:352-356`)
  is the load-bearing feature and is present in that revision.

---

## 11. Acceptance

The work is done when, from a clean checkout of `xraydc` with `[udkm]` extras, an
unmodified nx5d install, and the unmodified proposal folder:

1. `pm -u 'file://…/scans/{scan}.h5' scan import <proposal-path> -R pxs_import.reader:scans_from_pxs -W frame_guard=None`
   produces `scans/r0001.h5 … rNNNN.h5` plus a printed and written mapping table, with
   **no modification to nx5d**.
2. Scan numbering is chronological across *all* date folders in the proposal, not just one.
3. `Proposal(url='file://…/scans/{scan}.h5', spice='file://…/spice/')` lists all scans.
4. For a `tRSS` scan, `prop.rNNNN.raw` contains **every** variable listed in §5.8 —
   `loop_delay`, `delay`, `loop_theta`, `theta`, `crystal_v`, `crystal_calib`, `cntrate`,
   `repeats`, `image_number`, `curve`, `images`, plus derived `ttheta` — with
   `ttheta == 2*theta + correction`, no `ACHTUNG` warning, and **all of them the same
   length as the frame stack along `index`**.
5. For an `sRSM` scan, `prop.rNNNN.cooked` returns a Q-mapped Dataset with `qx`, `qy`, `qz`.
6. An `xRSS` scan loads as `.raw` and fails *loudly* if cooked.
7. The migrated spice records resolve: the `exp_info` that previously applied to
   `20260108/143930` applies to the scan that folder became.

Plus these, which come directly from the corpus validation and should be the regression
tests:

8. **All three surveyed proposals convert end to end** — `202601_PtAu` (15 scans, 1 date),
   `202602_FeAu` (38 scans, 5 dates), `202608_DyCo5Pt` (18 scans, 2 dates) — with no
   unclassified scans.
9. `202602_FeAu/20260203/120401` converts to a scan with **1 364 frames, not 2 115**: the
   751 unreferenced TIFFs are excluded.
10. `202602_FeAu/20260212/180247` converts with all 172 rows present and its 7 missing
    frames NaN-filled in a `float32` stack, `frame_missing` true at exactly those 7
    indices, with a warning naming them.
11. `202602_FeAu/20260213/125733` converts without allocating a 1 729-frame array:
    489 frames written, every `index`-aligned variable trimmed to 489, the full 1 729-row
    table preserved under `/measurement_full/` with `_full`-suffixed names, and
    `udkm-truncated` set. `prop.rNNNN.raw` still exposes plain `counts` (length 489) and
    `counts_full` (length 1 729).
11a. For an `sRSM` scan, `prop.rNNNN.raw` contains `loop`, `theta`, `ttheta`, `crystal_v`,
    `crystal_calib`, `curve`, `images`; for an `xRSS` scan, `delay_setpoint`, `delay`,
    `loop`, `counts`, `crystal_v`, `norm`, `crystal_calib`, `images`. No column from
    §5.8 is silently dropped in any scan type.
12. `202608_DyCo5Pt` migrates with **zero** spice records rewritten, and the
    `.ipynb_checkpoints` directory in its `spice/` folder does not raise.
13. `pip install xraydc` (without the `[udkm]` extra) still imports cleanly and does not
    pull `silx` or `tifffile`.
13a. **The proposal listing shows the notes.** `prop.ls()` renders a `notes` column
    carrying the `parameters*.txt` comment block — `"16nm Pt on MgO 157mW 930x880µm²
    10.2mJ/cm²"` for `20260108/120821`, `"MV217 T =300K search time zero 15mJ/cm² 800nm"`
    for `20260108/101313` — with an `integration` column populated from `Exposure Time`
    (trailing `s` stripped), and empty notes on `sRSM` scans. U+00B2 `²` and U+00B5 `µ`
    survive the Windows-1252 fallback intact.
13b. `202608_DyCo5Pt` converts with an **entirely empty notes column** and no warnings —
    it genuinely has no comments in any of its 18 scans (§5.4). Use `202601_PtAu` or
    `202602_FeAu` for any test that needs non-empty notes.
13c. **The listing shows no `Bad header spec` text** in the description column for any
    scan, and `scan_dims` is non-empty, i.e. every `k-primary:0` resolved (§6.2.1).
13d. **`--single-file` produces an equivalent proposal.** Running the preparation script
    with `--single-file` on `202601_PtAu` yields one `202601_PtAu.h5` with 15 groups;
    `Proposal(url='file:///…/202601_PtAu.h5#{scan}', spice=…)` lists the same 15 scans
    with the same `type`, `num_frames` and `notes` as the per-scan layout, and
    `prop.r0008.raw` is dataset-equal between the two. Re-running without `--force`
    against the existing file is refused.
14. **S3, once attempted:** the same reader, pointed at
    `s3://<bucket>/{proposal}/{proposal}.zarr#{scan}`, optionally with `-W zarr`, produces
    a store that `Proposal(url=…)` can list and read remotely, with spice served from its
    own prefix. Deferred until criteria 1-13 pass locally.

## 12. Left to the builder

- Whether the spice migration (§9) is a small standalone script in `xraydc` or a utility
  function invoked from a notebook. It is not part of the `pm scan import` flow either
  way, since `pm` has no spice-rewriting subcommand.
- Chunking and compression for the frame stacks. These are writer concerns passed via
  `-W` (§6.5), not reader code. Scans here run to 2618 frames; `_kmc3_from_tiff` uses
  `compression='lzf'` as precedent. The stack dtype is decided: `float32` (§5.7).
- Group placement of the columns not named in §6.3 (all of them are imported, §5.8).
- The precise set of `k-header:` entries worth surfacing per scan type beyond the
  required five of §6.2.1.
- Error behaviour for a `^\d{6}$` directory that contains none of the three signature
  `.dat` files (skip with a warning is the expected default; non-directory entries and
  non-matching names such as `115358.zip` or `135936Graphics` are skipped silently, §5).
