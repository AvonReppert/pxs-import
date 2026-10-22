'''Preparation orchestrator and CLI for importing a UDKM proposal into nx5d.

Ties together scan discovery/numbering (:mod:`pxs_import.format`), the
mapping-table helpers (:mod:`pxs_import.reader`) and spice anchor migration
(:mod:`pxs_import.spice`) into one command: discover and number the
proposal's scans, write the mapping table, shell out to nx5d's
``pm scan import`` CLI with the UDKM reader (``pxs_import.reader:scans_from_pxs``),
then (if the proposal has a spice folder) migrate its anchors to the new
run-number scheme.

This module never drives nx5d in-process -- it only shells out to the ``pm``
console script (or, failing that, invokes its ``main()`` in a subprocess). It
imports no ``nx5d``, ``h5py`` or ``xraydc``.
'''

import argparse
import logging
import os
import shutil
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pxs_import import s3
from pxs_import.reader import (format_mapping_table, read_mapping_table, renumbered,
                               write_mapping_table)
from pxs_import.format import ScanFolder, discover_scans, number_scans
from pxs_import.sources import discover_all, resolve_sources
from pxs_import.spice import SpiceChange, fix_recipes, migrate_spice

logger = logging.getLogger(__name__)


@dataclass
class PrepareResult:
    '''Outcome of :func:`prepare_proposal`.

    Args:
        mapping: ``(scan_name, ScanFolder)`` pairs, as returned by
            :func:`~pxs_import.format.number_scans`.
        target_url: The nx5d target URL passed to ``pm scan import -u``.
        spice_url: The URL nx5d would open the spice folder at -- the
            ``file://`` form locally, the bucket prefix under ``--s3``.
        command: Every ``pm`` command line that was run, in order. Local
            imports run one; S3 imports run one per scan, so that a crash
            is attributable to a single scan (see :func:`prepare_proposal`).
        skipped: Scan keys the S3 manifest already recorded as complete, so
            they were not re-imported. Always empty for a local import.
        imported: Scan keys actually imported by this run.
        spice_changes: Anchor rewrites applied to the proposal's spice
            folder (empty if there was none, or migration found nothing to
            change).
    '''

    mapping: list[tuple[str, ScanFolder]]
    target_url: str
    spice_url: str
    command: list[list[str]]
    skipped: list[str]
    imported: list[str]
    spice_changes: list[SpiceChange]


def _as_daystamp(value: str | None) -> str | None:
    '''Turn a ``YYYY-MM-DD`` command-line date into a ``YYYYMMDD`` day name.

    Measurement days are directories named ``YYYYMMDD``, and the filter
    compares directory names, so this is where the two spellings meet. ISO on
    the command line because that is what people type and misread least.

    Args:
        value: The flag's value, or ``None`` if it was not given.

    Returns:
        ``YYYYMMDD``, or ``None``.

    Raises:
        SystemExit: The value is not a valid ISO date.
    '''
    if value is None:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime('%Y%m%d')
    except ValueError:
        raise SystemExit(f'not a YYYY-MM-DD date: {value!r}') from None


def _file_url(path: Path) -> str:
    '''Build a ``file://`` URL for an absolute path, correct on every platform.

    ``path.as_posix()`` already carries the leading ``/`` for a POSIX
    absolute path (``/home/jovyan/...``) but not for a Windows one
    (``C:/Users/...``). Gluing a flat ``f'file:///{path.as_posix()}'``
    therefore doubles the slash on POSIX -- ``file:////home/jovyan/...``,
    which is what a user actually saw in a JupyterHub container -- while
    staying correct on Windows, which is why the bug went unnoticed: this
    repo's own tests only ever resolve paths on Windows. Normalizing to
    exactly one leading slash before prepending the two-slash scheme keeps
    both platforms correct.

    Args:
        path: An absolute path (the caller resolves it first).

    Returns:
        ``file:///<path>``, with exactly one slash between the scheme and
        the path's own leading slash.
    '''
    posix = path.as_posix()
    if not posix.startswith('/'):
        posix = f'/{posix}'
    return f'file://{posix}'


