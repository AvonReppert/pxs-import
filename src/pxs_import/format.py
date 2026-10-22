'''UDKM plasma-X-ray-source raw data layout: discovery, classification and numbering.

Raw data lives in folders ``<proposal>/YYYYMMDD/HHMMSS/``, each holding one
``.dat`` table, a ``parameters<HHMMSS>.txt`` file and TIFF frames. This module
only knows about the folder layout -- it never opens a file beyond checking
that a signature file exists. Parsing is added by later tasks.

Discovery works by directory name only (``^\\d{8}$`` date directories,
``^\\d{6}$`` scan directories inside them); it never globs for ``*.dat`` or
``*/*/``. Real proposal roots contain debris alongside the date folders --
zip files, ``<time>Graphics`` directories, a ``spice`` directory -- which is
skipped silently because it simply does not match those patterns.
'''

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy

logger = logging.getLogger(__name__)

#: Scan types whose ``.dat`` table carries a trailing profile column
#: (``Rocking Curve`` / ``Intensities [ph/s]``) and per-row ``%`` metadata
#: lines (``StartTime``/``EndTime``/``ImageName``). ``xRSS`` has neither.
_PROFILE_SCAN_TYPES = ('sRSM', 'tRSS')

#: Scan types whose ``.dat`` header declares one name fewer than its rows
#: carry fields, because the leading loop-index column is left unnamed.
#: ``alignment`` writes ``'% Position\tPilatus\tCrystal\tNorm'`` above rows
#: of five fields; ``xRSS`` by contrast names its ``Loop`` column properly.
#: Declared here per type rather than inferred from the field counts at
#: runtime: a table whose last row is half-written would otherwise look
#: like a table with one column fewer, and every column would silently
#: shift by one.
_LEADING_LOOP_TYPES: tuple[str, ...] = ('alignment',)

#: Scan-type signature filenames, keyed by scan type. ``{sfx}`` is replaced
#: with the scan folder's ``HHMMSS`` name. Folders also contain
#: ``intensities<sfx>.dat`` / ``averages<sfx>.dat``, which are not signatures
#: of any scan type -- they coexist with one of the names below. The same
#: goes for ``averages<sfx>.dat``, which ``xRSS`` and ``alignment`` folders
#: both carry; only the names below decide a type.
#:
#: Note ``tRSS``'s ``scans`` against ``alignment``'s ``scan``: one character
#: apart, and the only reason they cannot be confused is that
#: :func:`classify_scan` builds each full filename and asks the filesystem
#: for it, rather than matching a prefix.
SIGNATURES: dict[str, str] = {
    'sRSM': 'rockingcurves{sfx}.dat',
    'tRSS': 'scans{sfx}.dat',
    'xRSS': 'allSteps{sfx}.dat',
    'alignment': 'scan{sfx}.dat',
}

_DATE_RE = re.compile(r'^\d{8}$')
_TIME_RE = re.compile(r'^\d{6}$')

#: Some acquisition programs file their measurement days under a year
#: directory and some do not, and the practice changed over time:
#: ``PilatusTheta2Theta\20250408\`` but ``PilatusTheta2Theta\2026\20260108\``.
#: The year is **not** derivable from the dates inside it -- three real
#: measurement days from January 2026 sit under ``PilatusPumpProbe\2025\`` --
#: so it is enumerated, never constructed.
_YEAR_RE = re.compile(r'^\d{4}$')


@dataclass(frozen=True)
class ScanFolder:
    '''One ``<proposal>/YYYYMMDD/HHMMSS/`` scan folder.

    Args:
        path: Absolute path to the scan folder.
        date: The ``YYYYMMDD`` folder name.
        time: The ``HHMMSS`` folder name.
        scan_type: Scan type as returned by :func:`classify_scan`.
        root: The source directory this scan was discovered under, when the
            caller tracks several. Defaulted, so the four positional fields
            remain the whole constructor for every existing call site. It
            exists because a scan's identity is ``YYYYMMDD/HHMMSS``, which
            does not say which acquisition program's folder it came from --
            and with four of them feeding one proposal, a duplicate report
            or a re-import check has no other way to tell them apart.
    '''

    path: Path
    date: str
    time: str
    scan_type: str
    root: Path | None = None

    @property
    def source(self) -> str:
        '''``'YYYYMMDD/HHMMSS'`` identifier for this scan.'''
        return f'{self.date}/{self.time}'

    @property
    def sfx(self) -> str:
        '''The signature-filename suffix, i.e. ``time``.'''
        return self.time

    @property
    def dat_path(self) -> Path:
        '''Path to this scan's signature ``.dat`` table.'''
        return self.path / SIGNATURES[self.scan_type].format(sfx=self.sfx)

    @property
    def params_path(self) -> Path:
        '''Path to this scan's ``parameters<sfx>.txt`` file.'''
        return self.path / f'parameters{self.sfx}.txt'

    @property
    def temperature_path(self) -> Path:
        '''Path to this scan's ``scan data<sfx>.txt`` cryostat log.

        Note the space in the name. The file is optional and mostly absent --
        6 of 130 folders in the surveyed corpus have one.
        '''
        return self.path / f'scan data{self.sfx}.txt'


