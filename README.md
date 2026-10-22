# pxs-import

Reads PXS lab-source scan folders of the UDKM group (U. Potsdam) into the
[nx5d](https://gitlab.com/kmc3-xpp/nx5d) repository format.

The plasma X-ray source writes one folder per scan under
`YYYYMMDD/HHMMSS/`: a `parameters*.txt`, a signature `.dat` table naming the
scan type, and a stack of detector TIFFs. `pxs-import` discovers those
folders, classifies them (`sRSM`, `tRSS`, `xRSS`, `alignment`), numbers them
chronologically as `r0001…`, and hands each one to nx5d as an HDF5-like tree
with the acquisition parameters carried along as scan attributes.

## Install

```bash
pip install pxs-import            # the importer
pip install "pxs-import[s3]"      # plus the --s3 upload path
```

Importing a proposal also needs a current [nx5d](https://gitlab.com/kmc3-xpp/nx5d)
on the PATH: `pxs-prepare` drives nx5d's `pm scan import` as a subprocess
rather than importing it.

## Use

```bash
pxs-prepare 202601_PtAu              # discover, import, migrate the spice anchors
pxs-prepare 202601_PtAu --dry-run    # print the scan mapping and stop
pxs-prepare 202601_PtAu --s3         # import straight into the bucket
```

Before the first `--s3` import on a machine, run `pxs-prepare --s3-login`
once (no `proposal_root`) to store the bucket credentials in `~/.aws`, the
way `--s3` and a bare `nx5d.Proposal('s3://...')` both expect to find them:

```bash
pxs-prepare --s3-login
```

It prompts for the key, the endpoint and the region, writes an AWS profile,
and verifies it by listing the bucket -- it is the one mode that takes no
`proposal_root` (`--s3-bucket`/`--s3-profile` to use something other than
the defaults), since it is not importing anything.

The base path of the data share is found from `--source`, from the proposal
folder itself, or from the `UDKM_BASE` environment variable (`PXS_BASE` is
accepted as an alias).

### Running it from inside the proposal folder

`proposal_root` is a path, so it can be `.`. This is the common shape in a
JupyterHub terminal that already starts inside the proposal's own folder:

```bash
cd ~/work/Proposals/202601_PtAu
pxs-prepare . --single-file
```

`proposal_root` names both ends of the job: since it already holds the
`YYYYMMDD/HHMMSS/` measurement days, it is used as the *source* (the
"positional route" -- see [Date range](#date-range)) with no `--source`
needed, and since no `--output-root` is given, it is also the *target*: the
import lands right next to the data it came from. Nothing is written
outside that one folder.

`--single-file` controls the shape of that output, not where it goes: every
scan lands in one `202601_PtAu.h5` with a `#{scan}` group per scan, instead
of the default one `.h5` file per scan under `scans/`. Pick it for a
proposal you intend to open as a single `nx5d.Proposal('file:///.../202601_PtAu.h5#{scan}')`
rather than one file per scan.

(argparse accepts any unambiguous prefix, so `--single` also works -- but
`--single-file` is the documented, non-abbreviated form.)

## Date range

`--from`/`--to` take `YYYY-MM-DD` and select measurement days (`YYYYMMDD`
directories), **both bounds inclusive**. They are applied before any day is
even listed, which is what makes reading from the acquisition-program
folders affordable at all -- those hold every beamtime since 2011, and
classifying every scan folder in them would be tens of thousands of
round-trips over the share.

```bash
# A proposal folder that already holds hand-copied measurement days: the
# date range is optional here, and just narrows an already-small folder
# further.
pxs-prepare 202601_PtAu --from 2026-01-08 --to 2026-01-10

# Reading straight from the acquisition-program folders -- no --source, and
# the proposal folder itself holds no measurement days yet -- makes --from
# mandatory. --to is optional and defaults to today, so this imports
# everything from 2026-01-01 up to now:
pxs-prepare 202601_PtAu --base 'P:\' --from 2026-01-01

# Both bounds given: only that window, inclusive on both ends.
pxs-prepare 202601_PtAu --base 'P:\' --from 2026-01-01 --to 2026-01-31

# Repeatable --source reads several acquisition folders as one chronological
# import; the date range applies to all of them alike.
pxs-prepare 202601_PtAu --source 'P:\PilatusTheta2Theta' --source 'P:\PilatusPumpProbe' \
    --from 2026-01-01 --to 2026-01-31

# Pair a date range with --dry-run to see which scans a real import would
# pick up, without writing anything.
pxs-prepare 202601_PtAu --base 'P:\' --from 2026-01-01 --to 2026-01-10 --dry-run
```

Omitting `--from` while reading from the acquisition-program folders is
refused outright, rather than quietly importing fifteen years of every
proposal ever measured:

```bash
pxs-prepare 202601_PtAu --base 'P:\'
# refusing to import from the acquisition program folders without a date
# range: they hold every beamtime since 2011. Pass --from (and optionally
# --to), or point at a folder that already holds the measurement days.
```

The same applies to `.`: if the current folder does not yet hold any
`YYYYMMDD` folders of its own, `--from` is mandatory and the import reads
from the base path instead (the **base route**) --

```bash
cd ~/work/Proposals/201601_EuTe
pxs-prepare . --from 2016-01-23 --to 2016-01-23
```

-- which is why that route can print lines like this, for measurement days
nowhere near the requested window:

```
20200210: duplicate measurement day, keeping .../PilatusPumpProbe/2020/20200210 and ignoring .../PilatusPumpProbe/20200210
```

That is bookkeeping about the *acquisition-program* folder, not about your
import. One of its program folders (here `PilatusPumpProbe`) happens to hold
the same `YYYYMMDD` day twice -- once as a bare top-level folder, once nested
under a year folder -- which can happen after years of hand-copying and
re-filing on the share. `pxs-prepare` keeps one copy and discards the other
so the day is not counted (or numbered) twice. This dedup runs across the
*whole* program folder before `--from`/`--to` narrows anything down (listing
every year folder is cheap; classifying every scan inside a day is not --
see [Date range](#date-range)), so you will see it on any `--base`/base-route
run touching that program folder, whatever window you actually asked for. It
is almost always two copies of the same measurement, and safe to ignore; if
you are not sure, the two paths it names are both real, so diff them
yourself before trusting either is the complete one.

## How nx5d finds this package

Nothing imports `pxs_import` from nx5d; the two are coupled by strings that
nx5d resolves at runtime. The reader is registered on the import call as

```
pm scan import -R pxs_import.reader:scans_from_pxs …
```

and the cooking recipes a proposal's spice records point at live in
[xraydc](https://gitlab.com/kmc3-xpp/xraydc), the general-purpose Q-space
layer, as `pymod:///xraydc.cooking#sRSM` and friends. This package turns
measurements into an nx5d repository; xraydc turns that repository into
physics.

## Licence

GPLv3 or later.