def target_url_for(proposal_root: Path, *, single_file: bool = False,
                    output_root: Path | None = None,
                    s3_bucket: str | None = None) -> str:
    '''Build the nx5d target URL for a proposal's imported scans.

    Args:
        proposal_root: Proposal root directory; its ``.name`` names the
            single-file target and, on S3, the proposal folder.
        single_file: If true, every scan lands in one ``.h5`` file with a
            ``#{scan}`` group; otherwise one ``.h5`` file per scan under a
            ``scans/`` directory. Ignored when ``s3_bucket`` is given.
        output_root: Where the target lives, if different from
            ``proposal_root``. Defaults to ``proposal_root``. Ignored when
            ``s3_bucket`` is given.
        s3_bucket: If given, target that S3 bucket instead of the local
            filesystem: one Zarr store per scan, side by side under the
            proposal's folder.

    Returns:
        With ``s3_bucket``, ``s3://<bucket>/<proposal>/{scan}.zarr`` -- the
        two-slash form, with the bucket as the netloc; ``s3:///bucket/...``
        would parse as an empty netloc and name a different location.
        Otherwise a ``file:///...`` URL. Either way with a literal
        ``{scan}`` placeholder, as ``pm scan import -u`` expects.

        In the ``file://`` case both ``proposal_root`` and ``output_root``
        are resolved to absolute paths first (``.resolve()``), so a relative
        ``proposal_root`` (e.g. ``'.'`` or ``'mydata'``) still yields a
        well-formed, unambiguous URL instead of one anchored at the
        filesystem root or at an empty name.
    '''
    proposal_root = proposal_root.resolve()
    if s3_bucket is not None:
        return f's3://{s3_bucket}/{proposal_root.name}/{{scan}}.zarr'
    out = (output_root or proposal_root).resolve()
    if single_file:
        return f'{_file_url(out)}/{proposal_root.name}.h5#{{scan}}'
    return f'{_file_url(out)}/scans/{{scan}}.h5'


def spice_url_for(proposal_root: Path, spice_dir: Path, *,
                   s3_bucket: str | None = None) -> str:
    '''Build the URL of a proposal's spice folder, as nx5d would open it.

    Args:
        proposal_root: Proposal root directory; its ``.name`` names the
            proposal folder on S3.
        spice_dir: The local spice folder, used for the ``file://`` form.
        s3_bucket: If given, the bucket holding the spice folder.

    Returns:
        ``s3://<bucket>/<proposal>/spice`` with ``s3_bucket``, otherwise
        ``file:///<spice_dir>``.
    '''
    if s3_bucket is not None:
        return f's3://{s3_bucket}/{proposal_root.resolve().name}/spice'
    return _file_url(spice_dir.resolve())


def existing_output(proposal_root: Path, *, single_file: bool,
                     output_root: Path | None = None) -> Path | None:
    '''Find any already-existing import output for a proposal.

    Args:
        proposal_root: Proposal root directory.
        single_file: Same meaning as in :func:`target_url_for`.
        output_root: Same meaning as in :func:`target_url_for`.

    Returns:
        The ``scans/`` directory if it contains at least one ``*.h5`` file,
        or the single-file ``.h5`` path if it exists; ``None`` if there is
        no existing output. ``proposal_root``/``output_root`` are resolved
        the same way as in :func:`target_url_for`, so this checks the same
        location that URL points at even when given relative paths.
    '''
    proposal_root = proposal_root.resolve()
    out = (output_root or proposal_root).resolve()
    if single_file:
        h5_path = out / f'{proposal_root.name}.h5'
        return h5_path if h5_path.exists() else None
    scans_dir = out / 'scans'
    if scans_dir.is_dir() and any(scans_dir.glob('*.h5')):
        return scans_dir
    return None


def _resolve_pm() -> list[str]:
    '''Resolve the ``pm`` command to invoke.

    Prefers the console script installed alongside the current interpreter,
    then a bare ``PATH`` lookup. ``nx5d/pmtool/pm.py`` has no ``__main__``
    guard, so ``python -m nx5d.pmtool.pm`` does not work; if no console
    script is found at all, fall back to invoking its ``main()`` directly in
    a subprocess.

    Returns:
        The resolved command, as an argv list.
    '''
    found = (shutil.which('pm', path=str(Path(sys.executable).parent))
             or shutil.which('pm'))
    if found:
        return [found]
    return [sys.executable, '-c',
            'import sys; from nx5d.pmtool.pm import main; sys.exit(main() or 0)']