def classify_scan(folder: Path) -> str | None:
    '''Classify a scan folder by which signature file it contains.

    Args:
        folder: A ``HHMMSS`` scan folder.

    Returns:
        The scan type (a key of :data:`SIGNATURES`), or ``None`` if the
        folder contains none of the signature files.
    '''
    sfx = folder.name
    for scan_type, pattern in SIGNATURES.items():
        if (folder / pattern.format(sfx=sfx)).is_file():
            return scan_type
    return None


def _listdir(path: Path, lister) -> list[Path]:
    '''List one directory, naming it if the listing fails.

    A source may sit on a network share that drops mid-walk, and a bare
    ``PermissionError`` out of a comprehension does not say which of the
    hundreds of directories it was.

    Args:
        path: Directory to list.
        lister: Callable taking a :class:`~pathlib.Path` and returning an
            iterable of paths; ``None`` means :meth:`~pathlib.Path.iterdir`.

    Returns:
        The directory's entries, sorted.

    Raises:
        OSError: Re-raised from ``lister``, with ``path`` in the message.
    '''
    try:
        return sorted(lister(path) if lister is not None else path.iterdir())
    except OSError as e:
        raise OSError(f'{path}: cannot list directory: {e}') from e


def discover_scans(proposal_root: Path, *, start: str | None = None,
                    end: str | None = None, lister=None) -> list[ScanFolder]:
    '''Find and classify every scan folder under a proposal root.

    Iterates ``proposal_root``'s children, keeping only those matching
    ``^\\d{8}$`` (date directories), then each date directory's children
    matching ``^\\d{6}$`` (scan directories). Anything else -- zip files,
    ``<time>Graphics`` directories, a ``spice`` directory, etc. -- is skipped
    silently, since it simply never matches those two patterns.

    A child matching ``^\\d{4}$`` is a year directory, and its own
    ``^\\d{8}$`` children count as measurement days too -- some acquisition
    programs file their days that way and some do not (:data:`_YEAR_RE`).
    This descent is **one level deep and never recursive**: a year inside a
    year is not a layout that exists, and treating it as one would turn
    discovery into a walk of the whole archive.

    If ``proposal_root.name`` itself matches ``^\\d{8}$``, it is treated as a
    single date directory instead: its direct children are scanned for scan
    directories, and no further iteration of sibling date directories happens.

    Each matching scan directory is classified with :func:`classify_scan`;
    one with no signature file is skipped with a logged warning naming it as
    ``YYYYMMDD/HHMMSS``.

    ``start``/``end`` select measurement days, and are applied **before any
    day is descended into**. That ordering is the whole cost argument: the
    program folders hold every beamtime since 2011, and classifying their
    scan folders is tens of thousands of round-trips over a share. Listing
    the root and its year directories is bounded by the number of years;
    everything expensive happens only for days the caller asked for.

    Args:
        proposal_root: Proposal root directory containing date directories,
            year directories holding date directories, or a single date
            directory itself.
        start: Earliest measurement day to keep, as ``YYYYMMDD``, inclusive.
            ``None`` means no lower bound.
        end: Latest measurement day to keep, as ``YYYYMMDD``, inclusive.
            ``None`` means no upper bound -- deliberately not "today", so
            that a direct caller gets nothing clock-dependent it did not ask
            for. The CLI substitutes today when ``--to`` is omitted.
        lister: Callable taking a directory and returning an iterable of its
            entries; defaults to :meth:`~pathlib.Path.iterdir`. It exists so
            that a test can count which directories were opened, which is the
            only way to pin the cost property above.

    Returns:
        Scan folders sorted by ``date + time``. Days are sorted by their own
        name rather than by where they were found: a year directory's name
        does not have to agree with the dates inside it, so ordering by
        discovery would interleave two year directories wrongly.
    '''
    if _DATE_RE.match(proposal_root.name):
        date_dirs = [proposal_root]
    else:
        date_dirs = []
        for child in _listdir(proposal_root, lister):
            if not child.is_dir():
                continue
            if _DATE_RE.match(child.name):
                date_dirs.append(child)
            elif _YEAR_RE.match(child.name):
                date_dirs += [d for d in _listdir(child, lister)
                              if d.is_dir() and _DATE_RE.match(d.name)]
        date_dirs.sort(key=lambda d: d.name)

    # Before descending, not after: see the cost note in the docstring.
    if start is not None:
        date_dirs = [d for d in date_dirs if d.name >= start]
    if end is not None:
        date_dirs = [d for d in date_dirs if d.name <= end]

    scans = []
    for date_dir in date_dirs:
        for scan_dir in _listdir(date_dir, lister):
            if not (scan_dir.is_dir() and _TIME_RE.match(scan_dir.name)):
                continue
            scan_type = classify_scan(scan_dir)
            if scan_type is None:
                logger.warning('unclassifiable scan folder: %s/%s',
                                date_dir.name, scan_dir.name)
                continue
            scans.append(ScanFolder(scan_dir, date_dir.name, scan_dir.name, scan_type,
                                     root=proposal_root))
    return scans


