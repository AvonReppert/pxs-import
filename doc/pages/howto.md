# How to import a UDKM PXS proposal into nx5d

This walks through converting one UDKM plasma-X-ray-source proposal
(the `YYYYMMDD/HHMMSS/` scan folders) into nx5d-native HDF5 scans, using
`202601_PtAu` as the example. The same steps apply to any PXS proposal.

What the importer does, in order:

1. Finds every scan folder, classifies it (`sRSM`, `tRSS`, `xRSS`) and numbers
   all scans chronologically across all date folders: `r0001`, `r0002`, …
2. Refuses to run if an earlier import output exists (unless `--force`).
3. Writes `scan-mapping.csv` (run number → source folder → type → source root)
   and prints the same table. The fourth column says which acquisition
   folder a scan came from, which the `YYYYMMDD/HHMMSS` identity does not.
4. Runs nx5d's `pm scan import` with the `pxs_import.reader:scans_from_pxs`
   reader, writing one
   `scans/rNNNN.h5` per scan (or, with `--single-file`, one `.h5` per proposal).
5. Backs up the proposal's `spice/` folder to `spice.pre-migration/`, rewrites
   the date/time anchors of the spice records to the new run numbers, and
   removes the `xRSS` entry from the `recipes` record so those scans cannot be
   Q-mapped by accident.

The frame stack is taken from the `.dat` file's `ImageName` column, never from
globbing the TIFFs; missing frames become NaN and are flagged in
`frame_missing`; truncated `xRSS` runs keep the full counts trace under
`counts_full` and friends.

---

## A. From a local install

`pip install pxs-import` puts the `pxs-prepare` command on the PATH; the
examples below are PowerShell. Replace `D:\proposals\202601_PtAu` with the
proposal folder you want to convert.

### 1. Rehearse

```powershell
pxs-prepare D:\proposals\202601_PtAu --dry-run
```

Expected: a 15-line mapping table starting with
`r0001 <- 20260108/100331 (xRSS)`, nx5d's `Dry run -- no changes will be made.`
notice, and a final `Proposal(url=…, spice=…)` line. The only file written is
`D:\proposals\202601_PtAu\scan-mapping.csv`. Nothing is removed and the spice
folder is not touched, even if you add `--force`.

### 2. Import

```powershell
pxs-prepare D:\proposals\202601_PtAu
```

This reads about 2.2 GB of TIFFs and takes a few minutes. Afterwards:

```
D:\proposals\202601_PtAu\
  scans\r0001.h5 … r0015.h5
  scan-mapping.csv
  spice\                   anchors rewritten, xRSS recipe removed
  spice.pre-migration\     byte-for-byte copy of spice\ from before the run
```

### 3. Variants

```powershell
# one .h5 for the whole proposal, scans as groups r0001, r0002, …
pxs-prepare D:\proposals\202601_PtAu --single-file

# re-run after a previous import: removes the old scans\ (or the single .h5) first
pxs-prepare D:\proposals\202601_PtAu --force

# write the HDF5 somewhere else and migrate a copy of the spice folder
pxs-prepare D:\proposals\202601_PtAu --output-root D:\converted\202601_PtAu --spice-dir D:\converted\202601_PtAu\spice

# a different photon energy (eV) for the provenance variable
pxs-prepare D:\proposals\202601_PtAu --photon-energy 8041.0
```

`--help` lists everything.

### 3b. Assembling a proposal from the acquisition folders

A beamtime is not stored in one place. Four acquisition programs write to four
sibling folders, and which one a scan lands in follows from the program it was
started with — which also decides its scan type:

| folder | scan types |
|---|---|
| `PilatusTheta2Theta` | `sRSM`, `tRSS` |
| `PilatusPumpProbe` | `xRSS` |
| `PilatusScanGoniometer` | `alignment` |
| `PilatusScanZaber` | `alignment` |