def _s3_import_loop(*, fs, pm, runner, mapping, proposal_root, target_url,
                     bucket, photon_energy, force, dry_run, s3_profile=None):
    '''Import a proposal to S3, one ``pm`` invocation per scan.

    One invocation per scan, rather than one for the whole proposal, is what
    makes "resume where it stopped" possible: ``pm`` reports success for a
    batch, so a crash partway through a batch leaves no way to tell which
    scans finished. Per scan, a zero exit plus a non-empty store is a
    completion signal that can go in the manifest immediately.

    Each scan's prefix is deleted before it is written. Overwriting is not
    enough: nx5d's writer skips datasets that already exist unless
    ``overwrite_data`` is set, and camagick allocates each array at full
    shape upfront, so a run killed mid-write leaves correctly shaped arrays
    whose missing chunks read back as the fill value.

    Args:
        fs: Filesystem, as returned by :func:`pxs_import.s3.filesystem`.
        pm: The ``pm`` command, as an argv list.
        runner: Callable with :func:`subprocess.run`'s signature.
        mapping: ``(scan_name, ScanFolder)`` pairs for the whole proposal.
        proposal_root: Resolved proposal root, the import source.
        target_url: Target URL with a literal ``{scan}`` placeholder.
        bucket: Bucket name, for the manifest URL.
        photon_energy: Forwarded to the reader.
        force: If true, ignore the manifest and re-import every scan. The
            manifest is still *read*, so a scan this run never reaches keeps
            its record instead of being erased along with the skip set.
        dry_run: If true, build and print the commands, and return without
            deleting, running or writing anything.
        s3_profile: AWS profile to put in the subprocess environment, so
            that ``pm`` writes through the same credentials and endpoint
            this process reads the manifest with. Without it ``pm`` resolves
            the ``default`` profile and can silently target a different
            account entirely.

    Returns:
        ``(commands, skipped, imported)``.

    Raises:
        RuntimeError: A scan's ``pm`` invocation failed, or exited zero
            having written no arrays. Named after that scan; the manifest
            retains every scan completed before it.
    '''
    manifest_url = s3.manifest_url(bucket, proposal_root.name)
    manifest = s3.read_manifest(fs, manifest_url)
    env = {**os.environ, 'AWS_PROFILE': s3_profile} if s3_profile else None

    commands: list[list[str]] = []
    skipped: list[str] = []
    imported: list[str] = []

    for scan_name, folder in mapping:
        recorded = manifest.get(scan_name)
        # `source` alone (`YYYYMMDD/HHMMSS`) does not tell two scans apart
        # when they came from different acquisition folders -- only `root`
        # does. An entry written before `root` existed has no such key and is
        # honoured on `source` alone, same as before; one that does have it
        # must agree on both to count as the same scan.
        folder_root = '' if folder.root is None else str(folder.root)
        root_matches = recorded is None or 'root' not in recorded or recorded['root'] == folder_root
        if (not force and recorded is not None and recorded.get('source') == folder.source
                and root_matches):
            skipped.append(scan_name)
            continue

        resolved_arg = 'resolved=' + json.dumps(
            [[scan_name, str(folder.path), folder.scan_type]])
        command = [*pm, '-P', '-u', target_url,
                   'scan', 'import', str(proposal_root),
                   '-R', 'pxs_import.reader:scans_from_pxs',
                   # `only=` is redundant beside a one-entry `resolved=`, but both
                   # come from the same loop iteration and so cannot disagree, and
                   # it is what makes the logged command say which scan it is.
                   f'photon_energy={photon_energy}', f'only={scan_name}', resolved_arg,
                   '-W', 'zarr', 'overwrite_data=True', 'frame_guard=None']
        commands.append(command)
        if dry_run:
            print(f'would import {scan_name} ({folder.source}): {" ".join(command)}')
            continue

        store_url = target_url.format(scan=scan_name)
        print(f'{scan_name} ({folder.source}) -> {store_url}')
        s3.delete_prefix(fs, store_url)
        try:
            runner(command, check=True, **({'env': env} if env else {}))
        except Exception as exc:
            raise RuntimeError(f'{scan_name} ({folder.source}): import failed: {exc}') from exc
        if not s3.store_has_arrays(fs, store_url):
            raise RuntimeError(f'{scan_name} ({folder.source}): pm reported success but '
                                f'{store_url} holds no arrays')

        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        # `root` says which acquisition program's folder this came from, which
        # the identity (`source`, i.e. YYYYMMDD/HHMMSS) does not. Additive: an
        # entry written before this existed simply has no `root`, and the skip
        # check below never looks at it.
        manifest[scan_name] = {'source': folder.source, 'imported': stamp,
                               'root': '' if folder.root is None else str(folder.root)}
        s3.write_manifest(fs, manifest_url, manifest)
        imported.append(scan_name)

    if skipped:
        print(f'skipped {len(skipped)} scan(s) already recorded complete: '
              f'{", ".join(skipped)}')

    return commands, skipped, imported


