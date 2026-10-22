'''Authoritative frame plan and ``float32`` stack loading for UDKM scans.

A scan folder's ``.dat`` table (:mod:`pxs_import.format`) is the sole
authority on which TIFF frames belong to a scan. Real scan folders routinely
contain TIFFs the ``.dat`` never references (junk left over from aborted
acquisitions) and occasionally omit frames the ``.dat`` does reference. This
module never builds a frame stack by globbing: it always walks the ``.dat``
row by row.

``sRSM``/``tRSS`` carry a per-row ``ImageName`` column, so a missing frame is
simply a hole at its own row. ``xRSS`` has no name column -- row *i*'s frame
is ``image{i:05d}.tif`` by convention -- so an interrupted run leaves a long
tail of rows with no frame at all; the plan keeps only the contiguous run of
existing frames starting at index 0 and marks itself ``truncated``.
'''

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy
import tifffile

from pxs_import.format import DatTable

logger = logging.getLogger(__name__)

#: Scan types with no per-row ``ImageName`` column, whose frames are
#: therefore addressed by row index as ``image{i:05d}.tif``.
_INDEXED_FRAME_TYPES: tuple[str, ...] = ('xRSS', 'alignment')


@dataclass
class FramePlan:
    '''Which frame file belongs to each kept row of a scan.

    Args:
        files: One entry per kept row. ``files[i]`` is the frame path for
            row ``i``, or ``None`` if that row references a frame that does
            not exist on disk.
        n_rows: Number of rows in the source :class:`~pxs_import.format.DatTable`.
        truncated: ``True`` if this plan was cut short of ``n_rows`` (``xRSS``
            only, when the contiguous run of existing frames from index 0 is
            shorter than the table).
        unreferenced: ``*.tif`` files present in the scan folder but not part
            of this plan. Never loaded.
        folder: The scan folder this plan was built for.
    '''

    files: list[Path | None]
    n_rows: int
    truncated: bool
    unreferenced: list[Path]
    folder: Path

    @property
    def n_frames(self) -> int:
        '''Number of entries in :attr:`files` (the planned, possibly
        truncated, frame count).'''
        return len(self.files)


def plan_frames(folder: Path, table: DatTable, scan_type: str) -> FramePlan:
    '''Build the authoritative frame plan for one scan.

    For ``sRSM``/``tRSS``, ``table.image_names`` gives the frame file name
    for every row; a name whose file does not exist becomes ``None`` in
    :attr:`FramePlan.files` and the plan is never truncated.

    For ``xRSS`` and ``alignment`` (:data:`_INDEXED_FRAME_TYPES`), there is
    no name column: row *i*'s frame is always
    ``image{i:05d}.tif``. The plan keeps only the contiguous run of existing
    frames starting at index 0 -- an interrupted run leaves every later row
    without a frame, and those rows are dropped from the plan rather than
    represented as ``None`` holes. :attr:`FramePlan.truncated` is ``True``
    whenever that run is shorter than ``table.n_rows``.

    Any ``*.tif`` in ``folder`` that is not part of the plan is recorded in
    :attr:`FramePlan.unreferenced` and a single warning logs their count. Such
    files are never opened.

    Args:
        folder: The scan folder.
        table: The scan's parsed ``.dat`` table.
        scan_type: One of ``'sRSM'``, ``'tRSS'``, ``'xRSS'``, ``'alignment'``.

    Returns:
        The frame plan.
    '''
    n_rows = table.n_rows

    if scan_type in _INDEXED_FRAME_TYPES:
        files: list[Path | None] = []
        for i in range(n_rows):
            candidate = folder / f'image{i:05d}.tif'
            if not candidate.is_file():
                break
            files.append(candidate)
        truncated = len(files) < n_rows
    else:
        files = []
        for i, name in enumerate(table.image_names):
            candidate = folder / name
            if candidate.is_file():
                files.append(candidate)
            else:
                logger.warning('%s: missing frame for row %d: %s', folder, i, candidate)
                files.append(None)
        truncated = False

    planned = {f for f in files if f is not None}
    unreferenced = sorted(f for f in folder.glob('*.tif') if f not in planned)
    if unreferenced:
        logger.warning('%s: %d unreferenced TIFF file(s) ignored', folder, len(unreferenced))

    return FramePlan(folder=folder, files=files, n_rows=n_rows, truncated=truncated,
                      unreferenced=unreferenced)


def load_stack(plan: FramePlan) -> tuple[numpy.ndarray, numpy.ndarray]:
    '''Load a frame plan into a dense ``float32`` stack.

    Reads the first existing file in ``plan.files`` to fix the frame shape
    ``(H, W)``, then preallocates ``(N, H, W)`` filled with ``NaN`` and fills
    in every existing frame, converted to ``float32``. Missing frames (``None``
    entries) stay ``NaN`` and are reported in the returned boolean mask;
    :func:`plan_frames` already logged a warning naming each one when it
    built the plan.

    Args:
        plan: The frame plan to load.

    Returns:
        ``(stack, missing)``: ``stack`` is ``(N, H, W)`` ``float32`` with
        ``NaN`` where a frame was missing; ``missing`` is a ``(N,)`` boolean
        array, ``True`` where the corresponding frame was missing.

    Raises:
        ValueError: If no planned frame exists at all (no shape can be
            determined), naming the scan folder; or if a later frame's shape
            differs from the first loaded frame's, naming that file.
    '''
    n = plan.n_frames
    missing = numpy.array([f is None for f in plan.files], dtype=bool)

    shape = None
    for f in plan.files:
        if f is not None:
            shape = tifffile.imread(f).astype(numpy.float32).shape
            break
    if shape is None:
        raise ValueError(f'no existing frames to load in {plan.folder} '
                          f'({n} planned, all missing)')

    stack = numpy.full((n,) + shape, numpy.nan, dtype=numpy.float32)
    for i, f in enumerate(plan.files):
        if f is None:
            continue
        frame = tifffile.imread(f).astype(numpy.float32)
        if frame.shape != shape:
            raise ValueError(f'frame shape mismatch at {f}: expected {shape}, got {frame.shape}')
        stack[i] = frame

    return stack, missing
