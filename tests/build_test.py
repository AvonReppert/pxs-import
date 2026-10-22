'''Tests for :mod:`pxs_import.reader` -- assembling per-scan h5like groups.'''

import logging
import numpy
from pxs_import.format import ScanFolder, parse_dat, parse_parameters
from pxs_import.frames import plan_frames, load_stack
from pxs_import.reader import build_scan_h5like
from fixtures import (write_srsm, write_trss, write_xrss, write_alignment,
                      write_scan_temperatures)


def _build(folder, scan_type, **kw):
    sf = ScanFolder(folder, folder.parent.name, folder.name, scan_type)
    table = parse_dat(sf.dat_path, scan_type)
    plan = plan_frames(folder, table, scan_type)
    stack, missing = load_stack(plan)
    return build_scan_h5like(sf, table, parse_parameters(sf.params_path), plan, stack, missing, **kw)


def test_trss_layout_attrs_and_derived_ttheta(tmp_path):
    d = write_trss(tmp_path/'20260108'/'120821', n_rows=3, theta=19.48, correction=0.5,
                   params={'Comments': ['16nm Pt on MgO', '10.2mJ/cm²'], 'Exposure Time': '2.000000s'})
    h5 = _build(d, 'tRSS')
    assert h5['instrument/pilatus/images'].shape == (3, 4, 6)
    assert h5['instrument/pilatus/images'].dtype == numpy.float32
    assert h5['instrument/pilatus/curve'].shape == (3, 6)
    numpy.testing.assert_allclose(h5['instrument/motors/ttheta'][()], 2*19.48 + 0.5)
    for name in ('loop_delay', 'delay', 'loop_theta', 'crystal_v', 'crystal_calib',
                 'cntrate', 'repeats', 'image_number', 'frame_missing'):
        assert h5[f'measurement/{name}'].shape == (3,), name
    assert h5['measurement/loop_delay'].dtype.kind == 'i'
    a = h5.attrs
    assert a['k-scan-type'] == 'tRSS' and a['k-frame-guard'] == 'instrument/pilatus/images'
    assert a['k-primary:0'] == 'measurement/delay'
    assert a['notes'] == '16nm Pt on MgO 10.2mJ/cm²' and a['k-header:integration'] == 2.0
    assert a['k-header:description'] == '20260108/120821 16nm Pt on MgO 10.2mJ/cm²'
    assert a['photon_energy'] == 8048.0 and a['source'] == '20260108/120821'
    assert a['datetime'].startswith('20') and 'T' in a['datetime']
    # Only these two reach the listing; everything else must stay a plain attribute.
    assert not [k for k in a if k.startswith('k-header:')
                and k not in ('k-header:integration', 'k-header:description')]
    assert 'measurement_full' not in h5


def test_every_parameter_reaches_the_scan_under_its_own_key(tmp_path):
    """One attribute per parameters key, typed where there is a number."""
    d = write_trss(tmp_path/'20260108'/'120821', n_rows=2,
                   params={'Comments': ['16nm Pt on MgO'],
                           'Delay Vector': '-5:0.5:40',
                           'Inner Parameter': 'Theta',
                           '# of Loops': '80',
                           'File Name': '085055',
                           'Tmp Path': r'P:\PilatusTheta2Theta\2026\20260108\120821',
                           'Never Seen Before': 'carried anyway'})
    a = _build(d, 'tRSS').attrs
    # Typed where numeric_value finds a number ...
    assert a['# of Loops'] == 80 and isinstance(a['# of Loops'], int)
    assert a['Exposure Time'] == 2.0
    assert a['2 Theta Correction'] == 0.0
    assert list(a['ROI']) == [0, 0, 487, 195]
    # ... raw string where it does not, the unseen key included.
    assert a['Delay Vector'] == '-5:0.5:40'
    assert a['Inner Parameter'] == 'Theta'
    assert a['Date Time'] == '08.01.2026 12:08:21'
    assert a['Tmp Path'] == r'P:\PilatusTheta2Theta\2026\20260108\120821'
    assert a['Never Seen Before'] == 'carried anyway'
    # An HHMMSS stem stays a string even though it is all digits.
    assert a['File Name'] == '085055'
    # The derived forms this importer needs keep their own names.
    assert a['k-header:integration'] == 2.0 and a['datetime'] == '2026-01-08T12:08:21'
    # Comments are `notes`, and must not come back under their own key too.
    assert 'Comments' not in a and a['notes'] == '16nm Pt on MgO'
    # None of this may leak into the listing.
    assert sorted(k for k in a if k.startswith('k-header:')) == [
        'k-header:description', 'k-header:integration']


