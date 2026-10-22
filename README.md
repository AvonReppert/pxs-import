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