They all sit under one base path, which differs per machine and which
`pxs-prepare` finds by itself — the UNC share on a lab laptop,
`/home/jovyan/data/XRayLab-Pilatus` on the JupyterHub, `P:\` on the
measurement PC. So on any of those three, nothing has to be typed but the
dates:

```powershell
# every scan of that beamtime, from all four programs, in one numbering
pxs-prepare D:\proposals\202504_NbO2 --from 2025-04-08 --to 2025-04-25

# --to defaults to today, which is what you want while still measuring
pxs-prepare D:\proposals\202504_NbO2 --from 2025-04-08
```

Because those folders hold every beamtime since 2011, `--from` is **required**
on this route; without it the command refuses rather than importing fifteen
years.

The proposal folder here holds no raw data — only `spice\`,
`scan-mapping.csv` and the output. It is created if it does not exist, and
nothing is ever written into a source folder.

On a machine that is none of the three, name the base path once:

```powershell
pxs-prepare D:\proposals\202504_NbO2 --base \\server\share\XRayLab-Pilatus --from 2025-04-08
```

or set `UDKM_BASE` in the environment and keep the call bare.

**Reading from specific folders instead.** `--source` is repeatable and takes
a folder that holds `YYYYMMDD\` days directly:

```powershell
pxs-prepare D:\proposals\mix --source D:\raw\gonio --source D:\raw\theta2theta
```

`--from`/`--to` are a plain filter and work on every route, including a
hand-copied folder:

```powershell
pxs-prepare D:\proposals\202504_NbO2 --from 2025-04-08 --to 2025-04-10
```

Two things to know about merging several sources. Scans are numbered
chronologically across all of them, so `r0001` is the earliest scan of the
whole beamtime whichever program wrote it. And if a re-import would change a
scan's number — because one turned up between two already-imported ones —
the command stops and says which, since spice anchors point at those numbers;
`--force` turns that into a warning.

### 4. Open the result

The exact URL is printed at the end of the import. For the default layout:

```python
from nx5d import Proposal

root = 'file:///D:/proposals/202601_PtAu'
prop = Proposal(url=f'{root}/scans/{{scan}}.h5', spice=f'{root}/spice')

prop.ls()            # columns: key, type, num_frames, primary, pri. values, secondaries, others, description
prop.r0009.raw       # 20260108/120821 — the tRSS scan; variables theta, ttheta, delay, images, curve, …
prop.r0006.cooked    # 20260108/115550 — an sRSM scan, Q-mapped via the xraydc recipe
prop.r0001.raw       # 20260108/100331 — an xRSS scan; .cooked raises on purpose
```

For `--single-file` the URL is `f'{root}/202601_PtAu.h5#{{scan}}'`.

Which `rNNNN` is which folder is in `scan-mapping.csv`, and every scan carries
`source`, `date`, `time` attributes.

### What shows up in the listing

nx5d builds the listing's `others` and `description` columns from the scan's
`k-header:*` attributes only (`_get_header_items` in `nx5d/repo/universal.py`),
stripping the prefix; unprefixed attributes such as `source` are not harvested.
`description` is special: nx5d seeds it, then `summary.update(header)` lets a
`k-header:description` attribute overwrite it, and `_list` excludes it from the
`others` aggregation. So the importer writes exactly two header attributes:

| Attribute | Column | Content |
|---|---|---|
| `k-header:description` | `description` | `<date>/<time> <notes>`, e.g. `20260108/100331 MV217 T =300K search time zero 15mJ/cm² 800nm` |
| `k-header:integration` | `others` | exposure time in seconds (absent if the parameters file has none) |

Everything else the importer knows about the scan is a plain attribute under
its own bare name and stays out of both columns: `notes` (the `Comments` lines
of `parameters*.txt`, verbatim, without the folder prefix that `description`
carries), `photon_energy`, `datetime`, `source`, `date`, `time`, the
`truncated` / `rows` / `frames` trio on truncated `xRSS` scans, and every
`parameters*.txt` entry under its own key (below).

### Parameters carried onto the scan

Every line of `parameters*.txt` except the leading comment block becomes one
root attribute named exactly as the acquisition software spelled the key —
no prefix. The value is parsed where it holds an unambiguous number and kept
as the raw string where it does not:

```python
a = prop.r0001.raw.attrs
a['Selected Stage']    # 'PXS 600mm'
a['Step Width']        # 0.2                 float, degree sign stripped
a['# of Loops']        # 80                  int, not 80.0
a['ROI']               # [242, 50, 254, 87]
a['Calibration']       # [0.0, 0.0, 6828690.0, -143103.0]
a['Delay Vector']      # '-10:0.5:40, 41:1:200'   a list of ranges, not a number
a['File Name']         # '085055'            an HHMMSS stem, not a count
```

The parse recognises four shapes — a region `'(242, 50); (254, 87)'` as four
ints, tab-separated numbers as a list of floats, a bare integer as `int`, and
a decimal with an optional `°` or `s` suffix as `float` — and leaves
everything else alone. Two traps are handled deliberately: an integer with a
leading zero gets no numeric reading (it is an identifier, not a count), and
`File Name`/`Filename` are excluded by name as well, since `'120703'` is no
more a number than `'085055'` is.

Across the 71 real files that splits cleanly, with no key numeric for only
*some* of its scans:

| | Keys |
|---|---|
| parsed | `Start Position`, `End Position`, `Step Width`, `Velocity`, `Acceleration`, `Exposure Time`, `2 Theta Correction`, `Theta Vector`, `Number of Loops`, `# of Loops`, `Delay Loop`, `Theta Loop`, `ROI`, `Pump Region`, `Norm Region`, `Calib`, `Calibration` |
| raw string | `Date Time`, `Delay Vector`, `Delay Order`, `Theta Order`, `Inner Parameter`, `Selected Stage`, `Stage Unit`, `Tmp Path`, `Data Path`, `File Name`, `Filename` |

