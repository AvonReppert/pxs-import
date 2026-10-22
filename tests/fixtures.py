'''Synthetic UDKM scan-folder writers, reused by the ``format``/``frames``
tests.

Each ``write_*`` function builds one ``<proposal>/YYYYMMDD/HHMMSS/`` scan
folder from scratch: the signature ``.dat`` table (with the real header and
per-row metadata lines of the corresponding scan type), a
``parameters<sfx>.txt`` file (Windows-1252), and ``image<NNNNN>.tif`` frames
(``int32``, filled with the frame index) via :func:`tifffile.imwrite`.
``folder.name`` is used as the ``sfx`` (the ``HHMMSS`` suffix in real
filenames).
'''

from datetime import datetime, timedelta
from pathlib import Path

import numpy
import tifffile

_ROI = '(0, 0); (487, 195)'


def _date_time_str(folder: Path) -> str:
    '''Render a ``'DD.MM.YYYY HH:MM:SS'`` ``Date Time`` value from the scan
    folder's ``YYYYMMDD``/``HHMMSS`` path components.'''
    dt = datetime.strptime(folder.parent.name + folder.name, '%Y%m%d%H%M%S')
    return dt.strftime('%d.%m.%Y %H:%M:%S')


def _row_times(i: int) -> tuple[str, str]:
    '''Synthetic ``(start, end)`` ``'HH:MM:SS.mmm'`` timestamps for data row ``i``.'''
    start = datetime(2000, 1, 1, 11, 55, 59, 493000) + timedelta(seconds=2.517 * i)
    end = start + timedelta(seconds=1.513)
    return start.strftime('%H:%M:%S.%f')[:-3], end.strftime('%H:%M:%S.%f')[:-3]


def _write_params(folder: Path, sfx: str, *, exposure: str,
                   correction: float | None, params: dict | None) -> None:
    params = params or {}
    lines = list(params.get('Comments', []))
    if lines:
        lines.append('')
    lines.append(f'Date Time: {_date_time_str(folder)}')
    lines.append(f'Exposure Time: {exposure}')
    lines.append(f'ROI: {_ROI}')
    if correction is not None:
        lines.append(f'2 Theta Correction: {correction:.6f}')
    # Anything else the caller passes becomes a verbatim `Key: value` line,
    # so a test can simulate one scan type's own parameter vocabulary.
    lines += [f'{k}: {v}' for k, v in params.items() if k != 'Comments']
    content = '\n'.join(lines) + '\n'
    (folder / f'parameters{sfx}.txt').write_bytes(content.encode('windows-1252'))


def write_scan_temperatures(folder: Path, rows=((12.15, 15.25), (108.00, 108.77)),
                             *, stamp: tuple[str, str] | None = None) -> Path:
    '''Write a ``scan data<sfx>.txt`` cryostat log into a scan folder.

    The real files are a running log of a whole cooldown, copied into every
    scan folder and one line longer each time, so the **last** row is the
    scan's own. That row carries its own ``Day``/``Time``, which is what
    makes the association checkable rather than positional.

    Reproduces the real file's quirk exactly: the header is tab-separated,
    the data rows are space-separated with a trailing tab.

    Args:
        folder: The scan folder; its name supplies the ``sfx``.
        rows: ``(TA, TB)`` pairs. All but the last get a placeholder stamp.
        stamp: ``(day, time)`` for the last row. Defaults to the folder's
            own ``YYYYMMDD``/``HHMMSS`` -- pass something else to simulate a
            log that does not belong to this scan.
    '''
    sfx = folder.name
    day, time = stamp if stamp is not None else (folder.parent.name, sfx)
    lines = ['% TA []\tTB [K]\tDay\tTime', '% ']
    for i, (ta, tb) in enumerate(rows):
        last = i == len(rows) - 1
        d, t = (day, time) if last else ('20151223', f'0248{i:02d}')
        lines.append(f'{ta:.2f} {tb:.2f} {d} {t}\t')
    path = folder / f'scan data{sfx}.txt'
    path.write_text('\r\n'.join(lines) + '\r\n', encoding='windows-1252', newline='')
    return path


def _write_tifs(folder: Path, shape, n_referenced: int, extra_tifs: int,
                 missing: tuple) -> None:
    for i in range(n_referenced + extra_tifs):
        if i < n_referenced and i in missing:
            continue
        tifffile.imwrite(folder / f'image{i:05d}.tif',
                          numpy.full(shape, i, dtype=numpy.int32))


def write_srsm(folder: Path, n_rows: int, width: int = 6, shape=(4, 6),
               images: bool = True, extra_tifs: int = 0,
               missing: tuple = (), params: dict | None = None) -> Path:
    '''Write a synthetic ``sRSM`` (``rockingcurves<sfx>.dat``) scan folder.'''
    folder.mkdir(parents=True, exist_ok=True)
    sfx = folder.name
    header = ['Loop', 'Theta / deg.', '2Theta + Corr / deg.', 'Crystal',
               'Crystal Calib', 'Rocking Curve']
    lines = ['% ' + '\t'.join(header), '% StartTime\tEndTime\tImageName']
    for i in range(n_rows):
        start, end = _row_times(i)
        lines.append(f'% {start}\t{end}\timage{i:05d}.tif')
        theta = 18.5 + 0.1 * i
        scalars = [1.0, theta, 2.0 * theta, 0.134368 + 0.0001 * i,
                    774457.208069 - 100.0 * i]
        row = scalars + [0.0] * width
        lines.append('\t'.join(f'{v:.6f}' for v in row))
    (folder / f'rockingcurves{sfx}.dat').write_text('\r\n'.join(lines) + '\r\n',
                                                       encoding='utf-8', newline='')
    _write_params(folder, sfx, exposure='2.000000s', correction=0.0, params=params)
    if images:
        _write_tifs(folder, shape, n_rows, extra_tifs, missing)
    return folder


