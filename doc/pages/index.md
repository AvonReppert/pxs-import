# pxs-import

Reads PXS lab-source scan folders of the UDKM group (U. Potsdam) into the
[nx5d](https://gitlab.com/kmc3-xpp/nx5d) repository format.

## Overview

The plasma X-ray source writes one folder per scan under `YYYYMMDD/HHMMSS/`:
a `parameters*.txt`, a signature `.dat` table whose name says what kind of
scan it was, and a stack of detector TIFFs. `pxs-import`:

- **discovers** those folders under a proposal root, a date range, or
  several acquisition-program folders at once;
- **classifies** each one as `sRSM`, `tRSS`, `xRSS` or `alignment` by which
  signature file it holds;
- **numbers** them chronologically across every source as `r0001`, `r0002`, …;
- **builds** an HDF5-like tree per scan — the frame stack, the table's
  columns, and every acquisition parameter as a scan attribute — for nx5d's
  `pm scan import` to write out;
- **migrates** the proposal's spice anchors from date/time onto the new run
  numbers.

It does no physics. Turning the resulting repository into reciprocal-space
maps is [xraydc](https://gitlab.com/kmc3-xpp/xraydc)'s job.

## Installation

```bash
pip install pxs-import          # the importer
pip install "pxs-import[s3]"    # plus the --s3 upload path
```

Importing also needs a current nx5d on the PATH: `pxs-prepare` drives its
`pm scan import` as a subprocess rather than importing it.

## Quick start

```bash
pxs-prepare D:\proposals\202601_PtAu --dry-run   # print the mapping, change nothing
pxs-prepare D:\proposals\202601_PtAu             # import
```

```python
from nx5d import Proposal

root = 'file:///D:/proposals/202601_PtAu'
prop = Proposal(url=f'{root}/scans/{{scan}}.h5', spice=f'{root}/spice')
prop.ls()
prop.r0006.cooked      # Q-mapped through the xraydc recipe in the spice record
```

The [How-to](howto.md) walks through a whole proposal, including the
JupyterHub and S3 routes. See the [API Reference](api.md) for the functions
behind the command.