def test_reserved_names_win_over_a_colliding_parameters_key(tmp_path, caplog):
    """No real file does this, but the scan's own names must not be displaced."""
    d = write_trss(tmp_path/'20260108'/'120821', n_rows=2,
                   params={'Comments': ['real notes'], 'source': 'not the folder'})
    with caplog.at_level(logging.WARNING):
        a = _build(d, 'tRSS').attrs
    assert a['source'] == '20260108/120821' and a['notes'] == 'real notes'
    assert "'source'" in caplog.text and 'displaced' in caplog.text


def test_srsm_reads_ttheta_and_has_empty_notes(tmp_path):
    h5 = _build(write_srsm(tmp_path/'20260108'/'115550', n_rows=2), 'sRSM', photon_energy=8041.0)
    assert h5['instrument/motors/ttheta'].shape == (2,) and h5.attrs['notes'] == ''
    assert h5.attrs['k-header:description'] == '20260108/115550'
    assert h5.attrs['k-primary:0'] == 'instrument/motors/theta'
    assert float(h5['instrument/source/photon_energy'][0]) == 8041.0


def test_xrss_truncated_scan_keeps_full_table_with_suffixed_names(tmp_path):
    h5 = _build(write_xrss(tmp_path/'20260213'/'125733', n_rows=10, n_tifs=4), 'xRSS')
    assert h5['instrument/pilatus/images'].shape[0] == 4
    for name in ('delay_setpoint', 'delay', 'loop', 'counts', 'crystal_v', 'norm', 'crystal_calib'):
        assert h5[f'measurement/{name}'].shape == (4,)
        assert h5[f'measurement_full/{name}_full'].shape == (10,)
    assert h5.attrs['truncated'] is True and h5.attrs['rows'] == 10 and h5.attrs['frames'] == 4
    assert h5.attrs['k-primary:0'] == 'measurement/delay_setpoint'


def test_missing_parameters_file_still_builds(tmp_path):                   # Review Focus 3
    d = write_srsm(tmp_path/'20260108'/'115550', n_rows=2)
    (d/'parameters115550.txt').unlink()
    h5 = _build(d, 'sRSM')
    assert h5.attrs['notes'] == '' and 'k-header:integration' not in h5.attrs


def test_nan_in_integer_column_keeps_float_dtype_with_warning(tmp_path, caplog):   # M2
    d = write_trss(tmp_path/'20260108'/'120821', n_rows=2)
    p = d/'scans120821.dat'
    lines = p.read_text().split('\n')      # read_text() normalizes \r\n to \n
    for i, line in enumerate(lines):
        if line and not line.startswith('%'):
            fields = line.split('\t')
            fields[7] = 'NaN'          # Repeats is scalar column index 7
            lines[i] = '\t'.join(fields)
            break
    p.write_text('\r\n'.join(lines), encoding='utf-8', newline='')
    with caplog.at_level(logging.WARNING):
        h5 = _build(d, 'tRSS')
    assert h5['measurement/repeats'].dtype.kind == 'f'
    assert 'repeats' in caplog.text


def test_temperatures_become_variables_and_a_listing_attribute(tmp_path):
    d = write_srsm(tmp_path/'20151226'/'000051', n_rows=3)
    write_scan_temperatures(d, rows=[(12.15, 15.25), (108.00, 108.77)])
    h5 = _build(d, 'sRSM')
    numpy.testing.assert_allclose(h5['measurement/tcryo_readback'][()], 108.00)
    numpy.testing.assert_allclose(h5['measurement/tsample_readback'][()], 108.77)
    assert h5['measurement/tsample_readback'].shape == (3,)
    assert h5.attrs['k-header:tsample'] == 108.77


