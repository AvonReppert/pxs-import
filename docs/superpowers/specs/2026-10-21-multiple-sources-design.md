# Multiple source folders and a date range for `pxs-prepare`

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
**Target repository:** `xraydc` only. No nx5d change: the extra inputs travel
to the reader as `pm scan import -R … key=value` kwargs, which `pm` already
forwards.

---

## 1. Intent and why

A PXS beamtime is not stored in one place. Four acquisition programs write to
four sibling folders, and which one a scan lands in is decided by the program
it was started with — which in turn decides its scan type. Measured over all
134 real scans:

| acquisition program | scan types it writes | scans |
|---|---|---|
| `PilatusTheta2Theta` | `sRSM`, `tRSS` | 55 + 47 |
| `PilatusPumpProbe` | `xRSS` | 13 |
| `PilatusScanGoniometer` | `alignment` | 15 |
| `PilatusScanZaber` | `alignment` | 4 |

Each of those folders holds **every beamtime since 2011**, not one proposal.
So assembling a proposal means two things at once: reaching into several
folders, and taking only the days that belong to it.

Today `pxs-prepare <folder>` can do neither. It takes exactly one folder and
imports every date directory in it. The gap is closed by hand: on
2026-10-21 at 19:17, while this very design was being discussed, 19 alignment
scan folders were copied into `notebooks/202504_NbO2/` to join the 44 that
were already there — byte-identical to the ones under `notebooks/20250408/`
and `notebooks/20250424/`. That manual assembly is what this work replaces.

The intended end state, from any of the three machines, with no path typed:

```console
$ pxs-prepare proposals/202504_NbO2 --from 2025-04-08 --to 2025-04-25
```

## 2. Goals

- Read from several source folders in one import, with one chronological
  numbering across all of them.
- Select days by date range: `--from` required to engage it, `--to` optional
  and defaulting to today.
- Find the source folders without the user typing them, on each of the three
  known machines.
- Keep today's one-argument call working unchanged.
- Stay fast against a 15-year archive on an SMB share.
- No nx5d change; `--s3`, `--single-file`, `--output-root` and the spice
  migration keep working in every combination.

## 3. Non-goals

- No GUI. This is the UI-independent layer the GUI will later drive; the CLI
  and the GUI must end up calling the same function (see §9).
- No config file format. `--base` plus an environment variable covers the
  machines that are not presets (§5).
- No copying, moving or writing anything into the source folders. They are
  read-only as far as this tool is concerned.
- No renumbering of an already-imported proposal (§8).

## 4. Anti-goals — what would make this a failure even if it ran

- **Silently importing nothing.** Every way of finding sources can come up
  empty: no preset matches, `--base` points somewhere wrong, the date range
  misses. An empty import that exits 0 is the worst outcome here, because the
  next thing the scientist does is look for data that was never written.
- **Walking the whole archive.** `discover_scans` today opens and classifies
  every `HHMMSS` folder it finds. Pointed at `PilatusTheta2Theta` that is tens
  of thousands of directory round-trips over SMB.
- **Two scans from different programs quietly becoming one.** Scan identity is
  `YYYYMMDD/HHMMSS` (§7), which is not qualified by the program.
- **A flag that switches modes.** Source selection and date filtering are
  independent questions; an implementation where `--from` only works with
  presets, or `--source` disables the date filter, is wrong even if every
  individual case works.

## 5. Where the sources come from

Three steps, first match wins:

1. **`--source <path>`**, repeatable. Each is a folder that directly contains
   date directories (possibly behind a year level, §6).
2. **Otherwise the positional argument**, if it contains date directories
   **or is one itself** — its own name matching `^\d{8}$`. Both forms are
   today's behaviour (`discover_scans` already accepts either), and getting
   this predicate wrong is a trap: test only for "contains", and
   `pxs-prepare notebooks/20250408` falls through to route 3 and turns a
   one-day import into a whole-archive one.
3. **Otherwise the base path**, under which the four program folders are
   expected by their fixed names.

If all three come up empty, the tool exits non-zero with a message naming the
base paths it looked for. **Builder's call, recorded here:** this is not a
warning, because of anti-goal #1.

**Route 3 requires `--from`.** The program folders hold every beamtime since
2011; an import from them with no date range is never what anyone means, and
on an empty workspace route 3 is exactly what fires. Without `--from` the tool
exits non-zero saying so. **Builder's call, recorded here.** This does not
contradict anti-goal #4: the date range is still a plain filter that works
with any source, and routes 1 and 2 still accept its absence. What is refused
is one specific combination — the shared archive with no bound — because the
cost of getting it wrong is a 15-year import onto S3.