def number_scans(scans: list[ScanFolder],
                  scan_format: str = 'r{iscan:04d}') -> list[tuple[str, ScanFolder]]:
    '''Assign sequential scan numbers to a list of scan folders.

    Args:
        scans: Scan folders, in the order they should be numbered.
        scan_format: ``str.format`` template taking an ``iscan`` keyword,
            1-based.

    Returns:
        ``[(scan_format.format(iscan=1), scans[0]), ...]``, same order as
        ``scans``.
    '''
    return [(scan_format.format(iscan=iscan), sf) for iscan, sf in enumerate(scans, start=1)]


def _decode_line(raw: bytes) -> str:
    '''Decode one line read in binary mode.

    Tries ``utf-8`` first and falls back to ``windows-1252`` -- real UDKM
    files are almost all Windows-1252 and contain characters like
    ``²``/``µ``/``°`` that must survive. Shared by :func:`parse_parameters`
    and :func:`parse_dat`.

    Args:
        raw: One line's raw bytes, as read from a binary file handle.

    Returns:
        The decoded line.
    '''
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('windows-1252')


def parse_parameters(path: Path) -> dict[str, str]:
    '''Parse a ``parameters<HHMMSS>.txt`` file into a flat string dict.

    The file is free text: unkeyed leading lines (sample description,
    fluence, ...) followed by ``Key: value`` lines. Lines are read in binary
    and decoded one at a time, trying ``utf-8`` first and falling back to
    ``windows-1252`` -- real files are almost all Windows-1252 and contain
    characters like ``²``/``µ`` that must survive.

    Each line is split with ``split(': ', 1)``, so only the first ``': '``
    separates key from value (a value may itself contain ``': '``, as in
    ``'Note: a: b'``). Empty lines are skipped and do not reset state: before
    the first key, unkeyed non-empty lines accumulate as ``'Comments'``;
    once a key has been seen, a later unkeyed non-empty line is appended to
    the *previous* key's value with a single space (mirrors nx5d's reader;
    never triggers on real data).

    Args:
        path: Path to the ``parameters*.txt`` file.

    Returns:
        Dict of key -> value, plus ``'Comments'`` (``''`` if there were no
        leading comment lines). Returns ``{}`` if ``path`` does not exist,
        after logging a warning naming the file.
    '''
    if not path.is_file():
        logger.warning('missing parameters file: %s', path)
        return {}

    comments = []
    result: dict[str, str] = {}
    last_key = None
    with path.open('rb') as f:
        for raw_line in f:
            txt = _decode_line(raw_line).strip()
            if not txt:
                continue
            parts = txt.split(': ', 1)
            if len(parts) == 2:
                last_key, value = parts
                result[last_key] = value
            elif last_key is None:
                comments.append(txt)
            else:
                result[last_key] += ' ' + txt

    result['Comments'] = ' '.join(comments)
    return result