The sweep is generic, not a per-type table: a key no release of this importer
has seen still lands on the scan. The vocabulary *is* fixed per scan type
though — every file of a type logs exactly the same keys:

| Scan type | Keys (besides the comment block) |
|---|---|
| `sRSM` | `Date Time`, `Start Position`, `End Position`, `Step Width`, `Number of Loops`, `2 Theta Correction`, `Exposure Time`, `ROI`, `File Name`, `Tmp Path`, `Data Path`, `Calib` |
| `tRSS` | `Date Time`, `Delay Vector`, `Delay Loop`, `Delay Order`, `Theta Vector`, `Theta Loop`, `Theta Order`, `Inner Parameter`, `2 Theta Correction`, `Exposure Time`, `ROI`, `File Name`, `Tmp Path`, `Data Path`, `Calibration` |
| `xRSS` | `Date Time`, `Selected Stage`, `Stage Unit`, `Start Position`, `End Position`, `Step Width`, `# of Loops`, `Velocity`, `Acceleration`, `Exposure Time`, `ROI`, `Pump Region`, `Norm Region`, `Filename`, `Tmp Path`, `Data Path`, `Calibration` |

Three names drift between types for the same concept, so analysis code that
spans scan types has to check both spellings: `File Name` vs `Filename`,
`Calib` vs `Calibration`, and `Number of Loops` vs `# of Loops` (tRSS splits
the count into `Delay Loop` and `Theta Loop` instead). `pytest -m corpus`
pins every list on this page.

