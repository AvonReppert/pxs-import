'''Where a UDKM import reads from: presets, base paths, and the source routes.

A PXS beamtime is not stored in one place. Four acquisition programs write to
four sibling folders under one base, and which one a scan lands in is decided
by the program it was started with -- which in turn decides its scan type.
Each of those folders holds every beamtime since 2011, so assembling one
proposal means reaching into several of them and taking only the days that
belong to it.

This module owns the *site* knowledge of that: which base paths exist on
which machine, what the program folders are called, and how a caller's
arguments turn into a concrete list of source directories. Layout knowledge --
what a scan folder looks like, how a year directory nests, how a date range
filters -- stays in :mod:`pxs_import.format`, and orchestration stays in
:mod:`pxs_import.prepare`.

Two layering rules hold here as everywhere in this subpackage:

- It never imports ``nx5d``. The coupling is one-directional and resolved by
  string, through the ``pm`` console script -- see the project's ``CLAUDE.md``.
- Source folders are opened **read-only**. Nothing is ever written into one:
  no mapping file, no spice, no marker.
'''

import logging
import os
import re
from pathlib import Path

from pxs_import.format import ScanFolder, discover_scans

logger = logging.getLogger(__name__)

#: Base paths to probe, in order, when the caller names none. The first one
#: that exists on this machine wins. These are the three deployment sites of
#: the PXS: a laptop reaching the share by UNC, the group's JupyterHub where
#: it is mounted, and the measurement PC where it is a drive letter.
#:
#: The measurement PC entry is the share, not its local
#: ``C:\Users\Labor\Documents\Measurements`` -- both exist and both carry the
#: same program folders, but only the share is the data all three machines
#: see. A proposal imported from any of them must come out identical, and a
#: local copy holding only the running measurement would drop scans with
#: nothing to show for it. The local path remains reachable with ``--base``.
BASE_PRESETS: tuple[str, ...] = (
    r'\\141.89.115.120\Measurement\XRayLab-Pilatus',
    '/home/jovyan/data/XRayLab-Pilatus',
    'P:\\',
)

#: The acquisition programs' folders, expected directly under a base path.
#: One that does not exist is skipped: not every site runs all four.
PROGRAM_DIRS: tuple[str, ...] = (
    'PilatusTheta2Theta',
    'PilatusPumpProbe',
    'PilatusScanGoniometer',
    'PilatusScanZaber',
)

#: Environment variables naming a base path, for a machine that is not one of
#: the presets but wants the bare call to keep working. Read in this order,
#: so that ``UDKM_BASE`` -- the name the lab machines already export, from
#: when this code lived in ``xraydc`` -- keeps precedence over the newer
#: ``PXS_BASE`` when both are set.
BASE_ENV_VARS = ('UDKM_BASE', 'PXS_BASE')

#: The primary of :data:`BASE_ENV_VARS`, kept as its own name because the
#: how-to and the error messages speak of one variable.
BASE_ENV_VAR = BASE_ENV_VARS[0]


def _base_from_environ(environ):
    '''First non-empty value of :data:`BASE_ENV_VARS` in ``environ``.'''
    for name in BASE_ENV_VARS:
        value = environ.get(name)
        if value:
            return value
    return None

_DATE_RE = re.compile(r'^\d{8}$')
_YEAR_RE = re.compile(r'^\d{4}$')


class SourcesNotFound(RuntimeError):
    '''No route yielded a source directory.

    Raised rather than returning an empty list, because an import that finds
    nothing and exits successfully is the worst outcome available here: the
    next thing the scientist does is look for data that was never written.
    '''


class ScanTypeConflict(RuntimeError):
    '''Two sources hold the same ``YYYYMMDD/HHMMSS`` with different types.

    A plain duplicate is coalesced with a warning; this is not one. There is
    no sane way to pick between an ``sRSM`` and an ``alignment`` claiming the
    same identity, and building one scan out of both would be worse than
    stopping.
    '''