#: Trailing unit suffixes stripped before a numeric parse (see
#: :func:`numeric_value`). The degree sign rides on sRSM angles
#: (``'17.000000°'``) but not always on the same key, and the
#: ``'s'`` on ``Exposure Time`` is likewise optional.
_UNIT_SUFFIXES = ('°', 's')

#: ``'(242, 50); (254, 87)'`` -- the ROI / Pump Region / Norm Region shape.
_REGION_RE = re.compile(r'^\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*;'
                        r'\s*\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)$')

#: An integer, used to choose ``int`` over ``float``. A leading zero means
#: the field is an identifier (``'085055'``), not a count, so it is excluded.
_INT_RE = re.compile(r'^[+-]?(0|[1-9]\d*)$')

#: Any run of digits. What this matches but :data:`_INT_RE` does not is
#: exactly a leading-zero integer, which never gets a numeric reading.
_DIGITS_RE = re.compile(r'^[+-]?\d+$')


def numeric_value(value: str):
    '''Interpret one ``parameters*.txt`` value as a number, if it is one.

    Deliberately conservative: it recognises the four shapes the UDKM
    acquisition software actually writes and returns ``None`` for everything
    else, so the caller can fall back to the raw string. The shapes, in the
    order they are tried:

    - a region, ``'(242, 50); (254, 87)'`` -> four ``int``;
    - tab-separated numbers, as ``Calib``/``Calibration`` write four of
      them -> a list of ``float``;
    - a bare integer -> ``int``, unless it has a leading zero, which marks
      an identifier such as the ``'085055'`` scan stem rather than a count;
    - a decimal, optionally carrying one of :data:`_UNIT_SUFFIXES`
      (``'17.000000°'``, ``'2.000000s'``) -> ``float``.

    Anything else -- ``'Triangle'``, ``'ps'``, ``'PXS 600mm'``, a delay
    vector like ``'-10:0.5:40, 41:1:200'``, a path, a ``Date Time`` -- has
    no numeric reading and yields ``None``.

    Args:
        value: One parsed parameter value, as
            :func:`parse_parameters` returns it.

    Returns:
        ``int``, ``float``, a ``list`` of either, or ``None`` when the value
        has no unambiguous numeric reading.
    '''
    txt = value.strip()
    if not txt:
        return None

    region = _REGION_RE.match(txt)
    if region:
        return [int(g) for g in region.groups()]

    if '	' in txt:
        try:
            return [float(f) for f in txt.split('	')]
        except ValueError:
            return None

    for suffix in ('',) + _UNIT_SUFFIXES:
        if suffix and not txt.endswith(suffix):
            continue
        body = txt[:-len(suffix)].strip() if suffix else txt
        if _INT_RE.match(body):
            return int(body)
        if _DIGITS_RE.match(body):
            return None          # leading-zero integer: an identifier, not a number
        try:
            return float(body)
        except ValueError:
            continue
    return None


