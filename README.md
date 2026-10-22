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

The base path of the data share is found from `--source`, from the proposal
folder itself, or from the `UDKM_BASE` environment variable (`PXS_BASE` is
accepted as an alias).

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
