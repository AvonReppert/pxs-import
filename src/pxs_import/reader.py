'''Assemble one in-memory HDF5-like scan group for a parsed UDKM scan.

A UDKM scan folder (:mod:`pxs_import.format`), once parsed and planned
(:mod:`pxs_import.frames`), is assembled here into a single
``silx.io.commonh5.File`` shaped the way nx5d's writers expect (one group per
scan, frame-guarded ``index``-aligned datasets, ``k-*`` attributes that nx5d's
listing harvests, plus every other attribute unprefixed -- there is no
``udkm-*`` namespace). This module owns that layout; it does not read
anything from disk itself and knows nothing about nx5d, HDF5-on-disk or image
codecs.
'''

import csv
import json
import logging
import sys
from pathlib import Path

import numpy
from silx.io import commonh5

from pxs_import.format import (DatTable, ScanFolder, discover_scans,
                               parse_scan_temperatures,
                               exposure_seconds, number_scans, numeric_value,
                               parse_dat, parse_date_time, parse_parameters,
                               two_theta_correction)
from pxs_import.frames import FramePlan, load_stack, plan_frames

logger = logging.getLogger(__name__)

#: Verbatim ``.dat`` header column -> target variable name, per scan type
#: (spec Sec 5.8). The trailing profile column (``sRSM``/``tRSS`` only) maps
#: to ``'curve'`` and is read from ``DatTable.profile``, not ``.columns``.
COLUMN_VARIABLES: dict[str, dict[str, str]] = {
    'sRSM': {
        'Loop': 'loop',
        'Theta / deg.': 'theta',
        '2Theta + Corr / deg.': 'ttheta',
        'Crystal': 'crystal_v',
        'Crystal Calib': 'crystal_calib',
        'Rocking Curve': 'curve',
    },
    'tRSS': {
        'Loop_Delay': 'loop_delay',
        'Delay': 'delay',
        'Loop_Theta': 'loop_theta',
        'Theta': 'theta',
        'Crystal [V]': 'crystal_v',
        'Crystal [ph/s]': 'crystal_calib',
        'Pilatus [ph/s]': 'cntrate',
        'Repeats': 'repeats',
        'ImageNumber': 'image_number',
        'Intensities [ph/s]': 'curve',
    },
    'xRSS': {
        'Position set': 'delay_setpoint',
        'Position Real': 'delay',
        'Loop': 'loop',
        'Pilatus': 'counts',
        'Crystal': 'crystal_v',
        'Norm': 'norm',
        'Crystal Calib': 'crystal_calib',
    },
    # 'Loop' here is not a name the file declares: `alignment` headers omit
    # the leading loop-index column entirely, and parse_dat synthesises the
    # name (see _LEADING_LOOP_TYPES in :mod:`pxs_import.format`). Do not go looking for
    # it in a real scan<sfx>.dat header -- it is not there.
    'alignment': {
        'Loop': 'loop',
        'Position': 'position',
        'Pilatus': 'counts',
        'Crystal': 'crystal_v',
        'Norm': 'norm',
    },
}

#: Variables written as ``int64`` rather than the default ``float64`` --
#: unless the column actually contains a non-finite (``NaN``/``inf``) value,
#: in which case the ``int64`` cast is skipped and a warning logged (see
#: :func:`build_scan_h5like`).
INTEGER_VARIABLES = {'loop', 'loop_delay', 'loop_theta', 'repeats', 'image_number'}

#: Target variable name -> group path relative to the scan node. Anything
#: not listed here lands in ``measurement`` (see :func:`_group_of`).
GROUP_OF: dict[str, str] = {
    'theta': 'instrument/motors',
    'ttheta': 'instrument/motors',
    'images': 'instrument/pilatus',
    'curve': 'instrument/pilatus',
    'photon_energy': 'instrument/source',
}

#: Primary scan axis, as a dataset path relative to the scan node, per type.
PRIMARY_PATH = {
    'sRSM': 'instrument/motors/theta',
    'tRSS': 'measurement/delay',
    'xRSS': 'measurement/delay_setpoint',
    # Named `position` for every motor, never after the motor itself: the
    # moved axis varies per scan (Omega, X Linear, Horizontal, ...) and is
    # published as the `k-header:stage` attribute instead, so that the
    # dataset layout stays the same shape whatever was scanned.
    'alignment': 'measurement/position',
}