def write_trss(folder: Path, n_rows: int, width: int = 6, shape=(4, 6),
               theta: float = 19.48, correction: float = 0.0,
               images: bool = True, extra_tifs: int = 0,
               missing: tuple = (), params: dict | None = None) -> Path:
    '''Write a synthetic ``tRSS`` (``scans<sfx>.dat``) scan folder.'''
    folder.mkdir(parents=True, exist_ok=True)
    sfx = folder.name
    header = ['Loop_Delay', 'Delay', 'Loop_Theta', 'Theta', 'Crystal [V]',
               'Crystal [ph/s]', 'Pilatus [ph/s]', 'Repeats', 'ImageNumber',
               'Intensities [ph/s]']
    lines = ['% ' + '\t'.join(header)]
    for i in range(n_rows):
        start, end = _row_times(i)
        lines.append(f'% {start}\t{end}\timage{i:05d}.tif')
        scalars = [float(i + 1), -5.0 + i, 1.0, theta, 0.128643 + 0.0001 * i,
                    1563459.311439 - 1000.0 * i, 1380.5 + 10.0 * i, 0.0, float(i)]
        row = scalars + [0.0] * width
        lines.append('\t'.join(f'{v:.6f}' for v in row))
    (folder / f'scans{sfx}.dat').write_text('\r\n'.join(lines) + '\r\n',
                                              encoding='utf-8', newline='')
    _write_params(folder, sfx, exposure='2.000000s', correction=correction, params=params)
    if images:
        _write_tifs(folder, shape, n_rows, extra_tifs, missing)
    return folder


def write_xrss(folder: Path, n_rows: int, shape=(4, 6),
               n_tifs: int | None = None, params: dict | None = None) -> Path:
    '''Write a synthetic ``xRSS`` (``allSteps<sfx>.dat``) scan folder.

    The first row's ``Position Real`` value is always ``10.078973``.
    '''
    folder.mkdir(parents=True, exist_ok=True)
    sfx = folder.name
    header = ['Position set', 'Position Real', 'Loop', 'Pilatus', 'Crystal',
               'Norm', 'Crystal Calib']
    lines = ['% ' + '\t'.join(header)]
    for i in range(n_rows):
        position_set = 10.0 - i
        row = [position_set, position_set + 0.078973, 1.0, 5.0 + i,
                0.120620 + 0.0001 * i, 51.218880 + i, 0.097620 + 0.00001 * i]
        lines.append('\t'.join(f'{v:.6f}' for v in row))
    (folder / f'allSteps{sfx}.dat').write_text('\r\n'.join(lines) + '\r\n',
                                                 encoding='utf-8', newline='')
    _write_params(folder, sfx, exposure='1.000000', correction=None, params=params)
    for i in range(n_rows if n_tifs is None else n_tifs):
        tifffile.imwrite(folder / f'image{i:05d}.tif',
                          numpy.full(shape, i, dtype=numpy.int32))
    return folder


def write_alignment(folder: Path, n_rows: int, shape=(4, 6),
                     n_tifs: int | None = None, stage: str | None = 'Omega',
                     unit: str | None = 'deg', params: dict | None = None) -> Path:
    '''Write a synthetic ``alignment`` (``scan<sfx>.dat``) scan folder.

    Unlike every other type, the header declares four column names while each
    data row carries five fields: the leading one is the loop index and is
    undeclared. The fixture writes that shape deliberately -- it is the thing
    :func:`~pxs_import.format.parse_dat` has to get right.

    ``Position`` starts at ``-1.000000`` and steps by ``+0.1``; every row's
    loop index is ``1``. ``stage``/``unit``, when not ``None``, are merged
    into ``params`` so a caller can pass its own parameter keys as well.
    '''
    folder.mkdir(parents=True, exist_ok=True)
    sfx = folder.name
    header = ['Position', 'Pilatus', 'Crystal', 'Norm']
    lines = ['% ' + '\t'.join(header)]
    for i in range(n_rows):
        position = -1.0 + 0.1 * i
        row = [1.0, position, 388023.0 - i, 1031.350606 + i, 376.228024 - i]
        lines.append('\t'.join(f'{v:.6f}' for v in row))
    (folder / f'scan{sfx}.dat').write_text('\r\n'.join(lines) + '\r\n',
                                            encoding='utf-8', newline='')
    merged = dict(params or {})
    if stage is not None:
        merged['Stage'] = stage
    if unit is not None:
        merged['Unit'] = unit
    _write_params(folder, sfx, exposure='1.000000', correction=None, params=merged)
    for i in range(n_rows if n_tifs is None else n_tifs):
        tifffile.imwrite(folder / f'image{i:05d}.tif',
                          numpy.full(shape, i, dtype=numpy.int32))
    return folder
