'''Tests for ``tools/thin_corpus.py``, the script that makes ``tests/corpus``.

The corpus these tests guard is committed, so the tool is only ever run again
when a rule changes -- which is exactly when nobody remembers what it does.
Everything asserted here is a property the corpus's provenance claim rests on:
that text is carried byte for byte, that a frame is either a readable stub at
the real detector's shape or a name with nothing behind it, and that running
the tool again over its own output changes nothing.
'''

import hashlib

import numpy
import pytest
import tifffile

from pxs_import.format import parse_dat
from tools.thin_corpus import PLACEHOLDER, READABLE, FolderRule, thin

from fixtures import write_srsm, write_trss


def _build_source(root):
    '''Three scans of one synthetic proposal, one per frame flavour.'''
    prop = root / 'PROP'
    write_srsm(prop / '20260101' / '120000', n_rows=3, shape=(5, 7))
    write_trss(prop / '20260101' / '130000', n_rows=2, shape=(5, 7), extra_tifs=1)
    write_srsm(prop / '20260102' / '140000', n_rows=2, shape=(5, 7))
    return prop


#: Matches :func:`_build_source`: the first scan is carried whole, the second
#: keeps only its frame names, the third keeps no frames at all.
_RULES = {'PROP': FolderRule(
    dat=frozenset({'20260101/120000'}),
    frames={'20260101/120000': READABLE, '20260101/130000': PLACEHOLDER})}


def _digest(root):
    '''Relative path -> sha256 of its bytes, for every file under ``root``.'''
    return {p.relative_to(root).as_posix():
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def test_thin_keeps_text_and_shrinks_frames(tmp_path):
    source, out = tmp_path / 'source', tmp_path / 'out'
    prop = _build_source(source)
    thin(source, out, _RULES)

    src, dst = prop / '20260101' / '120000', out / 'PROP' / '20260101' / '120000'
    # Text is carried verbatim: the parameters file and, for this scan, the
    # table itself. A corpus assertion reading either one reads the real bytes.
    assert (dst / 'parameters120000.txt').read_bytes() \
        == (src / 'parameters120000.txt').read_bytes()
    assert (dst / 'rockingcurves120000.dat').read_bytes() \
        == (src / 'rockingcurves120000.dat').read_bytes()
    # Every frame name survives, and each one opens at the real frame's shape.
    assert sorted(p.name for p in dst.glob('*.tif')) \
        == sorted(p.name for p in src.glob('*.tif'))
    assert tifffile.imread(dst / 'image00000.tif').shape == (5, 7)

    src, dst = prop / '20260101' / '130000', out / 'PROP' / '20260101' / '130000'
    # Not named in `dat`, so the table keeps its header and loses its rows.
    assert (dst / 'scans130000.dat').read_bytes() \
        == (src / 'scans130000.dat').read_bytes().split(b'\n')[0] + b'\n'
    # Placeholders carry the names -- including the unreferenced third TIFF,
    # which is what `plan_frames` counts -- and nothing else.
    assert sorted(p.name for p in dst.glob('*.tif')) \
        == ['image00000.tif', 'image00001.tif', 'image00002.tif']
    assert {p.stat().st_size for p in dst.glob('*.tif')} == {0}

    dst = out / 'PROP' / '20260102' / '140000'
    assert list(dst.glob('*.tif')) == []


def test_a_stubbed_dat_keeps_its_metadata_sub_header(tmp_path):
    '''sRSM declares ``% StartTime...`` under its header; tRSS does not.

    Keeping one line would truncate an sRSM table's header, and keeping
    every leading ``%`` line would carry the first row's own metadata.
    '''
    source, out = tmp_path / 'source', tmp_path / 'out'
    _build_source(source)
    thin(source, out, _RULES)

    stub = (out / 'PROP' / '20260102' / '140000' / 'rockingcurves140000.dat')
    lines = stub.read_bytes().rstrip(b'\n').split(b'\n')
    assert len(lines) == 2
    assert lines[0].startswith(b'% Loop\t')
    assert lines[1].startswith(b'% StartTime\t')


def test_thin_carries_the_spice_directories(tmp_path):
    '''The spice anchors the migration tests read are not inside a scan.'''
    source, out = tmp_path / 'source', tmp_path / 'out'
    prop = _build_source(source)
    for name in ('spice', 'spice.pre-migration'):
        (prop / name).mkdir()
        (prop / name / 'a-uuid').write_bytes(b'{\r\n  "anchor": "20260101/120000"\r\n}\r\n')
    thin(source, out, _RULES)

    for name in ('spice', 'spice.pre-migration'):
        assert (out / 'PROP' / name / 'a-uuid').read_bytes() \
            == (prop / name / 'a-uuid').read_bytes()


def test_thin_is_idempotent(tmp_path):
    '''Re-running over an existing corpus must reproduce it exactly.

    This is the whole provenance claim: the committed tree is what the tool
    makes from the real proposals, and anyone can check that by running it
    again. A stub carrying a timestamp would break it silently.
    '''
    source, out = tmp_path / 'source', tmp_path / 'out'
    _build_source(source)

    thin(source, out, _RULES)
    first = _digest(out)
    thin(source, out, _RULES)

    assert _digest(out) == first
    assert first  # a tree that is empty is trivially idempotent


def test_thinned_dat_survives_lf_normalisation(tmp_path):
    '''`.gitattributes` forces LF, so the committed table is not the one written.

    The real tables are CRLF and Windows-1252 (``°`` in a column name). Git
    rewrites the line endings on commit; nothing may change but those.
    '''
    source, out = tmp_path / 'source', tmp_path / 'out'
    prop = _build_source(source)
    src = prop / '20260101' / '120000' / 'rockingcurves120000.dat'
    src.write_bytes(src.read_bytes().replace(b'deg.', 'deg.\xb0'.encode('windows-1252')))
    assert b'\r\n' in src.read_bytes()

    thin(source, out, _RULES)
    carried = out / 'PROP' / '20260101' / '120000' / 'rockingcurves120000.dat'
    before = parse_dat(carried, 'sRSM')

    carried.write_bytes(carried.read_bytes().replace(b'\r\n', b'\n'))
    after = parse_dat(carried, 'sRSM')

    assert list(after.columns) == list(before.columns)
    assert 'Theta / deg.\N{DEGREE SIGN}' in after.columns
    assert after.image_names == before.image_names
    numpy.testing.assert_array_equal(after.columns['Theta / deg.\N{DEGREE SIGN}'],
                                     before.columns['Theta / deg.\N{DEGREE SIGN}'])


def test_an_unknown_frame_flavour_is_refused(tmp_path):
    '''A typo in the rule table must not quietly carry nothing.'''
    source, out = tmp_path / 'source', tmp_path / 'out'
    _build_source(source)
    rules = {'PROP': FolderRule(frames='readable')}

    with pytest.raises(ValueError, match='readable'):
        thin(source, out, rules)
