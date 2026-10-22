import logging
from pathlib import Path

import pytest

from pxs_import.format import number_scans
from pxs_import.sources import (BASE_PRESETS, PROGRAM_DIRS, SourcesNotFound,
                                ScanTypeConflict, resolve_base, program_dirs,
                                resolve_sources, is_date_dir, has_date_dirs,
                                discover_all, _probe_order)
from fixtures import write_alignment, write_trss


def test_resolve_base_prefers_explicit_then_env_then_preset():
    # The preset has to be one this platform actually probes, or the
    # platform filter -- which the next test pins -- would hide it.
    probed = _probe_order()[0]

    def exists(p):
        return str(p) == probed

    assert resolve_base('/given', environ={}, exists=exists) == Path('/given')
    assert resolve_base(None, environ={'UDKM_BASE': '/env'}, exists=exists) == Path('/env')
    assert resolve_base(None, environ={}, exists=exists) == Path(probed)


def test_pxs_base_is_accepted_as_an_alias():
    """The package is pxs-import now; PXS_BASE is the name a new user guesses."""
    assert resolve_base(None, environ={'PXS_BASE': '/env'},
                        exists=lambda p: False) == Path('/env')


def test_udkm_base_wins_when_both_are_set():
    """UDKM_BASE is what the lab machines already export; it keeps precedence."""
    assert resolve_base(None, environ={'UDKM_BASE': '/old', 'PXS_BASE': '/new'},
                        exists=lambda p: False) == Path('/old')


def test_resolve_base_probes_only_platform_shaped_presets():
    '''A UNC probe on POSIX, or a POSIX probe on Windows, is wasted and slow.'''
    seen = []

    def exists(p):
        seen.append(str(p))
        return False

    resolve_base(None, environ={}, exists=exists)
    assert all(s in BASE_PRESETS for s in seen)
    assert len(seen) < len(BASE_PRESETS)


def test_resolve_base_returns_none_when_nothing_exists():
    assert resolve_base(None, environ={}, exists=lambda p: False) is None


def test_program_dirs_keeps_declared_order_and_skips_absent(tmp_path):
    for name in ('PilatusScanZaber', 'PilatusTheta2Theta'):
        (tmp_path / name).mkdir()
    assert [p.name for p in program_dirs(tmp_path)] == [
        'PilatusTheta2Theta', 'PilatusScanZaber']


def test_is_date_dir_and_has_date_dirs(tmp_path):
    day = tmp_path / '20250408'
    day.mkdir()
    assert is_date_dir(day)
    assert not is_date_dir(tmp_path)
    assert has_date_dirs(tmp_path)
    assert not has_date_dirs(day)
    year = tmp_path / 'years' / '2026'
    year.mkdir(parents=True)
    assert has_date_dirs(year.parent)


def test_explicit_sources_win(tmp_path):
    src = tmp_path / 'elsewhere' / '20250408' / '151027'
    write_alignment(src, n_rows=2)
    ws = tmp_path / 'ws' / '20250408' / '151027'
    write_alignment(ws, n_rows=2)
    got, route = resolve_sources(ws.parent.parent, explicit=[src.parent.parent])
    assert route == 'explicit' and got == [src.parent.parent.resolve()]


def test_positional_that_is_itself_a_date_dir_is_the_source(tmp_path):
    '''Review Focus 1: notebooks/20250408 must not fall through to the base.'''
    day = tmp_path / '20250408'
    write_alignment(day / '151027', n_rows=2)
    got, route = resolve_sources(day, base=tmp_path / 'nonexistent')
    assert route == 'positional' and got == [day.resolve()]


def test_positional_holding_date_dirs_is_the_source(tmp_path):
    write_alignment(tmp_path / 'prop' / '20250408' / '151027', n_rows=2)
    got, route = resolve_sources(tmp_path / 'prop')
    assert route == 'positional'


def test_empty_workspace_falls_through_to_the_base(tmp_path):
    ws = tmp_path / 'prop'
    ws.mkdir()
    base = tmp_path / 'base'
    (base / 'PilatusTheta2Theta').mkdir(parents=True)
    got, route = resolve_sources(ws, base=base)
    assert route == 'base' and got == [(base / 'PilatusTheta2Theta').resolve()]


def test_nothing_found_raises_naming_the_probed_paths(tmp_path):
    ws = tmp_path / 'prop'
    ws.mkdir()
    with pytest.raises(SourcesNotFound) as e:
        resolve_sources(ws, environ={}, exists=lambda p: False)
    assert BASE_PRESETS[0] in str(e.value)


def test_a_wrong_explicit_base_is_named_and_presets_are_not(tmp_path):
    '''The user passed --base; listing presets that were never probed misleads.'''
    ws = tmp_path/'prop'
    ws.mkdir()
    typo = tmp_path/'typo'
    with pytest.raises(SourcesNotFound) as e:
        resolve_sources(ws, base=typo, environ={})
    assert str(typo) in str(e.value)
    assert BASE_PRESETS[0] not in str(e.value)


def test_discover_all_numbers_chronologically_across_sources(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '120000', n_rows=2)
    write_alignment(b / '20250408' / '110000', n_rows=2)
    write_alignment(a / '20250409' / '090000', n_rows=2)
    got = discover_all([a, b])
    assert [s.source for s in got] == [
        '20250408/110000', '20250408/120000', '20250409/090000']
    assert [key for key, _ in number_scans(got)] == ['r0001', 'r0002', 'r0003']


def test_discover_all_stamps_the_source_root(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '120000', n_rows=2)
    write_alignment(b / '20250409' / '090000', n_rows=2)
    assert {s.root for s in discover_all([a, b])} == {a.resolve(), b.resolve()}


def test_discover_all_coalesces_identical_duplicates_with_a_warning(tmp_path, caplog):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '151027', n_rows=2)
    write_alignment(b / '20250408' / '151027', n_rows=2)
    with caplog.at_level(logging.WARNING):
        got = discover_all([a, b])
    assert [s.source for s in got] == ['20250408/151027']
    assert got[0].root == a.resolve()          # first source wins
    assert str(a) in caplog.text and str(b) in caplog.text


def test_discover_all_refuses_a_type_conflict(tmp_path):
    '''Review Focus 4: same key, different scan type, is not a duplicate.'''
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250408' / '151027', n_rows=2)
    write_trss(b / '20250408' / '151027', n_rows=2)
    with pytest.raises(ScanTypeConflict, match='20250408/151027'):
        discover_all([a, b])


def test_discover_all_applies_the_date_range_to_every_source(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    write_alignment(a / '20250407' / '120000', n_rows=2)
    write_alignment(b / '20250409' / '090000', n_rows=2)
    assert discover_all([a, b], start='20250408') == discover_all([b])