def test_no_temperature_file_is_silent(tmp_path, caplog):
    '''Absent in most of the roughly 140 real scans; a warning would be pure noise.'''
    d = write_srsm(tmp_path/'20260108'/'115550', n_rows=3)
    with caplog.at_level(logging.WARNING):
        h5 = _build(d, 'sRSM')
    assert 'measurement/tsample_readback' not in h5
    assert 'k-header:tsample' not in h5.attrs
    assert caplog.records == []


def test_temperature_from_another_scan_is_refused(tmp_path, caplog):
    '''The row carries its own stamp, so a mismatch is detectable -- and a
    temperature we cannot attribute to this scan is worse than none.'''
    d = write_srsm(tmp_path/'20151226'/'000051', n_rows=3)
    write_scan_temperatures(d, stamp=('20151225', '232952'))
    with caplog.at_level(logging.WARNING):
        h5 = _build(d, 'sRSM')
    assert 'measurement/tsample_readback' not in h5
    assert 'k-header:tsample' not in h5.attrs
    assert '20151225' in caplog.text


def test_alignment_layout_and_primary_axis(tmp_path):
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4)
    h5 = _build(d, 'alignment')
    numpy.testing.assert_allclose(h5['measurement/position'][()],
                                  [-1.0, -0.9, -0.8, -0.7])
    assert h5['measurement/loop'].dtype == numpy.int64
    assert h5['instrument/pilatus/images'].shape == (4, 4, 6)
    for name in ('counts', 'crystal_v', 'norm'):
        assert h5[f'measurement/{name}'].shape == (4,)
    assert h5.attrs['k-scan-type'] == 'alignment'
    assert h5.attrs['k-primary:0'] == 'measurement/position'


def test_alignment_short_run_writes_no_full_duplicates(tmp_path):
    '''Every real alignment scan stops early; that is not truncation.'''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4)
    h5 = _build(d, 'alignment')
    assert 'measurement_full' not in h5
    for attr in ('truncated', 'rows', 'frames'):
        assert attr not in h5.attrs


def test_alignment_publishes_motor_and_unit(tmp_path):
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4,
                        stage='X Linear', unit='mm')
    h5 = _build(d, 'alignment')
    assert h5.attrs['k-header:stage'] == 'X Linear'
    assert h5.attrs['k-header:unit'] == 'mm'
    assert h5.attrs['Stage'] == 'X Linear'          # bare sweep still runs


def test_alignment_without_stage_still_builds(tmp_path, caplog):
    '''Review Focus 3: the key is absent, not the whole file.'''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4,
                        stage=None, unit=None)
    with caplog.at_level(logging.WARNING):
        h5 = _build(d, 'alignment')
    assert 'k-header:stage' not in h5.attrs
    assert 'k-header:unit' not in h5.attrs
    assert 'stage' in caplog.text.lower()


def test_alignment_parameters_survive_non_ascii(tmp_path):
    '''Review Focus 4: real files are Windows-1252 and record degrees.'''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=4,
                        stage='Omega', unit='deg',
                        params={'Start Position': '-10.000000°'})
    h5 = _build(d, 'alignment')
    assert h5.attrs['k-header:stage'] == 'Omega'
    assert h5.attrs['Start Position'] == -10.0


def test_alignment_missing_tifs_do_truncate(tmp_path):
    '''The `no truncation` claim holds because of the data, not the code.

    Every real alignment scan has one TIFF per row, so `truncated` never
    fires. It is not suppressed, though: an acquisition killed between the
    row write and the TIFF flush takes the same path xRSS does, and keeps
    the full table alongside the trimmed one. Pinned so that nobody later
    reads the spec's wording as a guarantee the code does not make.
    '''
    d = write_alignment(tmp_path/'20250408'/'151027', n_rows=6, n_tifs=4)
    h5 = _build(d, 'alignment')
    assert h5.attrs['truncated'] is True
    assert h5.attrs['rows'] == 6 and h5.attrs['frames'] == 4
    assert h5['measurement_full/position_full'].shape == (6,)
    assert h5['measurement/position'].shape == (4,)
