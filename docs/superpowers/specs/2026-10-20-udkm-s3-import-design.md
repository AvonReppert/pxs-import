# UDKM → S3 import for `pxs-prepare`

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

**Status:** design, awaiting implementation. No code written.
**Date:** 2026-10-20
**Target repository:** `xraydc` only. Verified against the installed nx5d in
`.venv/Lib/site-packages/nx5d` on 2026-10-20: **no nx5d change is required.**

---

## 1. Intent and why

`pxs-prepare <proposal>` today discovers UDKM scan folders, numbers them
`r0001…`, shells out to nx5d's `pm scan import`, and writes one HDF5 file per
scan next to the raw data. The result is a local-only repository: to analyse it
from a machine that is not the one holding the raw TIFFs, the files have to be
copied by hand.

The group has an S3 endpoint (`https://s3.rz.uni-potsdam.de`) and a writable
bucket, `pxsrepo`. nx5d already knows how to *read* a repository from S3 — its
own `nx5d/xrd/kmc3.py` ships a public-access preset pointing at that same
endpoint, with URLs of the form `s3://kmc3pub-data/{proposal}/…`.

So the missing piece is only on the writing side, and only in `xraydc`: teach
`pxs-prepare` to point the import at S3 instead of the local disk, and to put
the credentials somewhere they do not have to be retyped or pasted into a
notebook.

The intended end state:

```console
$ pxs-prepare --s3-login                      # once, ever
$ pxs-prepare notebooks/202601_PtAu --s3      # per proposal
```

and then, from any machine set up the same way:

```python
from nx5d import Proposal
prop = Proposal(url='s3://pxsrepo/202601_PtAu/{scan}.zarr',
                spice='s3://pxsrepo/202601_PtAu/spice')
prop.r0008.raw
```

---

## 2. Goals

1. `pxs-prepare <proposal> --s3` imports every scan to
   `s3://pxsrepo/<proposal>/r####.zarr`, one Zarr store per scan, side by side.
2. Credentials live on the machine, outside the repository, entered once.
   `--s3-login` collects them; no secret reaches a notebook, a `.env`, the shell
   history, or a log line.
3. A re-run **skips scans already complete on S3**, so an interrupted upload
   resumes rather than restarting — and never mistakes a half-written scan for a
   complete one.
4. The proposal's local `spice/` folder is pushed to
   `s3://pxsrepo/<proposal>/spice`, refusing to clobber an existing remote copy
   unless `--force-spice` is given.
5. The command prints a ready-to-paste `Proposal(...)` line with both S3 URLs.
6. The existing local (`file://`, HDF5) behaviour is untouched when `--s3` is
   absent.

## 3. Non-goals

- Reading from S3. nx5d already does this; nothing here touches the read path.
- Changing nx5d. If the implementation finds itself wanting to patch nx5d, that
  is a signal the design is wrong — stop and raise it.
- Multi-bucket or multi-endpoint federation. One bucket, one endpoint.
- ACLs and public access. Objects stay private and are reached with the same
  credentials (§5.6). No `public-read`, no second read-only key.
- Syncing *back* from S3 to local, or any conflict resolution. The local copy is
  always the source of truth (§6.5).
- Importing to S3 *and* local in one run. `--s3` replaces the local target.
- `--single-file` on S3. Rejected at argument-parse time (§6.3).
- Making `pxsrepo` an nx5d `--repository` bookmark. That would need an nx5d
  change; explicit URLs are the access route.

## 4. Anti-goals

Things that would make this a failure even if it technically worked:

- **A secret that can be committed.** Any design where the key ends up in the
  working tree, in `pyproject.toml`, in a notebook cell, or in a file that `git
  status` would ever list is wrong, including "it's in `.gitignore`".
- **A silently corrupt repository.** A scan that was half-uploaded when a run
  died must not be mistaken for a complete one on the next run. Re-uploading a
  good scan wastes time; skipping a bad one poisons the data. §6.4 exists
  entirely for this, and §5.3 explains why the obvious approaches fail at it.
