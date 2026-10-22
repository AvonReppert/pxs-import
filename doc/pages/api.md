# API Reference

## The command

`pxs-prepare` ties everything below into one call: it resolves the source
folders, discovers and numbers the scans, runs nx5d's `pm scan import` with
this package's reader, and migrates the proposal's spice anchors onto the new
run numbers.

```bash
pxs-prepare /path/to/202601_PtAu [--dry-run] [--force] [--single-file]
                                 [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                                 [--source DIR ...] [--base DIR] [--s3]
```

`--help` lists every option; the [How-to](howto.md) explains the routes.

## Calling `pm` directly

The orchestrator only wraps this. Note the order: `-P` and `-u` are global
`pm` options and come **before** `scan import`.

```bash
# one file per scan (default)
pm -P -u 'file:///…/202601_PtAu/scans/{scan}.h5' scan import /…/202601_PtAu \
   -R pxs_import.reader:scans_from_pxs photon_energy=8048.0 \
      mapping_file=/path/to/scan-mapping.csv \
   -W frame_guard=None

# one file per proposal
pm -P -u 'file:///…/202601_PtAu/202601_PtAu.h5#{scan}' scan import /…/202601_PtAu \
   -R pxs_import.reader:scans_from_pxs photon_energy=8048.0 \
      mapping_file=/path/to/scan-mapping.csv \
   -W hdf5 frame_guard=None
```

`-W frame_guard=None` is required because nx5d's writer otherwise reads the
frame-guard attribute off the still-empty target and refuses; the `hdf5` type
hint in the second command is optional (auto-detected from the URL) but kept
explicit. The scan-number → source mapping table is always printed to
`stderr` (in addition to being logged at `INFO` and, if `mapping_file` is
given, written as CSV), since `pm` runs as a subprocess with no logging
configured and would otherwise drop it silently.

## pxs_import.reader

The entry point nx5d resolves by name, and the builder behind it.

::: pxs_import.reader.scans_from_pxs

::: pxs_import.reader.build_scan_h5like

## pxs_import.prepare

::: pxs_import.prepare.prepare_proposal

::: pxs_import.prepare.PrepareResult

## pxs_import.format

Discovery, classification, numbering, and the parsers for everything a scan
folder holds besides its frames.

::: pxs_import.format.discover_scans

::: pxs_import.format.classify_scan

::: pxs_import.format.number_scans

::: pxs_import.format.ScanFolder

::: pxs_import.format.parse_dat

::: pxs_import.format.DatTable

::: pxs_import.format.parse_parameters

::: pxs_import.format.numeric_value

::: pxs_import.format.parse_scan_temperatures

## pxs_import.frames

::: pxs_import.frames.plan_frames

::: pxs_import.frames.FramePlan

::: pxs_import.frames.load_stack

## pxs_import.sources

::: pxs_import.sources.resolve_sources

::: pxs_import.sources.discover_all

## pxs_import.spice

::: pxs_import.spice.migrate_spice

::: pxs_import.spice.SpiceChange

::: pxs_import.spice.fix_recipes