def parse_scan_temperatures(path: Path) -> tuple[float, float, str, str] | None:
    '''Read a scan's cryostat temperatures from its ``scan data<sfx>.txt``.

    The file is a running log of a whole cooldown -- 161 rows spanning four
    days in the surveyed example -- copied into every scan folder and one
    line longer each time. The **last** row is the scan's own, and it says
    so: it carries the scan's own ``Day`` and ``Time``, which is what lets a
    caller verify the association instead of trusting the position.

    Two quirks of the real files are load-bearing:

    - The header is tab-separated (``% TA []\\tTB [K]\\tDay\\tTime``) but the
      data rows are **space**-separated with one trailing tab. Splitting a
      row on ``'\\t'`` yields two fields, not four, and would read the
      temperatures as one string.
    - ``TA`` is declared with no unit (``TA []``) although it is Kelvin, as
      ``TB [K]`` is. Neither is converted; both are carried as written.

    Args:
        path: The log file. Absent is the normal case and not an error.

    The row is stamped at the scan's *start* -- each row carries the owning
    scan's own ``HHMMSS``. In the surveyed cooldown consecutive rows lie
    about 31 minutes and 3 K apart, so the cryostat is ramping throughout a
    scan and this is a start-of-scan reading, not a scan average. It is
    carried unchanged; an analyst reading it as a per-frame readback would be
    misled by roughly that much.

    Returns:
        ``(TA, TB, day, time)`` from the last data row -- ``TA`` at the cryo
        head, ``TB`` near the sample, both Kelvin; ``day`` as ``YYYYMMDD``
        and ``time`` as ``HHMMSS``, as that row recorded them. ``None``, with
        a warning naming the file, whenever it cannot be read that way: the
        file is absent, holds no data row, its last row is short, or its
        temperatures are not numbers. This is an optional enrichment and
        degrades rather than failing.
    '''
    if not path.is_file():
        return None
    with path.open('rb') as f:
        lines = [_decode_line(raw).strip() for raw in f.read().split(b'\n')]
    rows = [line for line in lines if line and not line.startswith('%')]
    if not rows:
        logger.warning('%s: cryostat log has no data rows', path)
        return None
    fields = rows[-1].split()
    if len(fields) < 4:
        logger.warning('%s: last cryostat row has %d field(s), expected 4: %r',
                        path, len(fields), rows[-1])
        return None
    try:
        ta, tb = float(fields[0]), float(fields[1])
    except ValueError:
        # A controller error token ('OVER', '----'), a comma decimal
        # separator from a German-locale build, or an interleaved concurrent
        # append. This file is an optional extra present in a handful of
        # folders; letting it raise would abort the whole proposal import
        # with a message about float(), which is both fatal and undiagnosable.
        logger.warning('%s: last cryostat row has non-numeric temperatures: %r',
                        path, rows[-1])
        return None
    return ta, tb, fields[2], fields[3]


def exposure_seconds(value: str) -> float:
    '''Parse an ``Exposure Time`` value into seconds.

    Args:
        value: e.g. ``'2.000000s'`` or a bare number ``'1.000000'``.

    Returns:
        The value as a float, with a trailing ``'s'`` unit stripped.
    '''
    return float(value.rstrip('s'))


def two_theta_correction(params: dict) -> float:
    '''Read the ``'2 Theta Correction'`` entry from parsed parameters.

    Args:
        params: Dict as returned by :func:`parse_parameters`.

    Returns:
        The correction as a float, defaulting to ``0.0`` if absent.
    '''
    return float(params.get('2 Theta Correction', '0'))


def parse_date_time(value: str) -> str:
    '''Parse a ``Date Time`` value into an ISO-8601 string.

    Args:
        value: e.g. ``'08.01.2026 12:08:21'``.

    Returns:
        ISO-8601 string, e.g. ``'2026-01-08T12:08:21'``.
    '''
    return datetime.strptime(value, '%d.%m.%Y %H:%M:%S').isoformat()


@dataclass
class DatTable:
    '''Parsed contents of a scan's signature ``.dat`` table.

    Args:
        columns: Scalar columns, verbatim declared header name -> ``(N,)``
            ``float64`` array. For ``sRSM``/``tRSS`` the trailing profile
            column is excluded here; it lives in :attr:`profile` instead.
        profile: ``(N, W)`` ``float64`` array holding the per-row profile
            (rocking curve / intensities), or ``None`` for ``xRSS``, which
            has no profile column.
        image_names: Per-row ``ImageName`` metadata, or ``None`` for ``xRSS``.
        start_times: Per-row ``StartTime`` metadata, or ``None`` for ``xRSS``.
        end_times: Per-row ``EndTime`` metadata, or ``None`` for ``xRSS``.
    '''

    columns: dict[str, numpy.ndarray]
    profile: numpy.ndarray | None
    image_names: list[str] | None
    start_times: list[str] | None
    end_times: list[str] | None

    @property
    def n_rows(self) -> int:
        '''Number of surviving data rows.'''
        return len(next(iter(self.columns.values())))