- **An `xraydc` that imports `nx5d`.** The one-directional, string-resolved
  coupling described in `CLAUDE.md` is load-bearing. `prepare` shells out
  to the `pm` console script and must keep doing so.
- **Credentials that only work inside `pxs-prepare`.** The same setup must
  serve a plain `Proposal(...)` call in a notebook, with no `os.environ`
  preamble.

---

## 5. Constraints

### 5.1 What the environment already provides

Verified in `.venv` on 2026-10-20:

| Package | Version | Role |
|---|---|---|
| `zarr` | 3.4.1 | store format, `zarr.open(url)` |
| `s3fs` | 2026.9.0 | fsspec S3 backend |
| `fsspec` | 2026.9.0 | URL → filesystem |
| `aiobotocore` | 3.9.2 | s3fs's AWS client |
| `botocore` | 1.43.106 | credential chain, `endpoint_url` config support |

`boto3` is **not** installed and is not needed — s3fs uses `aiobotocore`.

### 5.2 How `pm` chooses a writer

`pm scan import` derives the writer from the target URL, not from a flag:

- `nx5d/pmmodel/scan.py:299` `target_outer_inner` splits the formatted target on
  `#`. With no fragment, `outer` is the whole URL.
- `nx5d/pmmodel/scan.py:367` `_get_writer(target, ttype)` — when `ttype` is
  empty it takes the substring after the last `.`, so `…/r0001.zarr` yields
  `zarr`, and looks up `nx5d.tools.write_to_zarr`.
- `nx5d/tools.py:343` `write_to_zarr` → `write_to_astor(kind='zarr')`, which at
  `tools.py:136` calls `camagick.storage.base.load_from_url(outer, kind='zarr', …)`.
- `camagick/storage/zarr.py:30` calls `zarr.open(self.outer_path, mode='a')`
  with the URL as given, which routes `s3://…` through fsspec/s3fs.

**Implication:** passing `-W zarr …` as the writer hint bypasses
`camagick.storage.base.probe_url` entirely (`find_loader` short-circuits when
`kind` is given). Pass the hint explicitly rather than relying on extension
probing, which has to backtrack through a URL path.

### 5.3 Two traps in the import path

Both were found during review of an earlier draft of this document, after that
draft had asserted the opposite of each. They are the reason §6.4 looks the way
it does, and an implementer who "simplifies" either one will reintroduce silent
data corruption.