_FRAME_GUARD = 'instrument/pilatus/images'

#: ``parameters*.txt`` keys written as the raw string even when they parse
#: as a number: both spellings of the HHMMSS scan stem.
#: :func:`~pxs_import.format.numeric_value` already refuses the
#: leading-zero ones (``'085055'``), but not ``'120703'``, which is no more
#: a number than its neighbours.
IDENTIFIER_KEYS = {'File Name', 'Filename'}

#: Attribute names this module owns. They are written after the
#: ``parameters*.txt`` entries, so a future parameters key spelled like one
#: of these cannot displace it -- :func:`build_scan_h5like` logs a warning
#: if that ever happens.
RESERVED_ATTRS = frozenset({'notes', 'photon_energy', 'datetime', 'source',
                            'date', 'time', 'truncated', 'rows', 'frames'})


def _group_of(var: str) -> str:
    '''Group path for a variable name, defaulting to ``'measurement'``.'''
    return GROUP_OF.get(var, 'measurement')


def build_scan_h5like(scan: ScanFolder, table: DatTable, params: dict, plan: FramePlan,
                       stack: numpy.ndarray, missing: numpy.ndarray, *,
                       photon_energy: float = 8048.0) -> commonh5.File:
    '''Build one scan's in-memory HDF5-like group.

    Lays out every column of ``table`` (per :data:`COLUMN_VARIABLES`) plus
    the frame stack, the profile curve, the missing-frame mask and a
    constant ``photon_energy`` array, all trimmed to ``plan.n_frames`` rows
    (spec Sec 6.3). For ``tRSS`` scans, ``ttheta`` is derived rather than read.
    If ``plan.truncated``, every scalar column is additionally written at
    full length under ``measurement_full/<var>_full`` (spec Sec 8.1).

    Args:
        scan: The scan's :class:`~pxs_import.format.ScanFolder`.
        table: The scan's parsed :class:`~pxs_import.format.DatTable`.
        params: Parsed ``parameters*.txt`` dict (``{}`` if the file was
            missing), as returned by
            :func:`~pxs_import.format.parse_parameters`. Every entry
            except ``'Comments'`` becomes a root attribute under its own
            key, whatever that key is -- parsed by
            :func:`~pxs_import.format.numeric_value` where the value
            holds a number, raw string where it does not.
        plan: The scan's :class:`~pxs_import.frames.FramePlan`.
        stack: ``(plan.n_frames, H, W)`` ``float32`` frame stack, as
            returned by :func:`~pxs_import.frames.load_stack`.
        missing: ``(plan.n_frames,)`` boolean missing-frame mask, as
            returned by :func:`~pxs_import.frames.load_stack`.
        photon_energy: Constant photon energy (eV) written both as the
            ``photon_energy`` variable and the ``photon_energy`` root
            attribute. Defaults to the PXS's Cu Ka1 line.

    Returns:
        The assembled scan group.
    '''
    scan_type = scan.scan_type
    n = plan.n_frames
    column_variables = COLUMN_VARIABLES[scan_type]

    root = commonh5.File(mode='w')
    groups: dict[str, commonh5.Group] = {}
    leaf_names: list[str] = []

    def _group(path: str) -> commonh5.Group:
        if path not in groups:
            groups[path] = root.create_group(path)
        return groups[path]

    def _write(var: str, data, *, name: str | None = None,
               group_path: str | None = None) -> None:
        if name is None:
            name = var
        if group_path is None:
            group_path = _group_of(var)
        array = numpy.asarray(data)
        if var in INTEGER_VARIABLES:
            if numpy.isfinite(array).all():
                array = array.astype(numpy.int64)
            else:
                logger.warning('%s: non-finite values in %r, keeping float dtype',
                                scan.source, var)
        _group(group_path).create_dataset(name, data=array)
        leaf_names.append(name)

    # Frame stack, profile curve and missing-frame mask.
    _write('images', stack)
    if table.profile is not None:
        _write('curve', table.profile[:n])
    _write('frame_missing', missing)

    # Constant photon energy, index-aligned.
    _write('photon_energy', numpy.full(n, photon_energy, dtype=numpy.float64))

    # Cryostat temperatures, when this scan has a log and that log's last row
    # claims this scan. Written index-aligned like photon_energy rather than
    # as attributes alone: a constant variable is what an analysis can group
    # and plot against, and `tsample_readback` is already the second entry in
    # the search list `xraydc.cooking.TRSM` walks, so grouping by it needs no
    # change there. Absent is the normal case (most of the roughly 140 surveyed scans) and
    # is silent; a log belonging to a different scan is not, because the row
    # says which scan it belongs to and a temperature we cannot attribute is
    # worse than none.
    tsample = None
    temperatures = parse_scan_temperatures(scan.temperature_path)
    if temperatures is not None:
        ta, tb, log_date, log_time = temperatures
        if (log_date, log_time) == (scan.date, scan.time):
            _write('tcryo_readback', numpy.full(n, ta, dtype=numpy.float64))
            _write('tsample_readback', numpy.full(n, tb, dtype=numpy.float64))
            tsample = tb
        else:
            logger.warning('%s: cryostat log ends at %s/%s, not this scan; '
                            'no temperature written', scan.source,
                            log_date, log_time)

    # Every scalar column of the table, trimmed to plan.n_frames.
    scalar_vars = {var: table.columns[col][:n]
                   for col, var in column_variables.items() if var != 'curve'}

    # tRSS: ttheta is derived, not a column.
    if scan_type == 'tRSS':
        ttheta_full = 2 * table.columns['Theta'] + two_theta_correction(params)
        scalar_vars['ttheta'] = ttheta_full[:n]

    for var, values in scalar_vars.items():
        _write(var, values)

    # Truncated xRSS: preserve the full table alongside the trimmed one.
    if plan.truncated:
        full_vars = {var: table.columns[col]
                     for col, var in column_variables.items() if var != 'curve'}
        if scan_type == 'tRSS':
            full_vars['ttheta'] = ttheta_full

        for var, values in full_vars.items():
            _write(var, values, name=f'{var}_full', group_path='measurement_full')

        root.attrs['truncated'] = True
        root.attrs['rows'] = plan.n_rows
        root.attrs['frames'] = plan.n_frames
        logger.warning('%s: truncated scan, kept %d of %d rows', scan.source,
                        plan.n_frames, plan.n_rows)

    if len(leaf_names) != len(set(leaf_names)):
        dups = sorted({n for n in leaf_names if leaf_names.count(n) > 1})
        raise RuntimeError(f'duplicate leaf names in {scan.source}: {dups}')

    # Root attributes (spec Sec 6.2-6.4).
    notes = params.get('Comments', '')
    root.attrs['k-scan-type'] = scan_type
    root.attrs['k-frame-guard'] = _FRAME_GUARD
    root.attrs['k-primary:0'] = PRIMARY_PATH[scan_type]
    # Deliberately no 'k-header:notes': nx5d shows every k-header item in the
    # listing's `others` column, and the notes are already in `description`.
    root.attrs['k-header:description'] = f'{scan.source} {notes}'.strip()
    if 'Exposure Time' in params:
        root.attrs['k-header:integration'] = exposure_seconds(params['Exposure Time'])
    if tsample is not None:
        # The sample temperature, not the cryo head: it is the one a reader
        # scanning the listing is picking scans by. Bound by name where it
        # was validated, rather than indexed back out of the tuple here --
        # reordering that tuple would otherwise publish the cryo head's
        # temperature as the sample's with nothing failing.
        root.attrs['k-header:tsample'] = tsample

    # `alignment` scans differ from one another only by which motor was
    # moved, and the generic sweep below already writes `Stage`/`Unit` as
    # bare attributes -- which nx5d's listing never shows, since it harvests
    # `k-header:*` and nothing else. So these two keys get a second, prefixed
    # copy: without the motor the listing cannot tell two alignment runs
    # apart, and without the unit the raw `position` values are not
    # comparable between an Omega scan in degrees and a Zaber scan in steps.
    # Written verbatim, no normalisation.
    if scan_type == 'alignment':
        if 'Stage' in params:
            root.attrs['k-header:stage'] = params['Stage']
        else:
            logger.warning('%s: alignment scan without a Stage key; '
                            'the moved motor will not appear in the listing', scan.source)
        if 'Unit' in params:
            root.attrs['k-header:unit'] = params['Unit']

    # Every parameters*.txt entry under its own name, exactly as the
    # acquisition software spelled it, typed where it has an unambiguous
    # number in it and left as the raw string where it does not ('Triangle',
    # a delay vector like '-10:0.5:40, 41:1:200', a path). The key set
    # differs per scan type -- sRSM logs 'Calib'/'File Name'/'Number of
    # Loops', xRSS 'Calibration'/'Filename'/'# of Loops' plus the stage
    # settings, tRSS its Delay/Theta vectors -- so this is deliberately
    # generic rather than a per-type table like COLUMN_VARIABLES: a key no
    # release of this importer has ever seen still reaches the file.
    for key, value in params.items():
        if key == 'Comments':                     # that one is `notes`
            continue
        number = None if key in IDENTIFIER_KEYS else numeric_value(value)
        root.attrs[key] = value if number is None else number

    # This module's own names last, so that they win over a parameters key
    # that is ever spelled the same way. None is today (the real files use
    # 'Date Time', 'ROI', 'File Name' and friends), hence the warning rather
    # than a rename: it should never fire, and it says so if it does.
    reserved = {'notes': notes,
                'photon_energy': float(photon_energy),
                'source': scan.source,
                'date': scan.date,
                'time': scan.time}
    if 'Date Time' in params:
        reserved['datetime'] = parse_date_time(params['Date Time'])
    assert reserved.keys() <= RESERVED_ATTRS
    for name, value in reserved.items():
        if name in root.attrs:
            logger.warning('%s: parameters key %r displaced by this importer',
                            scan.source, name)
        root.attrs[name] = value

    return root