def _is_windows_shaped(raw: str) -> bool:
    '''True if a path is a Windows one -- a drive letter or a UNC prefix.'''
    return raw.startswith('\\\\') or (len(raw) > 1 and raw[1] == ':')


def _probe_order() -> list[str]:
    '''The presets worth probing on this platform, in declared order.

    Probing a UNC path on POSIX, or a POSIX path on Windows, can only ever
    fail -- and on Windows an unreachable UNC blocks for tens of seconds
    before it does, which the laptop would pay on every run whenever it is
    off the lab network.
    '''
    windows = os.name == 'nt'
    return [p for p in BASE_PRESETS if _is_windows_shaped(p) == windows]


def resolve_base(explicit: str | Path | None = None, *,
                  environ: dict | None = None, exists=None) -> Path | None:
    '''Decide which base path the program folders sit under.

    Args:
        explicit: A base path given on the command line; wins outright and is
            not checked for existence, so that a typo is reported by the
            caller against the path the user actually typed.
        environ: Environment mapping to read :data:`BASE_ENV_VARS` from;
            defaults to :data:`os.environ`.
        exists: Predicate taking a :class:`~pathlib.Path`; defaults to
            :meth:`~pathlib.Path.exists`. Injectable so that a test can assert
            the probe order without a filesystem.

    Returns:
        The base path, or ``None`` if no route produced one.
    '''
    if explicit is not None:
        return Path(explicit)
    environ = os.environ if environ is None else environ
    from_env = _base_from_environ(environ)
    if from_env:
        return Path(from_env)
    exists = (lambda p: p.exists()) if exists is None else exists
    for raw in _probe_order():
        if not raw.startswith('/'):
            logger.info('looking for the data share at %s', raw)
        if exists(Path(raw)):
            return Path(raw)
    return None


def program_dirs(base: Path, *, exists=None) -> list[Path]:
    '''The acquisition-program folders present under a base path.

    Args:
        base: The base path, as returned by :func:`resolve_base`.
        exists: Predicate taking a :class:`~pathlib.Path`; defaults to
            :meth:`~pathlib.Path.is_dir`.

    Returns:
        Existing :data:`PROGRAM_DIRS` entries, in declared order.
    '''
    exists = (lambda p: p.is_dir()) if exists is None else exists
    return [base / name for name in PROGRAM_DIRS if exists(base / name)]


def is_date_dir(path: Path) -> bool:
    '''True if ``path`` is itself a ``YYYYMMDD`` measurement day.'''
    return bool(_DATE_RE.match(path.name))


def has_date_dirs(path: Path, *, lister=None) -> bool:
    '''True if ``path`` holds measurement days, directly or behind a year.

    Args:
        path: Directory to look into. A missing or unreadable one is ``False``
            rather than an error: this is a predicate used to choose a route,
            and the chosen route reports its own failures.
        lister: Callable taking a directory and returning its entries;
            defaults to :meth:`~pathlib.Path.iterdir`.
    '''
    lister = (lambda p: p.iterdir()) if lister is None else lister
    try:
        return any(_DATE_RE.match(c.name) or _YEAR_RE.match(c.name)
                   for c in lister(path) if c.is_dir())
    except OSError:
        return False