def prepare_proposal(proposal_root: Path, *, single_file: bool = False,
                      force: bool = False, dry_run: bool = False,
                      photon_energy: float = 8048.0,
                      output_root: Path | None = None,
                      spice_dir: Path | None = None,
                      pm: list[str] | None = None,
                      runner=None,
                      s3_bucket: str | None = None,
                      s3_profile: str | None = None,
                      force_spice: bool = False,
                      fs=None,
                      sources: list[Path] | None = None,
                      base: Path | None = None,
                      start: str | None = None,
                      end: str | None = None) -> PrepareResult:
    '''Discover, number, import and (if present) migrate a UDKM proposal.

    Order of operations: discover and number the proposal's scans; refuse
    (or, with ``force`` and outside a dry run, clear) any pre-existing
    import output; write the mapping table and print it; build and run the
    ``pm scan import`` command; if the proposal has a spice folder, fix its
    recipes and migrate its anchors to the new run-number scheme; print the
    resulting ``Proposal(...)`` line nx5d can load.

    Args:
        proposal_root: Proposal root directory, as accepted by
            :func:`~pxs_import.format.discover_scans`. Resolved to an
            absolute path (``.resolve()``) before use, so a relative path
            (e.g. ``'.'``) still names the right directory both for
            discovery and for the target URL/overwrite guard.
        single_file: If true, import every scan into one ``.h5`` file with
            ``#{scan}`` groups; otherwise one ``.h5`` file per scan.
        force: If true, remove any pre-existing import output before
            writing (skipped during a dry run -- see ``dry_run``);
            otherwise refuse by raising ``FileExistsError``.
        dry_run: If true, ``pm`` is invoked with ``-d`` (so it writes
            nothing) and :func:`~pxs_import.spice.fix_recipes`/
            :func:`~pxs_import.spice.migrate_spice` are called with
            ``dry_run=True`` (so the spice folder is never edited); and
            pre-existing import output is never removed, even with
            ``force=True`` -- only logged as what *would* be removed. The
            mapping table is still computed and written to
            ``<output_root>/scan-mapping.csv``, and printed, either way.
        photon_energy: Forwarded to the reader as ``photon_energy=<value>``.
        output_root: Where the import lands, if different from
            ``proposal_root``. Defaults to ``proposal_root``; resolved to an
            absolute path before use.
        spice_dir: The proposal's spice folder to migrate, if any. Defaults
            to ``proposal_root/'spice'``; resolved to an absolute path
            before use; skipped entirely if it is not a directory.
        pm: The ``pm`` command to invoke, as an argv list. Defaults to
            :func:`_resolve_pm`'s result.
        runner: Callable used to run the ``pm`` command, with the same
            signature as :func:`subprocess.run`. Defaults to
            ``subprocess.run``, resolved at call time (rather than as a
            ``def``-time default) so that patching
            ``pxs_import.prepare.subprocess.run`` takes effect.
        s3_bucket: If given, import to that S3 bucket instead of the local
            filesystem -- one Zarr store per scan, resumable through a
            manifest object (:func:`_s3_import_loop`). ``single_file`` and
            ``output_root`` do not apply.
        s3_profile: AWS profile to use, or ``None`` for botocore's standard
            chain. Only consulted when ``fs`` is not given.
        force_spice: If true, overwrite an existing remote spice prefix
            instead of refusing. Independent of ``force``: spice is
            hand-edited, scan data is generated.
        fs: Filesystem to use instead of opening one, as an injection point
            for tests. When given, the credential precondition is skipped --
            a caller supplying a filesystem owns its authentication.

    Returns:
        The :class:`PrepareResult` describing what was done.

    Raises:
        FileExistsError: There is already import output at the target and
            ``force`` is false; or, under ``s3_bucket``, the remote spice
            prefix is non-empty and ``force_spice`` is false.
        RuntimeError: The ``base`` route was used with no ``start``; or no
            scans were found; or scans would be renumbered since the last
            import and ``force`` is false; or ``s3_bucket`` was given, no
            filesystem was injected and no credential profile is configured;
            or a scan's import failed (see :func:`_s3_import_loop`).
    '''
    if pm is None:
        pm = _resolve_pm()
    if runner is None:
        runner = subprocess.run

    proposal_root = proposal_root.resolve()
    out = (output_root or proposal_root).resolve()
    spice_dir = (spice_dir or proposal_root / 'spice').resolve()

    if s3_bucket is not None and fs is None:
        if not s3.credentials_available(s3_profile):
            raise RuntimeError(
                'no S3 credentials on this machine; run `pxs-prepare --s3-login` once '
                'to store them in ~/.aws')
        fs = s3.filesystem(s3_profile)

    sources = [Path(p).resolve() for p in sources] if sources else None
    resolved_sources, route = resolve_sources(proposal_root, explicit=sources, base=base)
    if route == 'base' and start is None:
        # A RuntimeError, not a SystemExit: this function is the one a future
        # measurement-PC GUI would call directly, and a CLI-only exception
        # sitting in it leaves such a caller with no way to catch the refusal
        # and show it in its own terms. `main` below still turns it into the
        # same stderr message and exit code 1, through the RuntimeError
        # handler every other orchestration failure already uses.
        raise RuntimeError(
            'refusing to import from the acquisition program folders without a date '
            'range: they hold every beamtime since 2011. Pass --from (and optionally '
            '--to), or point at a folder that already holds the measurement days.')
    # Printed, not just logged: nothing in this package configures logging, so
    # an INFO line reaches no one. Where the tool looked and which days it took
    # are the two facts a user needs when an import comes back smaller than
    # expected -- and the UNC probe above can stall for half a minute on a
    # laptop that is off the lab network, which is only explicable if the
    # attempt was announced.
    window = f'{start or "the beginning"}..{end or "no end"}'
    print(f'reading {window} from {len(resolved_sources)} source(s) '
          f'via the {route} route:', file=sys.stderr)
    for path in resolved_sources:
        print(f'  {path}', file=sys.stderr)

    scans = discover_all(resolved_sources, start=start, end=end)
    mapping = number_scans(scans)

    # Before anything is written. An import that finds nothing and reports
    # success is the worst outcome available here: the next thing the
    # scientist does is look for data that was never written. A mistyped
    # --from is enough to cause it, and overwriting the mapping table on the
    # way out would also destroy the renumbering baseline below.
    if not mapping:
        raise RuntimeError(
            f'no scans found in {window} under '
            f'{", ".join(str(p) for p in resolved_sources)}')

    # Numbering is chronological across every source, so a scan that turns up
    # between two already-imported ones shifts every number after it -- and
    # spice anchors point at numbers. migrate_spice cannot absorb that: it
    # migrates date/time -> r-number, not r-number -> r-number. Detected here
    # rather than fixed; the fix is its own piece of work.
    #
    # Checked *before* the existing-output guard below, and before anything is
    # written. Otherwise a real local re-import hits FileExistsError first, the
    # user reaches for --force, and --force turns this into a warning that
    # scrolls past among the mapping table -- so the one case the check exists
    # for would be the one case it never fires in.
    mapping_path = out / 'scan-mapping.csv'
    moved = renumbered(read_mapping_table(mapping_path), mapping)
    if moved:
        shown = '; '.join(f'{src}: {old} -> {new}' for src, old, new in moved[:5])
        more = f' (and {len(moved) - 5} more)' if len(moved) > 5 else ''
        if not force:
            raise RuntimeError(
                f'{len(moved)} scan(s) would change number since the last import: '
                f'{shown}{more}. Spice anchors point at these numbers. Re-run with '
                f'--force once you have checked that nothing depends on them.')
        logger.warning('%d scan(s) change number since the last import: %s%s',
                        len(moved), shown, more)

    if s3_bucket is None:
        existing = existing_output(proposal_root, single_file=single_file, output_root=out)
        if existing is not None:
            if not force:
                raise FileExistsError(existing)
            if dry_run:
                logger.warning('dry run: would remove existing import output: %s', existing)
            else:
                logger.warning('removing existing import output: %s', existing)
                if existing.is_dir():
                    shutil.rmtree(existing)
                else:
                    existing.unlink()

    # Both, and separately: with sources elsewhere the workspace holds no raw
    # data and so usually does not exist yet, and `out` is a different
    # directory whenever --output-root is given. Creating only `out` would
    # leave a nonexistent path going to pm as the positional argument, and no
    # place for the spice folder to live.
    proposal_root.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    write_mapping_table(mapping, mapping_path)
    print(format_mapping_table(mapping))

    # The whole mapping as one argv element would meet the Windows command
    # line's 32767-character ceiling: 134 real scans off the UNC share already
    # come to ~14000, and a longer beamtime would fail as an opaque OS error
    # rather than as anything a user could act on. Written next to the mapping
    # table instead -- no threshold, so the path that runs at scale is the one
    # that runs always.
    resolved_path = out / 'resolved-scans.json'
    resolved_path.write_text(json.dumps(
        [[key, str(sf.path), sf.scan_type] for key, sf in mapping]), encoding='utf-8')

    target_url = target_url_for(proposal_root, single_file=single_file, output_root=out,
                                 s3_bucket=s3_bucket)
    spice_url = spice_url_for(proposal_root, spice_dir, s3_bucket=s3_bucket)

    # Checked here rather than beside the upload below, so that a refusal costs
    # nothing: the upload happens after the scan import, and finding out that
    # spice is write-protected only then would mean waiting out a full proposal
    # upload first.
    if (s3_bucket is not None and spice_dir.is_dir() and not dry_run
            and not force_spice and s3.prefix_exists(fs, spice_url)):
        raise FileExistsError(
            f'{spice_url} already holds objects; re-run with --force-spice to '
            f'overwrite it with the local {spice_dir}')

    if s3_bucket is not None:
        command, skipped, imported = _s3_import_loop(
            fs=fs, pm=pm, runner=runner, mapping=mapping, proposal_root=proposal_root,
            target_url=target_url, bucket=s3_bucket, photon_energy=photon_energy,
            force=force, dry_run=dry_run, s3_profile=s3_profile)
    else:
        single = [*pm, *(['-d'] if dry_run else []), '-P', '-u', target_url,
                  'scan', 'import', str(proposal_root),
                  '-R', 'pxs_import.reader:scans_from_pxs',
                  f'photon_energy={photon_energy}', f'resolved_file={resolved_path}',
                  '-W', *(['hdf5'] if single_file else []), 'frame_guard=None']
        logger.info('running: %s', ' '.join(single))
        runner(single, check=True)
        command, skipped, imported = [single], [], [key for key, _ in mapping]

    spice_changes: list[SpiceChange] = []
    if spice_dir.is_dir():
        fix_recipes(spice_dir, dry_run=dry_run)
        key_mapping = {sf.source: key for key, sf in mapping}
        spice_changes = migrate_spice(spice_dir, key_mapping, dry_run=dry_run)
        if s3_bucket is not None and not dry_run:
            uploaded = s3.upload_dir(fs, spice_dir, spice_url)
            print(f'spice: uploaded {uploaded} file(s) to {spice_url}')
    elif s3_bucket is not None:
        print(f'spice: no {spice_dir} folder, so {spice_url} was not uploaded')

    print(f"Proposal(url='{target_url}', spice='{spice_url}')")

    return PrepareResult(mapping=mapping, target_url=target_url, spice_url=spice_url,
                          command=command, skipped=skipped, imported=imported,
                          spice_changes=spice_changes)