def write_mapping_table(mapping: list[tuple[str, ScanFolder]], path: Path) -> None:
    '''Write a scan-number -> source mapping table as CSV.

    Args:
        mapping: ``(scan_name, ScanFolder)`` pairs, as returned by
            :func:`~pxs_import.format.number_scans`.
        path: Output CSV path. Written with ``newline=''`` and ``utf-8``
            encoding, as :mod:`csv` requires.
    '''
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['scan', 'source', 'type', 'root'])
        for scan, folder in mapping:
            writer.writerow([scan, folder.source, folder.scan_type,
                             '' if folder.root is None else str(folder.root)])


def read_mapping_table(path: Path) -> dict[str, str]:
    '''Read back a mapping table written by :func:`write_mapping_table`.

    The table exists so that a re-import can tell whether a scan's number
    moved (:func:`renumbered`); it is the only durable record of the previous
    numbering that exists for a local import, since the S3 manifest is only
    written under ``--s3``.

    Args:
        path: The CSV. A missing file is not an error -- a first import has
            nothing to disagree with.

    Returns:
        ``source -> scan key``. Tables written before the ``root`` column
        existed are read the same way; the column is not needed here.
    '''
    if not Path(path).is_file():
        return {}
    with open(path, newline='', encoding='utf-8') as f:
        return {row['source']: row['scan'] for row in csv.DictReader(f)}


