import csv
import json
import pytest
from pxs_import.reader import scans_from_pxs, format_mapping_table
from fixtures import write_srsm, write_trss, write_xrss, write_alignment

def _proposal(root):
    write_xrss(root/'20260202'/'135936', n_rows=3)
    write_trss(root/'20260203'/'120401', n_rows=2)
    write_srsm(root/'20260202'/'153026', n_rows=2)
    (root/'20260202'/'153026.zip').write_bytes(b'PK')
    return root

def test_reader_numbers_chronologically_across_dates(tmp_path):
    out = list(scans_from_pxs(str(_proposal(tmp_path)), mapping_file=str(tmp_path/'map.csv')))
    keys = [k for k, _ in out]
    assert [k['scan'] for k in keys] == ['r0001', 'r0002', 'r0003']
    assert [k['udkm_source'] for k in keys] == ['20260202/135936', '20260202/153026', '20260203/120401']
    assert [k['scan_type'] for k in keys] == ['xRSS', 'sRSM', 'tRSS']
    assert out[2][1].attrs['k-scan-type'] == 'tRSS' and out[2][1]['instrument/pilatus/images'].shape[0] == 2
    rows = list(csv.DictReader(open(tmp_path/'map.csv')))
    assert rows[1] == {'scan': 'r0002', 'source': '20260202/153026', 'type': 'sRSM',
                       'root': str(tmp_path)}

def test_photon_energy_override_reaches_scan(tmp_path):
    _proposal(tmp_path)
    (_, h5), = scans_from_pxs(tmp_path/'20260203', photon_energy=8041.0)   # one scan in that date
    assert h5.attrs['photon_energy'] == 8041.0

def test_mapping_table_printed_to_stderr(tmp_path, capsys):       # F4
    list(scans_from_pxs(str(_proposal(tmp_path))))
    captured = capsys.readouterr()
    assert 'r0001 <- 20260202/135936 (xRSS)' in captured.err

def test_bad_scan_raises_runtime_error_naming_scan(tmp_path):      # F5 (covers T4's zero-frame xRSS too)
    write_xrss(tmp_path/'20260202'/'135936', n_rows=3, n_tifs=0)
    with pytest.raises(RuntimeError, match='r0001') as exc_info:
        list(scans_from_pxs(str(tmp_path)))
    assert '135936' in str(exc_info.value)

def test_format_mapping_table_line():
    from pathlib import Path
    from pxs_import.format import ScanFolder
    line = format_mapping_table([('r0001', ScanFolder(Path('x'), '20260108', '100331', 'xRSS'))])
    assert line.strip() == 'r0001 <- 20260108/100331 (xRSS)'


def test_only_filters_without_loading_other_scans(tmp_path, monkeypatch):
    import pxs_import.reader as u
    loaded = []
    real = u.load_stack
    monkeypatch.setattr(u, 'load_stack', lambda plan: (loaded.append(plan.folder), real(plan))[1])
    out = list(scans_from_pxs(str(_proposal(tmp_path)), only='r0002'))
    assert [k['scan'] for k, _ in out] == ['r0002']
    assert [k['iscan'] for k, _ in out] == [2]
    assert len(loaded) == 1


def test_only_accepts_comma_separated_and_rejects_unknown(tmp_path):
    root = _proposal(tmp_path)
    assert [k['scan'] for k, _ in scans_from_pxs(str(root), only='r0001,r0003')] == ['r0001', 'r0003']
    with pytest.raises(ValueError, match='r0099'):
        list(scans_from_pxs(str(root), only='r0099'))


def test_only_suppresses_mapping_table(tmp_path, capsys):
    list(scans_from_pxs(str(_proposal(tmp_path)), only='r0001'))
    assert capsys.readouterr().err == ''


def test_reader_uses_the_resolved_mapping_and_ignores_the_positional(tmp_path):
    a = tmp_path/'a'/'20250408'/'151027'
    write_alignment(a, n_rows=2)
    workspace = tmp_path/'ws'
    workspace.mkdir()
    resolved = json.dumps([['r0007', str(a), 'alignment']])
    got = list(scans_from_pxs(workspace, resolved=resolved))
    assert [k['scan'] for k, _ in got] == ['r0007']
    assert got[0][0]['udkm_source'] == '20250408/151027'


def test_reader_without_resolved_still_discovers(tmp_path):
    write_alignment(tmp_path/'20250408'/'151027', n_rows=2)
    got = list(scans_from_pxs(tmp_path))
    assert [k['scan'] for k, _ in got] == ['r0001']


def test_reader_accepts_an_already_decoded_resolved_value(tmp_path):
    '''pm decodes `key=value` itself and hands the reader a list, not a string.

    The only test that goes through a real `pm` caught this; every other
    test passed a JSON string straight in and never exercised the real path.
    '''
    a = tmp_path/'a'/'20250408'/'151027'
    write_alignment(a, n_rows=2)
    got = list(scans_from_pxs(tmp_path/'ws', resolved=[['r0007', str(a), 'alignment']]))
    assert [k['scan'] for k, _ in got] == ['r0007']


def test_reader_rejects_a_resolved_entry_whose_folder_is_gone(tmp_path):
    resolved = json.dumps([['r0001', str(tmp_path/'missing'), 'alignment']])
    with pytest.raises(RuntimeError, match='missing'):
        list(scans_from_pxs(tmp_path, resolved=resolved))
