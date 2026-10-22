'''Make ``tests/corpus`` out of the real UDKM proposal folders.

The corpus tests are the only ones that run against vocabularies, counts and
layouts nobody invented -- 22 GB of real measurement across seven proposal
folders. Committing that is out of the question, and dropping it loses the
only evidence the importer was ever right about real data. This script is the
compromise: it carries **every structure and every text file the tests read,
byte for byte**, and replaces only pixel content.

What survives untouched: the folder tree, ``parameters<sfx>.txt``,
``scan data<sfx>.txt``, ``spice/`` and ``spice.pre-migration/``, and the
signature ``.dat`` table of every scan a test parses. What is replaced: the
TIFF frames, and the data rows of the tables no test parses -- those keep
their real header lines, which is all :func:`~pxs_import.format.classify_scan`
and :func:`~pxs_import.format.discover_scans` ever look at.

:data:`RULES` is the decision, one row per carried folder, and
:class:`FolderRule` is its shape. Each row says which tables are carried
whole and what a frame is:

``READABLE``
    A real TIFF holding a constant frame **at the source frame's own shape
    and dtype**, read once per scan off its first real frame so detector
    geometry is never guessed. Needed wherever a test calls
    :func:`~pxs_import.frames.load_stack` or imports through nx5d. Every
    stub of one scan is byte-identical on purpose: git stores a single blob
    for all of them.
``PLACEHOLDER``
    A zero-byte file. :func:`~pxs_import.frames.plan_frames` decides what
    belongs to a scan by asking the filesystem for names, never by opening a
    file, so the counting tests need the names and nothing behind them.
``ABSENT``
    No frames at all, for folders whose tests stop at discovery.

Two things this script does *not* do. It never deletes, so a rule changed
from ``READABLE`` to ``PLACEHOLDER`` would leave the old stub behind --
regenerate into an empty directory when a rule changes. And it carries the
source's own line endings, which are CRLF: ``.gitattributes`` normalises
them to LF on commit, so a fresh clone holds LF text and re-running this
script over it reports every text file as modified. Nothing downstream
cares, because :func:`~pxs_import.format.parse_dat` and
:func:`~pxs_import.format.parse_parameters` both read binary and strip the
CR, and ``tests/tools_test.py`` pins exactly that.

Usage::

    python tools/thin_corpus.py --source <...>/notebooks --out tests/corpus
'''

import argparse
import io
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy
import tifffile

from pxs_import.format import ScanFolder, discover_scans

#: A real TIFF of a constant frame, at the source frame's shape and dtype.
READABLE = 'R'

#: A zero-byte file: the name is the whole content.
PLACEHOLDER = 'Z'

#: No frame files at all.
ABSENT = '-'

_FLAVOURS = (READABLE, PLACEHOLDER, ABSENT)

#: :attr:`FolderRule.dat` value carrying every scan's table verbatim.
ALL = 'all'

#: :attr:`FolderRule.dat` value carrying no table verbatim (header only).
NONE = 'none'

#: Proposal-level directories carried verbatim beside the scans.
SPICE_DIRS = ('spice', 'spice.pre-migration')


@dataclass(frozen=True)
class FolderRule:
    '''What to carry out of one source folder.

    Args:
        dat: :data:`ALL` to carry every scan's signature table verbatim,
            :data:`NONE` to carry none of them, or a set of
            ``'YYYYMMDD/HHMMSS'`` scan identifiers naming the ones to carry.
            A table not carried verbatim is still written -- with its real
            header lines and no data rows -- because a scan folder without
            its signature file is not a scan folder at all.
        frames: One of :data:`READABLE`/:data:`PLACEHOLDER`/:data:`ABSENT`
            for every scan of the folder, or a mapping from scan identifier
            to flavour, defaulting to :data:`ABSENT` for scans it does not
            name.
    '''

    dat: str | frozenset[str] = NONE
    frames: str | Mapping[str, str] = ABSENT

    def carries_dat(self, source: str) -> bool:
        '''Whether scan ``source``'s signature table is carried verbatim.'''
        if isinstance(self.dat, str):
            return self.dat == ALL
        return source in self.dat

    def flavour(self, source: str) -> str:
        '''The frame flavour for scan ``source``.

        Raises:
            ValueError: The rule names a flavour that does not exist. A typo
                there would otherwise read as :data:`ABSENT` and carry a
                whole proposal's frames away without a word.
        '''
        got = (self.frames if isinstance(self.frames, str)
               else self.frames.get(source, ABSENT))
        if got not in _FLAVOURS:
            raise ValueError(f'{source}: unknown frame flavour {got!r}, '
                             f'expected one of {_FLAVOURS}')
        return got