def renumbered(previous: dict[str, str],
                mapping: list[tuple[str, ScanFolder]]) -> list[tuple[str, str, str]]:
    '''Find scans whose number changed since the previous import.

    Numbering is chronological across every source, so a scan that turns up
    *between* two already-imported ones shifts every number after it. Spice
    anchors point at numbers, so this silently re-points analysis at the
    wrong data -- the one failure this check exists to make loud.

    Args:
        previous: ``source -> scan key`` from :func:`read_mapping_table`.
        mapping: The numbering about to be written.

    Returns:
        ``(source, old key, new key)`` per moved scan, in the new numbering's
        order. Scans absent from ``previous`` are new, not moved.
    '''
    return [(folder.source, previous[folder.source], scan)
            for scan, folder in mapping
            if folder.source in previous and previous[folder.source] != scan]


def format_mapping_table(mapping: list[tuple[str, ScanFolder]]) -> str:
    '''Format a scan-number -> source mapping table for logging.

    Args:
        mapping: ``(scan_name, ScanFolder)`` pairs, as returned by
            :func:`~pxs_import.format.number_scans`.

    Returns:
        One ``'<scan> <- <source> (<type>)'`` line per entry, joined by
        ``'\\n'``, with a trailing ``' [<root>]'`` wherever ``folder.root``
        is set -- the CSV :func:`write_mapping_table` writes always carries a
        ``root`` column, and without it here two scans sharing one
        ``YYYYMMDD/HHMMSS`` from different acquisition folders print as the
        same line, with nothing in the table a user actually reads to tell
        them apart.
    '''
    return '\n'.join(f'{scan} <- {folder.source} ({folder.scan_type})'
                      + ('' if folder.root is None else f' [{folder.root}]')
                      for scan, folder in mapping)