def parse_dat(path: Path, scan_type: str) -> DatTable:
    '''Parse a scan's signature ``.dat`` table.

    Read in binary and decoded line by line with :func:`_decode_line`
    (``utf-8`` falling back to ``windows-1252``), so a header or metadata
    line containing a non-ASCII character (e.g. ``°``) does not raise.

    The table is tab-separated. Its first ``%`` line is the header: strip
    the leading ``'% '`` and split on ``'\\t'`` to get the declared column
    names. For ``sRSM``/``tRSS`` the last declared column is a profile, and
    a second ``%`` line starting with ``StartTime`` (present for ``sRSM``,
    absent for ``tRSS``) is a metadata sub-header and is skipped; every other
    ``%`` line carries the ``(StartTime, EndTime, ImageName)`` metadata for
    the *next* data row. ``xRSS`` has no profile and no per-row ``%`` lines
    past the header.

    The expected field count for a well-formed data row is the *maximum*
    field count seen anywhere in the file: a corrupt/truncated row is only
    ever shorter than a good one, never longer, so trusting any single row
    (in particular the first) would let a truncated row of its own poison
    every later, correctly-sized row instead of being dropped itself. A row
    with fewer fields than that maximum is dropped, with a warning naming
    the 1-based data-row number. ``NaN`` fields parse as ``NaN``.

    For a type in :data:`_LEADING_LOOP_TYPES` the header is one name short:
    the name ``'Loop'`` is synthesised for the undeclared leading column
    before any of the above applies, so the table has as many named columns
    as its rows have fields.

    Args:
        path: Path to the ``.dat`` file.
        scan_type: One of ``'sRSM'``, ``'tRSS'``, ``'xRSS'``, ``'alignment'``.

    Returns:
        The parsed table.

    Raises:
        ValueError: The file holds a header but no data row at all -- an
            acquisition stopped before its first exposure. Such a scan
            carries no data and nothing downstream can do with it: before
            this guard existed it still failed, but two layers further on
            and as ``load_stack``'s "no existing frames to load", which
            names the folder rather than the file and reads like a missing
            TIFF rather than an empty table. Rejected here instead, naming
            the file. (``DatTable.n_rows`` itself is fine with it -- it
            returns ``0`` off a dict of empty arrays.)
    '''
    has_profile = scan_type in _PROFILE_SCAN_TYPES

    with path.open('rb') as f:
        raw_lines = f.read().split(b'\n')
    lines = [_decode_line(raw).rstrip('\r') for raw in raw_lines if raw.rstrip(b'\r') != b'']

    header_fields = lines[0][2:].split('\t')
    if scan_type in _LEADING_LOOP_TYPES:
        header_fields = ['Loop'] + header_fields
    scalars_n = len(header_fields) - 1 if has_profile else len(header_fields)

    # Pass 1: walk the file once, collecting every data row's raw fields
    # together with its 1-based row number and its pending `%`-line
    # metadata (if any), without yet judging whether a row is short.
    raw_rows: list[tuple[int, list[str], list[str] | None]] = []
    pending_meta = None
    meta_header_seen = False
    data_row_num = 0

    for line in lines[1:]:
        if line.startswith('%'):
            fields = line[2:].split('\t')
            if has_profile and not meta_header_seen and fields[0] == 'StartTime':
                meta_header_seen = True
                continue
            meta_header_seen = True
            pending_meta = fields
            continue

        data_row_num += 1
        raw_rows.append((data_row_num, line.split('\t'), pending_meta))
        pending_meta = None

    if not raw_rows:
        raise ValueError(f'{path}: header but no data rows')

    # Pass 2: the maximum field count across all rows is the expected
    # (unpoisoned) row width; validate and convert against that.
    expected = max((len(fields) for _, fields, _ in raw_rows), default=scalars_n)

    scalar_rows: list[numpy.ndarray] = []
    profile_rows: list[numpy.ndarray] = []
    image_names = [] if has_profile else None
    start_times = [] if has_profile else None
    end_times = [] if has_profile else None

    for row_num, fields, meta in raw_rows:
        if len(fields) < expected:
            logger.warning('%s: dropping data row %d (expected %d fields, got %d)',
                            path, row_num, expected, len(fields))
            continue

        values = numpy.array(fields[:expected], dtype=float)
        scalar_rows.append(values[:scalars_n])
        if has_profile:
            profile_rows.append(values[scalars_n:])
            start, end, image_name = meta
            start_times.append(start)
            end_times.append(end)
            image_names.append(image_name)

    columns = {name: numpy.array([row[i] for row in scalar_rows], dtype=float)
               for i, name in enumerate(header_fields[:scalars_n])}
    profile = numpy.array(profile_rows, dtype=float) if has_profile else None

    return DatTable(columns=columns, profile=profile, image_names=image_names,
                     start_times=start_times, end_times=end_times)