def _default_prompt(label: str, *, secret: bool = False, default: str | None = None) -> str:
    '''Ask the user for one value on the terminal.

    Args:
        label: What to ask for.
        secret: If true, read with :func:`getpass.getpass` so the value is
            never echoed to the terminal or captured in scrollback.
        default: Shown in brackets and returned when the user just presses
            Enter.

    Returns:
        The entered value, stripped, or ``default`` for empty input.
    '''
    import getpass
    shown = f'{label} [{default}]: ' if default else f'{label}: '
    raw = (getpass.getpass(shown) if secret else input(shown)).strip()
    return default if (not raw and default is not None) else raw


def s3_login(*, profile: str | None = None,
              bucket: str = s3.S3_BUCKET_DEFAULT,
              home: Path | None = None,
              prompt=None) -> int:
    '''Collect S3 credentials once and store them in ``~/.aws``.

    Writing a standard AWS profile, rather than exporting environment
    variables, is what lets a bare ``nx5d.Proposal('s3://...')`` in a
    notebook authenticate with no preamble: botocore reads the key, the
    endpoint and the region from the profile.

    Args:
        profile: Profile name to write. ``None`` means ``default``, which
            is the one botocore picks with no ``AWS_PROFILE`` set.
        bucket: Bucket to list as a verification that the credentials work.
        home: Home directory, as in :func:`pxs_import.s3.aws_paths`.
        prompt: Callable ``(label, *, secret=False, default=None) -> str``
            used to ask for each value; defaults to :func:`_default_prompt`.
            Injected by tests.

    Returns:
        ``0`` on success; ``1`` if the user declined to overwrite an
        existing profile, a target file could not be parsed, or the written
        credentials could not reach the bucket. The secret is never printed,
        logged, or included in an error message.
    '''
    if prompt is None:
        prompt = _default_prompt
    name = profile or 'default'

    if s3.profile_configured(name, home=home):
        if prompt(f'profile "{name}" already exists; overwrite? [y/N]',
                  default='n').lower() not in ('y', 'yes'):
            print(f'profile "{name}" left unchanged')
            return 1

    key_id = prompt('AWS access key ID')
    secret = prompt('AWS secret access key', secret=True)
    endpoint = prompt('S3 endpoint URL', default=s3.S3_ENDPOINT_DEFAULT)
    region = prompt('Region', default=s3.S3_REGION_DEFAULT)

    try:
        credentials, config = s3.write_profile(
            key_id, secret, endpoint=endpoint, region=region, profile=name, home=home)
    except ValueError as exc:
        print(exc)
        return 1
    print(f'wrote profile "{name}" to {credentials} and {config}')

    try:
        s3.filesystem(profile).ls(f's3://{bucket}')
    except Exception as exc:
        print(f'but could not reach s3://{bucket} with it: {exc}\n'
              f'check the values in {credentials} and {config}, then run --s3-login again')
        return 1

    print(f'verified: s3://{bucket} is reachable')
    return 0