def _mapping_from_resolved(resolved: str | list) -> list[tuple[str, ScanFolder]]:
    '''Rebuild a scan mapping that the orchestrator already worked out.

    Args:
        resolved: ``[scan_key, folder_path, scan_type]`` triples, in the
            order the scans should be yielded -- either as a JSON string or
            already decoded. Both forms are accepted because ``pm`` parses
            its ``key=value`` arguments itself and hands the reader the
            decoded value, while a direct caller naturally passes the string
            it would have put on the command line.

    Returns:
        ``(scan_key, ScanFolder)`` pairs, as :func:`number_scans` returns.

    Raises:
        RuntimeError: A named folder does not exist. The orchestrator listed
            it moments ago, so this means the source moved or a share
            dropped -- worth naming rather than failing later on a missing
            ``.dat``.
    '''
    triples = json.loads(resolved) if isinstance(resolved, (str, bytes, bytearray)) else resolved
    mapping = []
    for scan_key, folder, scan_type in triples:
        path = Path(folder)
        if not path.is_dir():
            raise RuntimeError(f'{scan_key}: resolved scan folder is gone: {path}')
        mapping.append((scan_key, ScanFolder(path, path.parent.name, path.name, scan_type)))
    return mapping


def scans_from_pxs(source: str | Path, *, photon_energy: float = 8048.0,
                     scan_format: str = 'r{iscan:04d}',
                     mapping_file: str | Path | None = None,
                     only: str | None = None,
                     resolved: str | list | None = None,
                     resolved_file: str | Path | None = None):
    '''Generate one in-memory scan per UDKM scan folder under ``source``.

    Intended as the ``-R`` reader target for nx5d's ``pm scan import`` CLI
    (``pxs_import.reader:scans_from_pxs``). Discovers and numbers every scan
    under ``source`` (:func:`~pxs_import.format.discover_scans`,
    :func:`~pxs_import.format.number_scans`), logs the resulting mapping
    once at ``INFO``, prints the same table to ``stderr`` (``pm`` runs as an
    nx5d subprocess with no logging configured, so the ``INFO`` line is
    otherwise invisible) and, if ``mapping_file`` is given, writes it there
    too (:func:`write_mapping_table`). Then, per scan in numbering order:
    parses the ``.dat`` table, plans and loads its frames, parses its
    parameters file and assembles the scan with :func:`build_scan_h5like`.
    Any exception raised while processing one scan is re-raised as a
    ``RuntimeError`` naming that scan's number, source and type (fail-fast --
    no scan is ever skipped).

    Args:
        source: Proposal root or single date directory, as accepted by
            :func:`~pxs_import.format.discover_scans`. May be a ``str``
            (as ``pm`` passes) or a ``Path``.
        photon_energy: Forwarded to :func:`build_scan_h5like` for every scan.
        scan_format: Forwarded to
            :func:`~pxs_import.format.number_scans`.
        mapping_file: If given, the mapping table is also written there as
            CSV (:func:`write_mapping_table`).
        only: If given, a single scan key (``'r0007'``) or a comma-separated
            list of them (``'r0001,r0003'``); only those scans are parsed,
            loaded and yielded, in numbering order. The filter is applied
            right after numbering, *before* :func:`parse_dat` /
            :func:`load_stack`, so unselected scans cost a directory listing
            rather than an image read -- this is what makes one ``pm scan
            import`` invocation per scan affordable. ``mapping_file`` is
            still written in full, and the mapping table is not logged or
            printed. ``iscan`` counts positions in whatever ``mapping`` this
            call ends up with, which is the *full* mapping only when
            ``resolved``/``resolved_file`` are also absent: under ``--s3``,
            :func:`~pxs_import.prepare._s3_import_loop` passes a one-entry
            ``resolved`` alongside ``only``, so ``mapping`` there already
            holds just that one scan and ``iscan`` is always ``1``, not that
            scan's position in the proposal.
        resolved: A scan mapping the caller already worked out, as a JSON
            array of ``[scan_key, folder_path, scan_type]`` triples. When
            given, this function does **no** discovery, no numbering and no
            date filtering, and ``source`` is ignored -- under the
            multi-source routes it is the data-free workspace, not a place to
            look for scans.

            It exists because numbering must have exactly one authority. The
            orchestrator has to discover and number anyway, to write the
            mapping table and to drive its per-scan S3 loop; letting this
            function walk again would mean two walks seconds apart over an
            archive the measurement PC may be writing into, and under
            ``--s3`` one full re-walk per scan. ``only`` still filters, and
            is simply unnecessary when ``resolved`` already names one scan.

            JSON rather than a delimited string because the paths are Windows
            ones: backslashes, drive letters and UNC prefixes. Note that a
            source path containing ``=`` cannot travel this way at all: nx5d
            splits ``key=value`` without a maxsplit.
        resolved_file: A file holding that same JSON, which wins over
            ``resolved`` when both are given. This is how the orchestrator
            passes a whole proposal: one argv element cannot hold 134 UNC
            paths and stay under the Windows command line's 32767-character
            ceiling.

    Yields:
        ``(key, h5like)`` pairs, in numbering order, where ``key`` is
        ``{'scan': scan_name, 'iscan': 1-based index, 'udkm_source':
        scan.source, 'scan_type': scan.scan_type}`` and ``h5like`` is the
        scan's assembled ``silx.io.commonh5.File``. No reference to a
        yielded ``h5like`` is retained after it is yielded.

    Raises:
        ValueError: ``only`` names a scan key this proposal does not have;
            the message lists the unknown keys and every valid one. Raised
            before any scan is parsed.
        RuntimeError: Any exception while parsing/planning/building one
            scan, wrapped with that scan's number, source and type, with
            the original exception chained as ``__cause__``.
    '''
    if resolved_file is not None:
        mapping = _mapping_from_resolved(Path(resolved_file).read_text(encoding='utf-8'))
    elif resolved is not None:
        mapping = _mapping_from_resolved(resolved)
    else:
        scans = discover_scans(Path(source))
        mapping = number_scans(scans, scan_format=scan_format)

    if only is None:
        logger.info('%s', format_mapping_table(mapping))
        print(format_mapping_table(mapping), file=sys.stderr)
    if mapping_file is not None:
        write_mapping_table(mapping, Path(mapping_file))

    selected = None
    if only is not None:
        selected = [key.strip() for key in only.split(',') if key.strip()]
        known = {scan_name for scan_name, _ in mapping}
        unknown = [key for key in selected if key not in known]
        if unknown:
            raise ValueError(
                f'{source}: unknown scan key(s): {", ".join(unknown)}; '
                f'this proposal has {", ".join(sorted(known))}')

    for iscan, (scan_name, folder) in enumerate(mapping, start=1):
        if selected is not None and scan_name not in selected:
            continue
        try:
            table = parse_dat(folder.dat_path, folder.scan_type)
            plan = plan_frames(folder.path, table, folder.scan_type)
            stack, missing = load_stack(plan)
            params = parse_parameters(folder.params_path)
            h5like = build_scan_h5like(folder, table, params, plan, stack, missing,
                                        photon_energy=photon_energy)
        except Exception as e:
            raise RuntimeError(f'{scan_name} ({folder.source}, {folder.scan_type}): {e}') from e
        key = {'scan': scan_name, 'iscan': iscan, 'udkm_source': folder.source,
               'scan_type': folder.scan_type}
        yield key, h5like
        del h5like, stack, missing, table, plan