**(a) The reader is eager, and `pm`'s scan filter runs too late.**
`pm scan import SOURCE [glob...]` accepts a scan list
(`nx5d/pmtool/scan.py:36`), but `_glob_scans` (`nx5d/pmmodel/scan.py:489`)
filters *after* the reader has already yielded. And `scans_from_pxs`
([`src/pxs_import/reader.py:337-348`](../../../src/pxs_import/reader.py#L337-L348))
calls `parse_dat` → `plan_frames` → `load_stack` → `build_scan_h5like` **inside
the loop, before each yield**. `load_stack`
([`frames.py:115`](../../../src/pxs_import/frames.py#L115)) is eager:
it preallocates `(N, H, W)` float32 and `tifffile.imread`s every frame.

So invoking `pm` once per scan and filtering with `pm`'s own glob would read
*every* scan's images on *every* invocation — for the 19-scan / 6042-TIFF /
2.3 GB PtAu proposal, ≈115k image reads instead of 6k, and the corpus sits under
OneDrive, which can add rehydration on top. §6.2 solves this with a reader-side
filter instead.

**(b) `write_to_astor` skips existing datasets rather than overwriting them.**
Its default is `overwrite_data=False` (`nx5d/tools.py:26`), and at
`nx5d/tools.py:267-270` that means `if k in zfile.keys(): continue` — an
existing dataset is **skipped**. `pm` never passes `overwrite_data` unless it is
given as a `-W key=value`.

Meanwhile camagick creates each array at full shape upfront
(`camagick/storage/zarr.py`, `create_array(shape=dshape)`) and then assigns, so
a run killed mid-write leaves a correctly-shaped array with missing chunks that
**read back as the fill value**. Re-importing over it would skip it, and the
scan would look complete while being partly zeros.

### 5.4 Global `pm` flags in use

`nx5d/pmtool/pm.py`: `-u/--url` (target), `-d/--dry`, `-P/--progress`,
`-p/--proposal`. The target URL bakes the proposal name in literally rather than
using `{proposal}`, so `-p` is not needed and stays unused.

`pm` exits 0 in cases where nothing was written — a glob matching no scan leaves
the loop body unexecuted and `PmToolScanImport.__call__` still returns 0
(`nx5d/pmtool/scan.py:204`). Exit status alone is therefore not a completion
signal (§6.4).

### 5.5 Credential chain

s3fs → aiobotocore → botocore resolves credentials in the standard order:
environment variables, then `~/.aws/credentials`, keyed by `AWS_PROFILE` and
falling back to the `default` profile. botocore ≥ 1.31 reads `endpoint_url` from
a profile section in `~/.aws/config`, and 1.43.106 is installed, so endpoint and
region live in the profile rather than in environment variables.

### 5.6 Decisions taken by the user

Recorded here because they close off design branches that would otherwise look
open:

1. **Access model.** The data is private and reached with the same credentials.
   No ACL plumbing, no `public-read`, no separate read key.
2. **No other AWS use on this machine.** `--s3-login` may therefore write the
   `default` profile, which is what lets a bare `Proposal(...)` work with no
   `AWS_PROFILE` and no notebook preamble (§4).
3. **The key has full access, including delete.** Re-import may delete a stale
   prefix before rewriting it (§6.4).

### 5.7 Platform

Windows is the primary development and use platform (`%USERPROFILE%\.aws\`).
Any file-permission hardening must degrade gracefully rather than assuming POSIX
`chmod`.

---

## 6. Design

### 6.1 Target URL

```
s3://pxsrepo/<proposal>/{scan}.zarr
```

Bucket from `--s3-bucket` (default `pxsrepo`), proposal from
`proposal_root.name`, `{scan}` left as a literal placeholder for `pm` to format,
exactly as the `file://` form does today.

`target_url_for` ([`prepare.py:51`](../../../src/pxs_import/prepare.py#L51))
grows the S3 case; it uses neither `single_file` nor `output_root`.

The scheme is `s3://` with the bucket as netloc — **not** `s3:///pxsrepo/…`.
A triple slash parses as an empty netloc with `/pxsrepo/…` as the path, which is
a different location. The two-slash form is used throughout, matching nx5d's own
`kmc3_s3_public_urls`.

### 6.2 Reader change: `only=`

`scans_from_pxs` gains one optional keyword:

```python
def scans_from_pxs(source, *, photon_energy=8048.0, only=None, …):
```

`only` is a single scan key or a comma-separated list of them. When set, the
filter is applied **immediately after `number_scans`**, before `parse_dat` /
`load_stack` — so non-selected scans cost a directory listing, not an image
read. This is what makes the per-scan loop in §6.4 affordable (§5.3a).

`only=None` keeps today's behaviour exactly, so the change is additive and the
string-resolved reader contract (`-R pxs_import.reader:scans_from_pxs`) stays
compatible. `pm` passes it through as a `key=value` reader spec entry, parsed by
nx5d's `opcast`.

Secondary behaviour: when `only` is set, suppress the mapping-table print to
stderr at [`reader.py:333`](../../../src/pxs_import/reader.py#L333). Otherwise a
per-scan loop prints the full table once per scan.

A scan key in `only` that does not exist in the mapping is an error, not a
silent empty result — this is what turns §5.4's "exit 0 wrote nothing" case into
a visible failure.

### 6.3 CLI surface

New flags on `pxs-prepare`:

| Flag | Default | Meaning |
|---|---|---|
| `--s3` | off | Import to S3 instead of the local filesystem. |
| `--s3-bucket NAME` | `pxsrepo` | Bucket for both data and spice. |
| `--s3-profile NAME` | unset | AWS profile. Unset means the standard chain, i.e. `default`. |
| `--force-spice` | off | Overwrite an existing remote `spice/` prefix. |
| `--s3-login` | — | Separate mode: write credentials, do nothing else. |

Interactions:

- `--s3` with `--single-file` → argument error. The layout is one store per
  scan; a single-file Zarr would need a `#{scan}` fragment and a different URL
  shape.
- `--s3` with `--output-root` → argument error. `--output-root` names a local
  directory and is meaningless when the target is a bucket.
- `--s3-login` with any other flag except `--s3-profile` → argument error. It is
  a mode, not a modifier.
- `--force` re-imports every scan, ignoring the manifest (§6.4). It does **not**
  imply `--force-spice`; the two are independent because spice is hand-edited
  and data is generated.
- `--dry-run` with `--s3` lists the bucket, prints the resulting skip set and
  the `pm` commands, and touches neither S3 nor the spice folder.

`scan-mapping.csv` is still written locally, into `proposal_root`, in both
modes. It is a human artefact, not repository content.

### 6.4 Resume: the manifest

A manifest object records what is known-complete:

```
s3://pxsrepo/<proposal>/.udkm-import.json
  {"version": 1,
   "scans": {"r0007": {"source": "20260108/120703",
                       "imported": "2026-10-20T14:03:11Z"}}}
```

`source` is `ScanFolder.source`, the `YYYYMMDD/HHMMSS` folder. It is recorded
because `number_scans` re-derives `r####` by chronologically renumbering the
whole proposal on every run: if a scan folder is added, removed or repaired
between runs, every later number shifts, and a manifest keyed on `r0007` alone
would then protect an upload that no longer corresponds to that scan.

**Skip rule.** A scan is skipped iff its key is in the manifest *and* the
recorded `source` equals the current mapping's. Any other state — absent,
source mismatch, or a prefix on S3 with no manifest entry — means re-import.
`--force` skips nothing. A manifest that is missing or unparseable is treated as
empty, with a warning.

**Per-scan loop.** For each scan to import, in mapping order:

1. If its `{scan}.zarr` prefix exists on S3, **delete it** (recursively). This is
   the fix for §5.3b: `overwrite_data=False` would skip existing arrays, and
   overwriting in place cannot remove an array whose shape changed. The user's
   key has delete rights (§5.6.3).
2. Run `pm` for that one scan:
   ```
   pm -P -u s3://<bucket>/<proposal>/{scan}.zarr scan import <proposal_root>
      -R pxs_import.reader:scans_from_pxs photon_energy=<e> only=<scan>
      -W zarr overwrite_data=True frame_guard=None
   ```
   `overwrite_data=True` is belt-and-braces after step 1, and makes the command
   correct in isolation.
3. Require exit 0 **and** a post-check: reopen the remote store and confirm it
   holds at least one array. Step 1 plus §6.2's "unknown key is an error" make
   the remaining failure mode narrow, so this check stays cheap deliberately —
   it is guarding against "wrote nothing", not verifying the science.
4. Append the entry to the manifest and write it back, so an interruption loses
   at most the scan in flight.

A failed scan aborts the run with a message naming the scan; the manifest keeps
everything before it, so re-running resumes there.

**Cost.** One Python process start and one proposal directory walk per scan.
With §6.2's reader filter, image reads are exactly once per scan across the
whole run. Against a TIFF upload measured in minutes per scan the overhead is
small, but it should be measured on the real PtAu proposal and reported.

Because the loop is now owned by `pxs-prepare` rather than by `pm`, print
one progress line per scan; `pm -P`'s own bar now covers a single scan.

**Rejected: one `pm` invocation for all missing scans.** `pm` reports success
only for the whole batch, so a crash leaves no way to tell which scans finished.
The manifest could only be written on whole-batch success, collapsing resume
back to restart-from-scratch.

### 6.5 Spice

Unchanged locally: `fix_recipes` then `migrate_spice` run against the local
`spice/` folder exactly as today, before any upload. The local folder is the
source of truth.

Then, if `--s3`:

1. List `s3://<bucket>/<proposal>/spice/`.
2. If it holds any object and `--force-spice` was not given → raise, naming the
   prefix and the flag.
3. Otherwise upload every file in the local `spice/` folder to that prefix.

The spice records are small JSON files; `fsspec`'s `put` over the folder is
sufficient. `migrate_spice`/`fix_recipes`
([`spice.py`](../../../src/pxs_import/spice.py)) keep their local
`Path` signatures and are **not** made fsspec-aware — the upload is a separate
step layered on top.

`--force-spice` overwrites colliding objects; it does not delete remote files
with no local counterpart. Mirroring deletions is out of scope (§3).

### 6.6 Credentials and `--s3-login`

`--s3-login` prompts and writes two files under `~/.aws/`:

```ini
# ~/.aws/credentials
[default]
aws_access_key_id = …
aws_secret_access_key = …

# ~/.aws/config
[default]
endpoint_url = https://s3.rz.uni-potsdam.de
region = US
```

The `default` profile is deliberate and rests on §5.6.2 — this machine uses AWS
for nothing else. It is what makes a bare `Proposal('s3://…')` work in a
notebook with no `AWS_PROFILE` and no `os.environ` preamble, satisfying §4.
`--s3-profile NAME` writes `[NAME]` / `[profile NAME]` instead, for anyone whose
machine does not meet that assumption; `--s3` then sets `AWS_PROFILE` for both
the `pm` subprocess and its own process (which does its own S3 access for §6.4
and §6.5).

Rules:

- The secret is read with `getpass` — never echoed, never logged, never placed
  in a traceback-visible local that gets formatted into a message.
- Both files are read, modified and rewritten with `configparser` so other
  profiles survive. An existing profile of the same name is overwritten only
  after an explicit confirmation prompt.
- Files are created owner-only where the platform supports it, best-effort on
  Windows. A failure to harden permissions warns, it does not abort.
- Endpoint and region are prompted with the known values pre-filled, so the
  common case is two <kbd>Enter</kbd> presses.
- The profile is validated before the command returns: open the bucket through
  `fsspec` and list it. A failure is reported as a credential problem, naming
  the written file paths, rather than surfacing a raw botocore error.

`AWS_CA_BUNDLE` is deliberately not set. nx5d's `kmc3.py` sets it to a Linux
path (`/etc/ssl/certs/ca-certificates.crt`) that does not exist on Windows;
inheriting that would break TLS. If certificate verification fails against this
endpoint on Windows, that is a separate finding to report, not something to
paper over with `verify=False`.

### 6.7 Final output line

```python
Proposal(url='s3://pxsrepo/202601_PtAu/{scan}.zarr', spice='s3://pxsrepo/202601_PtAu/spice')
```

Printed in place of today's `file:///` line when `--s3` is active.

### 6.8 `PrepareResult`

Gains the S3 facts the caller cannot otherwise recover: the spice target URL,
the set of scans skipped, and the list of scans actually imported. `command`
now holds a list of commands; its docstring must say so.

---

## 7. Testing

The existing suite's split (`CLAUDE.md`, "Conventions") is the model: synthetic
data, `tmp_path`, no network in the default run.

1. **URL construction** — the S3 branch of `target_url_for`, including the
   two-slash form and the `{scan}` placeholder surviving.
2. **Argument parsing** — each rejected flag combination in §6.3 raises.
3. **Reader filter** — `scans_from_pxs(..., only='r0002')` yields only that
   scan; and, the point of §6.2, `load_stack` is **not** called for the others.
   Assert that directly (monkeypatch/spy), not via timing. An unknown key
   raises. `only=None` is unchanged from today.
4. **Skip set** — with the S3 listing and manifest read stubbed: empty manifest
   → all scans; partial manifest → the complement; `--force` → all regardless;
   unparseable manifest → all, with a warning; **source mismatch → re-import**
   even though the key is present.
5. **Delete-before-write** — an existing remote prefix is deleted before the
   `pm` invocation for that scan, and `-W zarr overwrite_data=True` is in the
   command.
6. **Manifest update** — a runner that fails on the Nth scan leaves a manifest
   containing exactly the first N−1, and the run aborts naming scan N.
7. **Post-check** — a runner that exits 0 while leaving an empty store fails the
   scan and does not write a manifest entry.
8. **Spice guard** — non-empty remote prefix without `--force-spice` raises and
   uploads nothing; with it, uploads.
9. **`--s3-login`** — against a `tmp_path` fake home: a pre-existing unrelated
   profile in both files survives; the secret never appears in captured stdout
   or in the log record stream.
10. **Layering** — extend `tests/packaging_test.py`: `import xraydc` must
    still not pull `s3fs`/`fsspec`/`boto*` into `sys.modules`. S3 imports are
    function-local, matching how `signal.py` treats `xrayutilities`.

A real-endpoint test is **not** added to the default suite. If one is written it
belongs behind a new opt-in marker alongside `corpus` and `nx5d`, registered in
`pyproject.toml`. Note the existing `addopts` already deselects `corpus`; a new
marker needs the same treatment or it will run in CI without credentials.

**CI note.** `.gitlab-ci.yml` installs only the `test` extra. Per `CLAUDE.md`,
any dependency the suite imports must be reachable from that extra. If `s3fs`
becomes a new optional dependency it must be mirrored into `test`, or collection
breaks for the whole suite, not just these tests.

**Run `pytest -m corpus`** after this work. §6.2 changes `scans_from_pxs`, and
the corpus tests are the only ones exercising real `parameters*.txt`
vocabularies; being deselected by default they will not announce a break.

---

## 8. Known limitations

- **Interrupted-scan detection relies on the manifest.** If the manifest object
  is lost or the bucket is edited by hand, the skip set degrades to "re-import
  everything", which is safe but slow. `--force` is the explicit remedy.
- **No remote deletion of retired scans.** Scans or spice records removed
  locally stay on S3. A renumbering that shifts `r####` will re-import under the
  new numbers and leave the old objects behind.
- **Serial upload.** One scan at a time. If throughput is the bottleneck,
  parallelising the loop is a follow-up, not part of this design.
- **One credential set.** Data and spice share a bucket, endpoint and profile.
- **`default` profile.** §6.6 rests on this machine having no other AWS use. If
  that changes, `--s3-profile` is the escape hatch, and the notebook then needs
  `AWS_PROFILE` set.

---

## 9. Left to the builder

- Whether the per-scan loop lives in `prepare_proposal` or a helper beside it.
- The exact progress output format for the per-scan loop.
- Where the S3 helpers live — a new `src/pxs_import/s3.py` is the obvious
  home and keeps `prepare.py` from growing past its current ~280 lines, but
  that is the implementer's call.
- Whether `only` takes a comma-separated string or accepts a list too. `pm`
  only ever passes a string.
- Whether `s3fs` becomes a hard dependency of the `udkm` extra or is imported
  lazily with a clear error. Lazy is preferred for consistency with test 10, but
  the CI constraint in §7 applies either way.
- Measuring and reporting the per-scan process-start overhead (§6.4).
