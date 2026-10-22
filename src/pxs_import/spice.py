'''Migration of nx5d "spice" records from UDKM scan-folder keys to run numbers.

A spice folder holds one JSON object per file, each file named by a random
UUID with no extension (``handle``, ``anchor``, ``revision``, ``data``,
usually ``uuid``). ``anchor`` ties a record to a scan: either ``''`` (a
proposal-wide default), a run number already in the new ``r0001`` scheme, or
-- before migration -- an old ``'YYYYMMDD/HHMMSS'`` scan-folder source as
produced by :mod:`pxs_import.format`. Scans are renumbered chronologically
(Task 1's ``number_scans``), so existing spice records must be rewritten to
point at the new keys before nx5d can resolve them.

This module is standalone and stdlib-only: it never imports ``nx5d``,
``xraydc`` or ``silx``, so it can migrate a spice folder without pulling
in the rest of the import or cooking stack.
'''

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_RUN_RE = re.compile(r'^r\d{4}$')


def load_records(spice_dir: Path) -> list[tuple[Path, dict]]:
    '''Load every spice record in ``spice_dir``.

    Only regular files directly inside ``spice_dir`` are considered --
    subdirectories (e.g. ``.ipynb_checkpoints``) are never descended into.
    A file that is not valid JSON, or not decodable as UTF-8, is skipped
    with a DEBUG log entry rather than raising. A file that *is* valid JSON
    but is not a record -- not a ``dict``, or a ``dict`` with no ``anchor``
    key -- is likewise skipped, with a WARNING naming the file, rather than
    raising later when callers assume every record has one.

    Args:
        spice_dir: Directory containing spice record files.

    Returns:
        ``[(path, record_dict), ...]`` for every file that parsed as a spice
        record, in directory-iteration order.
    '''
    records = []
    for path in spice_dir.iterdir():
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug('skipping non-JSON spice file %s', path)
            continue
        if not isinstance(record, dict) or 'anchor' not in record:
            logger.warning('skipping spice file with no anchor: %s', path)
            continue
        records.append((path, record))
    return records


def target_key_for(anchor: str | int, mapping: dict[str, str]) -> str | int:
    '''Resolve an old spice ``anchor`` to its new run-number key.

    A non-``str`` anchor (nx5d's own schema allows ``anchor: Or(str, int)``)
    is not a UDKM ``YYYYMMDD/HHMMSS`` anchor and is returned unchanged.
    ``anchor`` already in the new scheme (``''`` or matching ``^r\\d{4}$``)
    is likewise returned unchanged. Otherwise it is resolved by exact match
    against ``mapping``'s keys (old scan sources), falling back to "most
    recent record at or before the scan": the first source that sorts ``>=
    anchor``.

    Args:
        anchor: The record's current ``anchor`` value.
        mapping: ``{old_source: new_key}``, as produced from Task 1's
            ``number_scans`` output.

    Returns:
        The new anchor value.

    Raises:
        ValueError: ``anchor`` sorts after every source in ``mapping``.
    '''
    if not isinstance(anchor, str):
        return anchor
    if anchor == '' or _RUN_RE.match(anchor):
        return anchor
    if anchor in mapping:
        return mapping[anchor]
    for source in sorted(mapping):
        if source >= anchor:
            return mapping[source]
    raise ValueError(f'anchor {anchor!r} is after every scan')


def _ensure_backup(spice_dir: Path) -> Path | None:
    '''Back up ``spice_dir`` once, before the first write of a migration.

    Copies ``spice_dir`` to ``spice_dir.with_name(spice_dir.name +
    '.pre-migration')``, unless that directory already exists (from an
    earlier call, in this run or a previous one) -- so repeated calls across
    :func:`fix_recipes` and :func:`migrate_spice` in the same run only ever
    copy once. Never call this during a dry run and never when there is
    nothing to write; callers are responsible for both checks.

    Args:
        spice_dir: Directory to back up.

    Returns:
        The backup directory, or ``None`` if it already existed (no copy was
        made).
    '''
    backup_dir = spice_dir.with_name(spice_dir.name + '.pre-migration')
    if backup_dir.exists():
        return None
    shutil.copytree(spice_dir, backup_dir)
    return backup_dir


@dataclass
class SpiceChange:
    '''One anchor rewrite applied (or, for a dry run, that would be applied).

    Args:
        path: Path to the record file.
        handle: The record's ``handle`` field.
        old_anchor: The anchor value before migration.
        new_anchor: The anchor value after migration.
    '''

    path: Path
    handle: str
    old_anchor: str
    new_anchor: str


def migrate_spice(spice_dir: Path,
                   mapping: dict[str, str],
                   *,
                   dry_run: bool = False,
                   backup: bool = True) -> list[SpiceChange]:
    '''Rewrite every spice record's ``anchor`` to the new run-number scheme.

    Only the ``anchor`` field is changed; ``revision``, ``uuid`` and every
    other field are preserved verbatim. Idempotent: once every anchor is
    already in the new scheme, this returns ``[]`` and writes nothing.

    Args:
        spice_dir: Directory containing spice record files.
        mapping: ``{old_source: new_key}``, as produced from Task 1's
            ``number_scans`` output.
        dry_run: If true, compute the changes but write nothing and make no
            backup.
        backup: If true (default) and there is at least one change, copy
            ``spice_dir`` to ``spice_dir.with_name(spice_dir.name +
            '.pre-migration')`` before writing -- once; skipped if that
            directory already exists.

    Returns:
        The changes applied (or, for a dry run, that would be applied).
    '''
    records = load_records(spice_dir)
    changes = []
    for path, record in records:
        old_anchor = record['anchor']
        new_anchor = target_key_for(old_anchor, mapping)
        if new_anchor != old_anchor:
            changes.append(SpiceChange(path=path, handle=record['handle'],
                                        old_anchor=old_anchor, new_anchor=new_anchor))

    if not changes or dry_run:
        return changes

    if backup:
        _ensure_backup(spice_dir)

    records_by_path = dict(records)
    for change in changes:
        record = records_by_path[change.path]
        record['anchor'] = change.new_anchor
        change.path.write_text(json.dumps(record, indent=2), encoding='utf-8')

    return changes


def fix_recipes(spice_dir: Path, *, dry_run: bool = False, backup: bool = True) -> bool:
    '''Remove the obsolete ``xRSS`` key from every ``recipes`` record.

    Args:
        spice_dir: Directory containing spice record files.
        dry_run: If true, detect the change but write nothing (and make no
            backup).
        backup: If true (default) and there is at least one change, back up
            ``spice_dir`` via :func:`_ensure_backup` before writing anything.

    Returns:
        Whether any record's ``data`` contained ``xRSS`` (and so would be,
        or was, changed).
    '''
    to_fix = []
    for path, record in load_records(spice_dir):
        if record.get('handle') != 'recipes':
            continue
        data = record.get('data')
        if data is None or 'xRSS' not in data:
            continue
        to_fix.append((path, record, data))

    if not to_fix or dry_run:
        return bool(to_fix)

    if backup:
        _ensure_backup(spice_dir)

    for path, record, data in to_fix:
        del data['xRSS']
        path.write_text(json.dumps(record, indent=2), encoding='utf-8')

    return True