### 5.1 Resolving the base path

`--base <path>` beats `UDKM_BASE` in the environment, which beats auto-detection
against the three built-in presets — the first one that exists on this machine
wins:

```python
BASE_PRESETS = (
    r'\\141.89.115.120\Measurement\XRayLab-Pilatus',   # the laptop
    '/home/jovyan/data/XRayLab-Pilatus',               # the JupyterHub
    'P:\\',                                            # the measurement PC
)
PROGRAM_DIRS = ('PilatusTheta2Theta', 'PilatusPumpProbe',
                'PilatusScanGoniometer', 'PilatusScanZaber')
```

**Decision by the user:** on the measurement PC the preset is `P:\`, the share,
*not* the local `C:\Users\Labor\Documents\Measurements` — even though local disk
would be faster. All 134 scans record both (`Tmp Path: P:\<program>\…`,
`Data Path: C:\Users\Labor\Documents\Measurements\<program>\…`), so both layouts
exist, but only the share is the same data all three machines see. A proposal
imported from the laptop, the hub or the measurement PC must come out identical;
a local copy holding only the running measurement would drop scans with nothing
to show for it. The local path remains reachable with `--base`.

A program folder that does not exist under the base is skipped silently — not
every site runs all four.

**Probing must not hang.** `BASE_PRESETS[0]` is a UNC path, and on Windows
`.exists()` against an unreachable UNC blocks for tens of seconds — which the
laptop would pay on every run whenever it is off the lab network, and the
measurement PC would pay before reaching its own preset third in the list. So
only presets whose shape matches the current platform are probed (the UNC and
`P:\` on Windows, the POSIX path elsewhere), and before probing a network path
the tool says which one it is trying, so that a pause is explicable rather
than a hang. `UDKM_BASE` and `--base` short-circuit probing entirely, which is
the documented answer for a laptop that is usually disconnected. **Builder's
call, recorded here.**

**`--base` and `--source` mean different things**, and the error messages should
keep them apart:

| flag | what you give it | what is expected underneath |
|---|---|---|
| `--base` | the parent of the four program folders | `PilatusTheta2Theta/`, `PilatusPumpProbe/`, … |
| `--source` | one concrete data folder | `YYYYMMDD/` directories, possibly behind a year level |

## 6. Finding the days, without walking the archive

### 6.1 The year level is irregular and not derivable

From the real `Tmp Path` values:

```
PilatusTheta2Theta\20250408\…          2025: no year level
PilatusTheta2Theta\2026\20260108\…     2026: year level
PilatusPumpProbe\2025\20260108\…       year folder 2025, measurement day in January 2026
```

The last line is the one that kills the obvious shortcut: three measurement days
from 2026 are filed under `2025`. **Never construct `<root>/<YYYY>/<YYYYMMDD>`
from the requested date** — those scans would be invisible. The year level has
to be discovered, not computed.

### 6.2 The walk

Per source root:

1. List the root once. Entries matching `^\d{8}$` are measurement days;
   entries matching `^\d{4}$` are year levels.
2. List each year level once. Entries matching `^\d{8}$` are measurement days.
3. **Filter the measurement days against the date range, here.**
4. Only now descend into the surviving days and classify their scan folders,
   exactly as `discover_scans` does today.

Cost per source: one directory listing, plus one per year level — bounded by
the number of years, not by the size of the archive. Four sources come to
roughly fifty listings. The expensive part today is step 4, and after step 3 it
only ever runs on days the user asked for.

Nesting is **one** level deep at most: a year level inside a year level is not
a layout that exists, and treating it as one would reopen the walk-everything
problem. Anything else in the root — zip files, `spice/`, stray directories —
never matches either pattern and is skipped, as today.

### 6.3 Date range semantics

`--from YYYY-MM-DD`, `--to YYYY-MM-DD`, both **inclusive**, day granularity,
compared against the `YYYYMMDD` directory name. Without `--to`, the range ends
today. Without `--from` there is no filter at all and everything found is
imported — today's behaviour.

The filter applies to whatever sources were selected, by any of the three
routes in §5. **Builder's call:** it is a filter, never a mode switch
(anti-goal #4), so `--from` with a hand-copied folder is an ordinary
combination and must work.

## 7. Merging, numbering, and identity

`number_scans` numbers chronologically. With several sources, the merge happens
before numbering: concatenate every source's scan folders, sort by
`date + time`, number `r0001…` across the whole set.

**Scan identity stays `YYYYMMDD/HHMMSS`** (`ScanFolder.source`), unqualified by
the program. It is what the mapping CSV and the S3 manifest record, and what
makes an import restartable and machine-independent — changing it would
invalidate every manifest already in the bucket.

That makes a collision conceivable: two scans, two programs, the same second.
In practice all four programs drive the same detector and cannot run at once,
and the real corpus has no collision — and because the three source routes are
mutually exclusive (§5), the realistic cause is two overlapping `--source`
paths rather than two programs.

**Decision by the user: coalesce, but warn.** When two folders carry the same
`YYYYMMDD/HHMMSS`, the first wins and a warning names both. Failing outright
would refuse the import for the very users this feature is for — the ones
whose proposal folders already hold hand-copied duplicates of what the sources
contain — while §3 forbids the tool from writing to sources and §8.1 forbids
renumbering, leaving them no way forward but deleting data.

The warning is per duplicate and unmissable, not a log line: anti-goal #3 is
about building one scan out of two folders *unnoticed*, and the warning is
what makes it noticed. Which folder is "first" follows source order, so it is
deterministic rather than filesystem-dependent.

This check must run **before** `migrate_spice`: its
`key_mapping = {sf.source: key …}` (`prepare.py:422`) is a dict keyed by
exactly this value and would collapse duplicates with no warning at all.

A genuine conflict — same `YYYYMMDD/HHMMSS`, different scan type — is a
different thing and still fails, naming both folders. There is no sane way to
pick.

### 7.1 How the sources reach the reader

`pm scan import <path> -R pxs_import.reader:scans_from_pxs key=value …` invokes
the reader as `reader(source, **reader_kw)`, so `key=value` pairs arrive as
keyword arguments — that is how `photon_energy` and `only` already travel.

**The orchestrator resolves the mapping once; the reader materialises it.**
That is the central structural decision of this design, and it reverses an
earlier draft in which the reader re-walked the sources itself.

Today both sides discover and number independently: `prepare_proposal` to
write `scan-mapping.csv` and build `only=` keys, and `scans_from_pxs` again
inside the `pm` subprocess. Against one static folder that could not diverge.
Against four live program folders with `--to` defaulting to *today*, the two
walks run seconds apart over an archive the measurement PC may be writing
into — and if they disagree, `only=r0007` names a different scan than the CSV
and the migrated spice anchors claim. Under `--s3`, where `_s3_import_loop`
runs one `pm` per scan, the reader would additionally repeat the entire walk
134 times over SMB, including the expensive step 4 of §6.2.

So the reader receives a **resolved list of `(scan key, absolute folder path,
scan type)`** and does no discovery, no numbering and no date filtering. There
is exactly one numbering authority, the divergence class disappears, the
per-scan re-walk disappears, and `only=` becomes unnecessary for the
multi-source path.

**The single-path contract stays.** `scans_from_pxs(source)` with nothing but
a path must keep working exactly as today — it is the documented nx5d reader
target, and someone will invoke `pm` directly with it. The resolved list is an
additional keyword argument; absent it, the reader discovers as it always has.

Two constraints on the wire format:

- **`from` is a Python keyword**, so the reader's parameters cannot be named
  `from`/`to` however the CLI flags are spelled.
- The positional argument is **never implicitly a source** once the resolved
  list is given. Under routes 1 and 3 the positional is the data-free
  workspace (§8); under route 2 the orchestrator puts the workspace into the
  list explicitly. A reader that unioned the two would double-count every scan
  in route 2 and trip the §7 duplicate check on a correct import.

The exact spelling of the argument (one delimited string, a JSON blob) is
**left to the builder**; a delimiter has to survive Windows paths with drive
letters and UNC prefixes, which rules out `:`.

## 8. The proposal folder stops being the data folder

Today the positional argument does three jobs: it is the source, the output
location, and the proposal's name. `--output-root` already separates the second.
This work removes the first, leaving **workspace and name**: `spice/`,
`scan-mapping.csv`, and — unless `--output-root` or `--s3` says otherwise — the
output.

Follows from that, and the implementation must handle it:

- **The workspace may not exist yet.** With preset sources it usually will not,
  since no raw data has to live there. It is created rather than refused.
  Today's code assumes it exists, because the data was in it.
- **Relative paths already work, and stay working.** `prepare_proposal`
  resolves `proposal_root`, `output_root` and `spice_dir` on entry
  (`prepare.py:343-345`), so the path handed to the `pm` subprocess is
  already absolute and a GUI with a different CWD is not a problem. The new
  source paths must be resolved in the same place, for the same reason.
- **Sources are opened read-only.** Nothing is written into a source folder —
  no mapping file, no spice, no marker.

### 8.1 Renumbering: named, not solved here

Numbering is chronological across the merged set, so a scan that appears
*between* two already-imported ones shifts every `r`-number after it. With four
sources merged this is likelier than it was with one: importing mid-beamtime
from a proposal whose alignment scans arrive later is enough to trigger it.

`migrate_spice` cannot absorb it — it migrates `date/time → r-number`, not
`r-number → r-number`.

This design does **not** fix it. What it does owe the user is detection: on a
re-import, if a scan's number changes from what the mapping table recorded,
say so loudly rather than writing a quietly different repository.

**The baseline is `scan-mapping.csv`, read back before it is overwritten.**
It is written today at `prepare.py:373` and never read again; this gives
it a second job. It lives in the workspace, which is the positional argument
and therefore always known — unlike the S3 manifest, which exists only under
`--s3`. If no CSV is present the check is skipped: a first import has nothing
to disagree with. **Builder's call, recorded here.**

### 8.2 Both records gain the source root

A scan's identity stays `YYYYMMDD/HHMMSS` (§7), which no longer says which
program folder it came from. With four sources feeding one proposal, neither a
collision report, nor a re-import check, nor the later GUI can otherwise tell
`PilatusTheta2Theta/20250408/151027` from
`PilatusScanGoniometer/20250408/151027`.

So `scan-mapping.csv` gains a source-root column, and each S3 manifest entry
gains a `root` field. Both are additive: the CSV is informational and read only
by this tool, and a manifest entry without `root` is simply one written before
this change and must keep working. The identity key itself does not change, so
nothing already in the bucket is invalidated. **Builder's call, recorded
here.** `doc/pages/howto.md` documents the CSV and needs updating
with it.

## 9. Shape of the code

One UI-independent function takes resolved inputs — sources, date range,
target, options — and does the work. `main()` is the thin CLI shell over it,
and the later GUI is a second shell over the same function. That split is
Florin's guidance for pmtool/pmmodel applied one level down, and it is why §3
keeps the GUI out of scope without letting the design foreclose it.

Everything stays inside `src/pxs_import/`. `signal.py` and `cooking.py` are not
touched: the `io/` subpackage must remain liftable into a standalone beamline
package as a pure file move.

## 10. Testing

Default suite, synthetic folders under `tmp_path`:

- Two source roots, interleaved dates, merge chronologically into one
  `r0001…` sequence.
- A year level on one source and not the other; both discovered.
- A year folder whose name disagrees with the dates inside it (the
  `2025/20260108` case) — the scans are still found.
- Date range: inclusive at both ends; `--to` omitted reaches today; `--from`
  omitted imports everything.
- `--from` against a hand-copied folder (source route 2) filters it.
- Duplicate `YYYYMMDD/HHMMSS` across two sources: the first source wins, a
  warning names both folders, and the import completes. The same key with
  *different* scan types still fails.
- The duplicate check runs before `migrate_spice`, whose `key_mapping` dict
  would otherwise collapse duplicates silently.
- Route 3 without `--from` exits non-zero; routes 1 and 2 without `--from`
  import everything found.
- A positional argument that is itself a date directory is used as the source
  (route 2), not passed over to route 3.
- No source found anywhere exits non-zero with the base paths in the message.
- A workspace directory that does not exist is created.
- **No directory below a day outside the range is opened.** Assert on the
  access, not just the result. This is the only way the §6.2 performance
  property is pinned rather than hoped for.

  To make that testable, discovery takes an optional listing callable
  defaulting to `Path.iterdir`, and the test passes a counting one. A keyword
  argument with a default is preferred over monkeypatching `Path.iterdir`,
  which is fragile and global; it costs one parameter and does not affect §9's
  requirement that `io/` stay liftable as a pure file move. **Builder's call,
  recorded here.**

Corpus suite (`-m corpus`): the four program folders are not on the test
machines, so the real-data coverage here is the hand-copied proposals that
already exist; a date range over `202504_NbO2` (which now holds both
`20250408` and `20250424`) exercises the filter against real vocabularies.

## 11. Left to the builder

- The wire format for passing several sources and the date range through
  `pm`'s `key=value` kwargs (§7.1), subject to the `from`-is-a-keyword and
  delimiter constraints.
- Whether `--source` and `--base` are mutually exclusive at the argparse level
  or `--source` simply wins (§5 fixes the precedence either way).
- Whether the day filter compares strings or parsed dates — both work on
  `YYYYMMDD`; strings are fewer moving parts.
- How the "scan number changed since the mapping table" check (§8.1) is
  surfaced: which command reports it, and whether `--force` overrides it.