def _validate_args(parser, args) -> None:
    '''Reject flag combinations argparse cannot express on its own.

    Args:
        parser: The parser, used for its ``.error()`` (which exits 2).
        args: The parsed namespace.
    '''
    if args.s3_login:
        others = {'proposal_root': args.proposal_root is not None,
                  '--single-file': args.single_file, '--force': args.force,
                  '--dry-run': args.dry_run, '--output-root': args.output_root is not None,
                  '--spice-dir': args.spice_dir is not None, '--s3': args.s3,
                  '--s3-bucket': args.s3_bucket is not None,
                  '--force-spice': args.force_spice,
                  '--photon-energy': args.photon_energy != 8048.0}
        named = sorted(name for name, given in others.items() if given)
        if named:
            parser.error(f'--s3-login takes no other flag except --s3-profile; '
                          f'got {", ".join(named)}')
        return

    if args.proposal_root is None:
        parser.error('proposal_root is required unless --s3-login is given')
    if args.s3 and args.single_file:
        parser.error('--s3 and --single-file are mutually exclusive: the S3 layout is '
                      'one Zarr store per scan')
    if args.s3 and args.output_root is not None:
        parser.error('--output-root names a local directory and is meaningless with --s3')
    if not args.s3:
        for name, given in (('--s3-bucket', args.s3_bucket is not None),
                            ('--force-spice', args.force_spice)):
            if given:
                parser.error(f'{name} requires --s3')