Two entries are *also* available in a derived form, because the importer
needs them for its own work: `Exposure Time` as the float
`k-header:integration` (the listing's `others` column) and `Date Time` as the
ISO-8601 `datetime`. The parameters key keeps its own name either way, so
`Date Time` is still the German-format string the file contained.

Nine names belong to the importer rather than to the parameters file —
`notes`, `photon_energy`, `datetime`, `source`, `date`, `time`, `truncated`,
`rows`, `frames`. They are written last, so a future parameters key spelled
the same way cannot displace one; the importer logs a warning if that ever
happens. No real file does today.

### 5. Undo the spice migration

```powershell
Remove-Item -Recurse D:\proposals\202601_PtAu\spice
Rename-Item D:\proposals\202601_PtAu\spice.pre-migration spice
```

### 6. Check without side effects

```powershell
pytest -m corpus -q
```

from a checkout of this repository runs every corpus regression test
against the thinned real proposals committed under `tests/corpus/`,
including one whole-proposal import into a temporary directory. Takes
about a minute and writes nothing outside it.

---

## B. On the JupyterHub (where the data lives)

The hub image ships nx5d and `xraydc`, but not this package. These commands
run in a hub terminal (or prefixed with `!` in a notebook cell).

### 1. Install the importer

```bash
pip install --user pxs-import
```

or, for the development version:

```bash
pip install --user "pxs-import @ git+https://gitlab.com/kmc3-xpp/pxs-import.git@master"
```

A `--user` install needs nothing from the image and changes nothing in it,
so existing notebooks keep working. Cooking the result still needs
`xraydc`, which the image already has.

If nx5d is missing or older than 3.2 (`python -c "import nx5d; print(nx5d.__version__)"`;
the `pm` tool must exist: `pm --version`):

```bash
pip install --user "nx5d @ git+https://gitlab.com/kmc3-xpp/nx5d.git@master"
```

Restart the kernel after installing.

### 2. Run the import

`--user` installs put console scripts in `~/.local/bin`, which may not be on
`PATH`. Either form works:

```bash
~/.local/bin/pxs-prepare /path/to/202601_PtAu --dry-run
python -m pxs_import.prepare /path/to/202601_PtAu --dry-run
```

Then without `--dry-run` to do the import. All options from section A apply.

If the proposal folder is on a read-only share, write elsewhere and migrate a
copy of the spice folder:

```bash
cp -r /path/to/202601_PtAu/spice ~/converted/202601_PtAu/spice
python -m pxs_import.prepare /path/to/202601_PtAu \
    --output-root ~/converted/202601_PtAu \
    --spice-dir   ~/converted/202601_PtAu/spice
```

### 3. Or from a notebook cell

```python
from pathlib import Path
from pxs_import.prepare import prepare_proposal

result = prepare_proposal(Path('/path/to/202601_PtAu'), dry_run=True)   # rehearse
result = prepare_proposal(Path('/path/to/202601_PtAu'))                 # import
result.target_url        # paste into Proposal(url=...)
result.mapping           # [('r0001', ScanFolder(...)), ...]
result.spice_changes     # anchors rewritten
```

`prepare_proposal` locates `pm` next to the running interpreter, then on
`PATH`, and otherwise launches it through `python -c`, so it works from a
kernel even when `~/.local/bin` is not on `PATH`.

### 4. Open it

```python
from nx5d import Proposal
root = 'file:///path/to/202601_PtAu'
prop = Proposal(url=f'{root}/scans/{{scan}}.h5', spice=f'{root}/spice')
prop.ls()
```

Use the `--output-root` path in the URL if you wrote elsewhere.

### 5. Calling `pm` directly (no `pxs-prepare`)

The orchestrator only wraps this command; you can run it yourself. Note the
order: `-P` and `-u` are global `pm` options and come **before** `scan import`,
and `-W frame_guard=None` is required because nx5d's writer otherwise looks
for the frame-guard attribute on the still-empty target and refuses.

```bash
pm -P -u 'file:///path/to/202601_PtAu/scans/{scan}.h5' \
   scan import /path/to/202601_PtAu \
   -R pxs_import.reader:scans_from_pxs photon_energy=8048.0 mapping_file=/path/to/202601_PtAu/scan-mapping.csv \
   -W frame_guard=None
```

This does **not** migrate spice or refuse to overwrite; `pxs-prepare` adds
those.

---

## Importing to S3

The same import can write to an S3 bucket instead of the local disk: one Zarr
store per scan, side by side under the proposal's folder, with the spice folder
beside them. Reading needs nothing special -- nx5d opens `s3://` URLs through
fsspec already.

### One-time credential setup

```bash
pxs-prepare --s3-login
```

This asks for the access key and secret (the secret is never echoed), pre-fills
the endpoint and region, and writes a standard AWS profile:

```ini
# ~/.aws/credentials            # ~/.aws/config
[default]                       [default]
aws_access_key_id = ...         endpoint_url = https://s3.rz.uni-potsdam.de
aws_secret_access_key = ...     region = US
```

Other profiles already in those files are preserved. Nothing is written into
the repository, so no credential can be committed. Writing the `default`
profile is what lets a bare `Proposal('s3://...')` work with no `AWS_PROFILE`
and no `os.environ` preamble; use `--s3-profile NAME` if this machine already
uses AWS for something else, and set `AWS_PROFILE=NAME` when reading.

The command finishes by listing the bucket, so a wrong key is reported at setup
rather than halfway through an upload.

### Per proposal

```bash
pxs-prepare D:/proposals/202601_PtAu --s3
```

It prints a `Proposal(...)` line ready to paste:

```python
from nx5d import Proposal
prop = Proposal(url='s3://pxsrepo/202601_PtAu/{scan}.zarr',
                spice='s3://pxsrepo/202601_PtAu/spice')
prop.r0008.raw
```

Worth knowing:

- **It resumes.** Each scan that finishes is recorded in
  `s3://<bucket>/<proposal>/.udkm-import.json`, together with its
  `YYYYMMDD/HHMMSS` source folder. A re-run skips only the scans that record
  marks complete *and* whose source folder still matches, so an upload killed
  partway carries on where it stopped. A scan whose prefix exists but is not in
  that record is treated as an interrupted write and re-imported from scratch.
- **One `pm` run per scan.** That is what makes the record per-scan rather than
  per-proposal; progress is one line per scan.
- **Spice is pushed from the local folder**, which stays the source of truth. If
  the remote `spice/` prefix already holds objects the run refuses before
  importing anything -- pass `--force-spice` to overwrite it. `--force` does not
  imply this.
- `--s3` cannot be combined with `--single-file` or `--output-root`, and
  `--dry-run` lists the bucket and prints the commands without touching it.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `FileExistsError: … already exists; re-run with --force` | A previous import's `scans/` or `.h5` exists. Re-run with `--force` to replace it. Run numbers are recomputed every time, so do this deliberately. |
| `RefuseFrameStorage: Attribute required for frame guard not found` | You called `pm` directly without `-W frame_guard=None`. Add it; delete the empty `r0001.h5` it left behind. |
| `pm: error: unrecognized arguments: -u` | `-u`/`-P` were placed after `scan import`. Global options go first. |
| `No module named pxs_import` | `pm` is running in an interpreter that does not have this package. Install it there (section B.1) and restart the kernel. |
| `RuntimeError: r0007 (20260108/131040, tRSS): …` | One scan failed; the key, source folder and type are in the message, the original error follows. Fix or remove that folder and re-run with `--force`. |
| `WARNING … unclassifiable scan folder: 20260202/140000` | A `HHMMSS` folder without a `rockingcurves*/scans*/allSteps*.dat`. It is skipped and does not get a run number. |
| Description column shows the scan folder but no notes | Expected when `parameters*.txt` has no `Comments` line (or is missing entirely). |
| nx5d's `Bad header spec` message never appears | Known limitation: nx5d reports an unresolvable `k-primary:0` by putting that message in `description`, and this importer's `k-header:description` overwrites it. If `primary` and `pri. values` are empty for a scan, that is the symptom — check its `k-primary:0` attribute. |
| `description` is just the folder for `sRSM` scans, and `notes` is empty | Expected: the acquisition software never writes comments for rocking curves. |
| `no S3 credentials on this machine` | `--s3` was used before any credentials were stored. Run `pxs-prepare --s3-login` once. |
| `<prefix> already holds objects; re-run with --force-spice` | The remote `spice/` prefix is not empty. The local folder is the source of truth, so pass `--force-spice` to overwrite it. |
| `rNNNN (...): pm reported success but <url> holds no arrays` | `pm` exited 0 without writing, which it does when its scan filter matches nothing. The manifest keeps every earlier scan, so re-running resumes at this one. |