def resolve_sources(proposal_root: Path, *, explicit: list[Path] | None = None,
                     base: Path | None = None, environ: dict | None = None,
                     exists=None, lister=None) -> tuple[list[Path], str]:
    '''Decide which directories this import reads from.

    Three routes, first match wins:

    1. ``explicit`` -- paths the caller named outright.
    2. ``proposal_root`` itself, when it is a measurement day or holds some.
       This is the long-standing behaviour, and the common case where the
       data has already been hand-copied into one folder. The predicate has
       to accept *both* shapes: test only for "holds days" and a one-day
       import like ``udkm-prepare notebooks/20250408`` falls through to route
       3 and becomes a whole-archive one.
    3. The program folders under the base path.

    Args:
        proposal_root: The workspace, which may also be the data.
        explicit: Paths from ``--source``.
        base: A base path from ``--base``; otherwise resolved by
            :func:`resolve_base`.
        environ: Passed to :func:`resolve_base`.
        exists: Passed to :func:`resolve_base` and :func:`program_dirs`.
        lister: Passed to :func:`has_date_dirs`.

    Returns:
        ``(sources, route)`` -- absolute, resolved source directories and the
        name of the route that produced them: ``'explicit'``, ``'positional'``
        or ``'base'``.

    Raises:
        SourcesNotFound: No route produced a directory. The message names the
            base paths that were probed, since "nothing found" is otherwise
            indistinguishable from "nothing to import".
    '''
    if explicit:
        return [Path(p).resolve() for p in explicit], 'explicit'

    if is_date_dir(proposal_root) or has_date_dirs(proposal_root, lister=lister):
        return [proposal_root.resolve()], 'positional'

    environ = os.environ if environ is None else environ
    named = base if base is not None else _base_from_environ(environ)
    resolved_base = resolve_base(base, environ=environ, exists=exists)
    if resolved_base is not None:
        found = program_dirs(resolved_base, exists=exists)
        if found:
            return [p.resolve() for p in found], 'base'

    # A base the user named and a base we went looking for are different
    # failures, and listing presets that were never probed sends someone
    # hunting in the wrong place.
    if named is not None:
        raise SourcesNotFound(
            f'no source folders found: {proposal_root} holds no measurement days, '
            f'no --source was given, and {named} holds none of '
            f'{", ".join(PROGRAM_DIRS)}')

    probed = ', '.join(_probe_order()) or '(none on this platform)'
    raise SourcesNotFound(
        f'no source folders found: {proposal_root} holds no measurement days, '
        f'no --source was given, and no base path was usable. '
        f'Set {BASE_ENV_VAR} or pass --base. Probed: {probed}')


def discover_all(sources: list[Path], *, start: str | None = None,
                  end: str | None = None, lister=None) -> list[ScanFolder]:
    '''Discover every scan across several sources as one chronological set.

    Numbering is global, so the merge has to happen before it: each source is
    discovered in turn, the results are concatenated and then sorted by
    ``date + time`` across all of them. Numbering the sources separately
    would restart at ``r0001`` for each.

    Duplicates are possible because a scan's identity, ``YYYYMMDD/HHMMSS``,
    is not qualified by the acquisition program. In practice all four
    programs drive the same detector and cannot run at once, so the realistic
    cause is two overlapping ``--source`` paths -- or a proposal folder that
    already holds hand-copied duplicates of what the sources contain. Those
    users must not be refused their import, so a duplicate is **coalesced**:
    the earlier source in ``sources`` order wins, and a warning names both
    folders. The warning is the point -- building one scan out of two folders
    unnoticed is the failure being avoided, not the coalescing itself.

    Args:
        sources: Source directories, in precedence order.
        start: Earliest measurement day, ``YYYYMMDD``, inclusive.
        end: Latest measurement day, ``YYYYMMDD``, inclusive.
        lister: Directory-listing callable, as
            :func:`~pxs_import.format.discover_scans` takes.

    Returns:
        Scan folders sorted by ``date + time``, each stamped with the
        ``root`` it came from, one per identity.

    Raises:
        ScanTypeConflict: Two sources claim the same identity with different
            scan types.
    '''
    seen: dict[str, ScanFolder] = {}
    for source in sources:
        for scan in discover_scans(source, start=start, end=end, lister=lister):
            first = seen.get(scan.source)
            if first is None:
                seen[scan.source] = scan
                continue
            if first.scan_type != scan.scan_type:
                raise ScanTypeConflict(
                    f'{scan.source}: {first.path} is {first.scan_type} but '
                    f'{scan.path} is {scan.scan_type}; the same scan cannot be both')
            logger.warning('%s: duplicate scan, keeping %s and ignoring %s',
                            scan.source, first.path, scan.path)
    return sorted(seen.values(), key=lambda s: (s.date, s.time))