def main(argv: list[str] | None = None) -> int:
    '''CLI entry point for the ``pxs-prepare`` console script.

    Args:
        argv: Argument list, excluding the program name (defaults to
            ``sys.argv[1:]`` via :mod:`argparse`).

    Returns:
        ``0`` on success, ``1`` if the target already has import output and
        ``--force`` was not given.
    '''
    parser = argparse.ArgumentParser(
        prog='pxs-prepare',
        description='Discover, number and import a UDKM proposal into nx5d, '
                     'migrating any existing spice folder to the new run-number anchors.')
    parser.add_argument('proposal_root', type=Path, nargs='?', default=None,
                         help='Proposal root directory, or a single date directory. '
                              'Required unless --s3-login is given.')
    parser.add_argument('--single-file', action='store_true',
                         help='Import every scan into one .h5 file with #{scan} groups.')
    parser.add_argument('--force', action='store_true',
                         help='Remove any pre-existing import output before writing '
                              '(has no effect during --dry-run; see --dry-run).')
    parser.add_argument('--dry-run', action='store_true',
                         help='Pass -d to pm and dry-run spice migration; never removes '
                              'existing output even with --force. The mapping table is '
                              'still written and pm is still invoked, in its own dry-run mode.')
    parser.add_argument('--source', type=Path, action='append', default=None,
                         metavar='DIR', dest='sources',
                         help='A folder holding measurement days, repeatable. Overrides '
                              'both the proposal folder and the base path.')
    parser.add_argument('--base', type=Path, default=None, metavar='DIR',
                         help='Parent of the acquisition-program folders '
                              '(PilatusTheta2Theta, ...). Defaults to $UDKM_BASE, then to '
                              'whichever built-in preset exists on this machine.')
    parser.add_argument('--from', dest='date_from', default=None, metavar='YYYY-MM-DD',
                         help='Earliest measurement day to import, inclusive. Required '
                              'when reading from the program folders.')
    parser.add_argument('--to', dest='date_to', default=None, metavar='YYYY-MM-DD',
                         help='Latest measurement day to import, inclusive. '
                              'Default: today.')
    parser.add_argument('--photon-energy', type=float, default=8048.0,
                         help='Photon energy (eV) forwarded to the reader. Default: 8048.0.')
    parser.add_argument('--output-root', type=Path, default=None,
                         help='Where the import lands, if different from proposal_root.')
    parser.add_argument('--spice-dir', type=Path, default=None,
                         help="The proposal's spice folder. Default: <proposal_root>/spice.")
    parser.add_argument('--s3', action='store_true',
                         help='Import to S3 instead of the local filesystem: one Zarr '
                              'store per scan under s3://<bucket>/<proposal>/. Requires '
                              'credentials from a previous --s3-login run.')
    parser.add_argument('--s3-bucket', default=None, metavar='NAME',
                         help=f'Bucket for both data and spice. Requires --s3. '
                              f'Default: {s3.S3_BUCKET_DEFAULT}.')
    parser.add_argument('--s3-profile', default=None, metavar='NAME',
                         help='AWS profile to use. Default: the standard botocore chain, '
                              'i.e. the "default" profile --s3-login writes.')
    parser.add_argument('--force-spice', action='store_true',
                         help='Overwrite an existing remote spice/ prefix. Requires --s3. '
                              'Not implied by --force.')
    parser.add_argument('--s3-login', action='store_true',
                         help='Store S3 credentials in ~/.aws and exit. Takes no other '
                              'flag except --s3-profile.')
    args = parser.parse_args(argv)

    _validate_args(parser, args)

    if args.s3_login:
        return s3_login(profile=args.s3_profile)

    try:
        prepare_proposal(args.proposal_root, single_file=args.single_file,
                          force=args.force, dry_run=args.dry_run,
                          photon_energy=args.photon_energy,
                          output_root=args.output_root, spice_dir=args.spice_dir,
                          s3_bucket=(args.s3_bucket or s3.S3_BUCKET_DEFAULT)
                                    if args.s3 else None,
                          s3_profile=args.s3_profile,
                          force_spice=args.force_spice,
                          sources=args.sources, base=args.base,
                          start=_as_daystamp(args.date_from),
                          # Today, not "no upper bound": --to is documented as
                          # defaulting to today, and that is what you want while
                          # still measuring. Filled in here rather than in
                          # prepare_proposal, so that the GUI shell of the same
                          # function does not inherit a clock-dependent default
                          # it never asked for.
                          end=_as_daystamp(args.date_to)
                              or datetime.now().strftime('%Y%m%d'))
    except FileExistsError as exc:
        if args.s3:
            # The S3 path raises with a complete, actionable message of its own
            # (which flag to pass, and for which prefix); the local path raises
            # a bare path, so only that one needs the advice appended.
            print(exc, file=sys.stderr)
        else:
            print(f'{exc} already exists; re-run with --force to replace it', file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