#: The corpus, folder by folder. Every row is justified by a test in
#: ``tests/corpus_test.py``, and the comment above it says which: a row
#: downgraded without reading them is how a corpus stops proving anything.
RULES: dict[str, FolderRule] = {
    # Imported end to end through nx5d's `pm` and cooked, so every scan needs
    # its real table and real-shaped frames.
    '202601_PtAu': FolderRule(dat=ALL, frames=READABLE),

    # Classification, the parameter vocabulary, and three named frame-plan
    # cases: 120401's 751 unreferenced TIFFs, 180247's 7 NaN gaps (the only
    # one whose pixels are read), 125733's truncation at 489.
    '202602_FeAu': FolderRule(
        dat=frozenset({'20260203/120401', '20260212/180247', '20260213/125733'}),
        frames={'20260203/120401': PLACEHOLDER,
                '20260212/180247': READABLE,
                '20260213/125733': PLACEHOLDER}),

    # Classification, parameter vocabulary, and a spice migration that must
    # come out a no-op. No table is parsed and no frame is opened.
    '202608_DyCo5Pt': FolderRule(),

    # The date-range tests, which count scan folders and must never descend
    # into a day outside the range. Discovery only.
    '202504_NbO2': FolderRule(),

    # Alignment scans of both acquisition programs: parsed, counted against
    # their frames, and imported through the reader.
    '20250408': FolderRule(dat=ALL, frames=READABLE),
    '20250424': FolderRule(dat=ALL, frames=READABLE),

    # The cryostat log, present in six folders of one 2015 cooldown. All six
    # need their `scan data<sfx>.txt`; only 000051 is built into a scan.
    '20151226': FolderRule(dat=frozenset({'20151226/000051'}),
                           frames={'20151226/000051': READABLE}),
}


def thin(source_root: Path, out_root: Path, rules: Mapping[str, FolderRule]) -> None:
    '''Write the thinned corpus for every folder named in ``rules``.

    Args:
        source_root: Directory holding the real proposal folders.
        out_root: Directory to write the corpus into. Created as needed and
            never cleared (see the module docstring).
        rules: Folder name -> what to carry out of it.

    Raises:
        FileNotFoundError: A folder named in ``rules`` is not under
            ``source_root``. Carrying on would write a corpus silently
            missing a whole proposal.
        ValueError: A rule names a frame flavour that does not exist.
    '''
    for folder, rule in rules.items():
        source, out = Path(source_root) / folder, Path(out_root) / folder
        if not source.is_dir():
            raise FileNotFoundError(f'no such source folder: {source}')
        for scan in discover_scans(source):
            _thin_scan(scan, source, out, rule)
        for name in SPICE_DIRS:
            if (source / name).is_dir():
                shutil.copytree(source / name, out / name, dirs_exist_ok=True,
                                copy_function=shutil.copyfile)


def _thin_scan(scan: ScanFolder, source: Path, out: Path, rule: FolderRule) -> None:
    '''Write one scan folder's carried files.'''
    # Asked for before anything is written, so a typo in the rule table
    # costs nothing but the exception.
    flavour = rule.flavour(scan.source)
    folder = out / scan.path.relative_to(source)
    folder.mkdir(parents=True, exist_ok=True)

    for path in (scan.params_path, scan.temperature_path):
        if path.is_file():
            shutil.copyfile(path, folder / path.name)

    target = folder / scan.dat_path.name
    if rule.carries_dat(scan.source):
        shutil.copyfile(scan.dat_path, target)
    else:
        _write_header_only(scan.dat_path, target)

    _write_frames(scan, folder, flavour)


def _write_header_only(source: Path, target: Path) -> None:
    '''Copy a table's header lines and drop every data row.

    The header is the first line. ``sRSM`` declares a second one -- the
    ``% StartTime EndTime ImageName`` sub-header that
    :func:`~pxs_import.format.parse_dat` skips -- while ``tRSS`` puts those
    same three fields on a per-row line instead, so keeping every leading
    ``%`` line would carry the first row's own metadata along with it.

    Line endings are the source's: each kept line still carries the trailing
    CR that splitting on LF left on it.
    '''
    lines = source.read_bytes().split(b'\n')
    keep = lines[:1]
    if len(lines) > 1 and lines[1].startswith(b'% StartTime'):
        keep.append(lines[1])
    target.write_bytes(b'\n'.join(keep) + b'\n')


def _write_frames(scan: ScanFolder, folder: Path, flavour: str) -> None:
    '''Write one scan's frames, one per real frame name.

    The names come off the source folder rather than off its table on
    purpose: a scan's unreferenced TIFFs are part of what the frame-plan
    tests measure.
    '''
    if flavour == ABSENT:
        return
    names = sorted(p.name for p in scan.path.glob('*.tif'))
    if flavour == PLACEHOLDER:
        for name in names:
            (folder / name).touch()
        return
    if not names:
        return
    blob = _encode_stub(scan.path / names[0])
    for name in names:
        (folder / name).write_bytes(blob)


def _encode_stub(sample: Path) -> bytes:
    '''Encode one constant TIFF shaped like ``sample``.

    Encoded once per scan and written byte for byte to every one of its
    frames, so git stores a single blob however many there are. Deflate of a
    constant array is deterministic and well under a kilobyte, and nothing
    in the encoded file varies between runs -- which is what
    ``test_thin_is_idempotent`` pins.
    '''
    real = tifffile.imread(sample)
    buffer = io.BytesIO()
    tifffile.imwrite(buffer, numpy.ones(real.shape, dtype=real.dtype),
                     compression='deflate')
    return buffer.getvalue()


def _report(out_root: Path) -> str:
    '''One line: how many files the corpus holds and how large it is.'''
    files = [p for p in Path(out_root).rglob('*') if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    return f'{len(files)} files, {total / 1e6:.1f} MB in {out_root}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Build tests/corpus from the real UDKM proposal folders.')
    parser.add_argument('--source', required=True, type=Path,
                        help='directory holding the real proposal folders')
    parser.add_argument('--out', required=True, type=Path,
                        help='directory to write the corpus into')
    args = parser.parse_args(argv)

    thin(args.source, args.out, RULES)
    print(_report(args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
