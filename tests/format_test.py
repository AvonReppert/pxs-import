import logging
from pathlib import Path
import numpy
import pytest
from pxs_import.format import (classify_scan, discover_scans, number_scans,
                               parse_parameters, exposure_seconds,
                               two_theta_correction, parse_date_time,
                               parse_dat, numeric_value,
                               parse_scan_temperatures)
from fixtures import (write_srsm, write_trss, write_xrss, write_alignment,
                      write_scan_temperatures)

def _mk(root: Path, date: str, time: str, signature: str | None, extra=()):
    d = root / date / time; d.mkdir(parents=True)
    if signature: (d / signature.format(sfx=time)).write_text('% x\n1\n')
    for e in extra: (d / e.format(sfx=time)).write_text('')
    return d

def test_classify_each_type(tmp_path):
    assert classify_scan(_mk(tmp_path, '20260108', '115550', 'rockingcurves{sfx}.dat',
                             extra=['intensities{sfx}.dat'])) == 'sRSM'
    assert classify_scan(_mk(tmp_path, '20260108', '120821', 'scans{sfx}.dat')) == 'tRSS'
    assert classify_scan(_mk(tmp_path, '20260108', '100331', 'allSteps{sfx}.dat',
                             extra=['averages{sfx}.dat'])) == 'xRSS'
    assert classify_scan(_mk(tmp_path, '20260108', '101010', None)) is None

def test_discover_skips_debris_and_sorts_across_dates(tmp_path, caplog):
    _mk(tmp_path, '20260203', '120401', 'scans{sfx}.dat')
    _mk(tmp_path, '20260202', '153026', 'rockingcurves{sfx}.dat')
    _mk(tmp_path, '20260202', '135936', 'allSteps{sfx}.dat')
    (tmp_path / '20260202' / '135936Graphics').mkdir()
    (tmp_path / '20260202' / '153026.zip').write_bytes(b'PK')
    (tmp_path / 'spice').mkdir()
    _mk(tmp_path, '20260202', '140000', None)             # Review Focus 1
    with caplog.at_level(logging.WARNING):
        scans = discover_scans(tmp_path)
    assert [s.source for s in scans] == ['20260202/135936', '20260202/153026', '20260203/120401']
    assert [s.scan_type for s in scans] == ['xRSS', 'sRSM', 'tRSS']
    assert '20260202/140000' in caplog.text

def test_number_scans_is_zero_padded_and_ordered(tmp_path):
    from pxs_import.format import ScanFolder
    scans = [ScanFolder(tmp_path, '20260108', t, 'sRSM') for t in ('100331', '115550')]
    assert [k for k, _ in number_scans(scans)] == ['r0001', 'r0002']
    assert number_scans(scans)[1][1].time == '115550'


TRSS_PARAMS = ('16nm Pt on MgO\r\n157mW\r\n930x880µm²\r\n10.2mJ/cm²\r\n\r\n \r\n\r\n'
               'Date Time: 08.01.2026 12:08:21\r\n\r\nTheta Vector: 19.48\r\n'
               '2 Theta Correction: 0.000000\r\nExposure Time: 2.000000s\r\n'
               'Tmp Path: P:\\Pilatus\\2026\\20260108\\120821\r\nROI: (0, 0); (487, 195)\r\n')

def test_parse_parameters_windows1252_comments_and_keys(tmp_path):
    p = tmp_path / 'parameters120821.txt'
    p.write_bytes(TRSS_PARAMS.encode('windows-1252'))
    d = parse_parameters(p)
    assert d['Comments'] == '16nm Pt on MgO 157mW 930x880µm² 10.2mJ/cm²'
    assert d['Tmp Path'] == 'P:\\Pilatus\\2026\\20260108\\120821'
    assert d['ROI'] == '(0, 0); (487, 195)'
    assert two_theta_correction(d) == 0.0
    assert exposure_seconds(d['Exposure Time']) == 2.0
    assert parse_date_time(d['Date Time']) == '2026-01-08T12:08:21'

def test_parse_parameters_handles_second_colon_space(tmp_path):
    p = tmp_path / 'parameters000000.txt'
    p.write_text('Date Time: 08.01.2026 12:08:21\nNote: a: b\n')
    assert parse_parameters(p)['Note'] == 'a: b'

def test_parse_parameters_missing_file_is_empty_with_warning(tmp_path, caplog):   # Review Focus 3
    with caplog.at_level(logging.WARNING):
        assert parse_parameters(tmp_path / 'parameters999999.txt') == {}
    assert 'parameters999999.txt' in caplog.text

def test_exposure_seconds_bare_number():
    assert exposure_seconds('1.000000') == 1.0


def test_parse_srsm_table(tmp_path):
    d = write_srsm(tmp_path / '20260108' / '115550', n_rows=3, width=6, images=False)
    t = parse_dat(d / 'rockingcurves115550.dat', 'sRSM')
    assert set(t.columns) == {'Loop', 'Theta / deg.', '2Theta + Corr / deg.', 'Crystal', 'Crystal Calib'}
    assert t.profile.shape == (3, 6) and t.n_rows == 3
    assert t.image_names == ['image00000.tif', 'image00001.tif', 'image00002.tif']

def test_parse_trss_table(tmp_path):
    d = write_trss(tmp_path / '20260108' / '120821', n_rows=2, width=6, images=False)
    t = parse_dat(d / 'scans120821.dat', 'tRSS')
    assert len(t.columns) == 9 and 'Intensities [ph/s]' not in t.columns
    assert t.profile.shape == (2, 6) and len(t.start_times) == 2

def test_parse_xrss_table_keeps_nan(tmp_path):
    d = write_xrss(tmp_path / '20260108' / '100331', n_rows=2)
    p = d / 'allSteps100331.dat'
    p.write_text(p.read_text().replace('10.078973', 'NaN', 1))
    t = parse_dat(p, 'xRSS')
    assert t.profile is None and t.image_names is None and len(t.columns) == 7
    assert numpy.isnan(t.columns['Position Real']).sum() == 1

def test_parse_dat_drops_short_trailing_row(tmp_path, caplog):           # Review Focus 2
    d = write_xrss(tmp_path / '20260108' / '100331', n_rows=3)
    p = d / 'allSteps100331.dat'
    p.write_text(p.read_text().rstrip('\n').rsplit('\t', 3)[0] + '\n')
    with caplog.at_level(logging.WARNING):
        t = parse_dat(p, 'xRSS')
    assert t.n_rows == 2 and 'row 3' in caplog.text

def test_parse_dat_handles_windows1252_header(tmp_path):                 # F1
    d = write_srsm(tmp_path / '20260108' / '115550', n_rows=2, images=False)
    p = d / 'rockingcurves115550.dat'
    raw = p.read_bytes().replace(b'Theta / deg.', 'Theta / °'.encode('windows-1252'))
    p.write_bytes(raw)
    t = parse_dat(p, 'sRSM')
    assert 'Theta / °' in t.columns

def test_parse_dat_drops_poisoned_first_row_not_whole_file(tmp_path, caplog):   # Review Focus 5
    d = write_srsm(tmp_path / '20260108' / '140224', n_rows=3, width=6, images=False)
    p = d / 'rockingcurves140224.dat'
    lines = p.read_text().split('\n')          # read_text() normalizes \r\n to \n
    first_data_idx = next(i for i, line in enumerate(lines)
                           if i >= 2 and line and not line.startswith('%'))
    fields = lines[first_data_idx].split('\t')
    lines[first_data_idx] = '\t'.join(fields[:4])      # truncate the *first* data row short
    p.write_text('\r\n'.join(lines), encoding='utf-8', newline='')
    with caplog.at_level(logging.WARNING):
        t = parse_dat(p, 'sRSM')
    assert t.n_rows == 2
    assert t.profile.shape[1] == 6
    assert 'row 1' in caplog.text


@pytest.mark.parametrize('value,expected', [
    ('10.000000', 10.0),                    # xRSS writes positions bare ...
    ('17.000000°', 17.0),                   # ... sRSM with a degree sign
    ('0.050000°', 0.05),
    ('2.000000s', 2.0),                     # Exposure Time, suffix optional
    ('1.000000', 1.0),
    ('-1.700000', -1.7),
    ('19.48', 19.48),                       # Theta Vector, when it is scalar
    ('80', 80), ('2', 2), ('0', 0),         # counts stay int, not float
    ('(242, 50); (254, 87)', [242, 50, 254, 87]),
    ('0.000000	0.000000	6828690.000000	-143103.000000',
     [0.0, 0.0, 6828690.0, -143103.0]),
    # No unambiguous number in any of these:
    ('-10:0.5:40, 41:1:200', None),         # a delay vector, not a scalar
    ('Triangle', None), ('ps', None), ('PXS 600mm', None),
    ('08.01.2026 10:03:31', None),
    ('085055', None),                       # leading zero: a scan stem
    ('00', None),
    ('', None), ('   ', None),
])
def test_numeric_value(value, expected):
    assert numeric_value(value) == expected


def test_numeric_value_keeps_int_and_float_apart():
    assert isinstance(numeric_value('80'), int)
    assert isinstance(numeric_value('80.0'), float)
    assert all(isinstance(x, int) for x in numeric_value('(0, 1); (486, 194)'))


def test_alignment_folder_classifies(tmp_path):
    folder = write_alignment(tmp_path / '20250408' / '151027', n_rows=4)
    assert classify_scan(folder) == 'alignment'


def test_scan_and_scans_do_not_collide(tmp_path):
    '''`scan<sfx>.dat` is alignment, `scans<sfx>.dat` is tRSS. One character.'''
    alignment = write_alignment(tmp_path / '20250408' / '151027', n_rows=4)
    trss = write_trss(tmp_path / '20250408' / '151028', n_rows=4)
    assert classify_scan(alignment) == 'alignment'
    assert classify_scan(trss) == 'tRSS'
    assert not (alignment / 'scans151027.dat').exists()
    assert not (trss / 'scan151028.dat').exists()


def test_discover_scans_finds_alignment(tmp_path):
    write_alignment(tmp_path / '20250408' / '151027', n_rows=4)
    scans = discover_scans(tmp_path)
    assert [s.scan_type for s in scans] == ['alignment']


def test_alignment_columns_are_not_shifted(tmp_path):
    '''The header names four columns; the rows have five. Values, not shapes.'''
    folder = write_alignment(tmp_path / '20250408' / '151027', n_rows=3)
    table = parse_dat(folder / 'scan151027.dat', 'alignment')
    assert list(table.columns) == ['Loop', 'Position', 'Pilatus', 'Crystal', 'Norm']
    assert table.columns['Loop'].tolist() == [1.0, 1.0, 1.0]
    assert table.columns['Position'].tolist() == [-1.0, -0.9, -0.8]
    assert table.profile is None
    assert table.image_names is None


def test_alignment_short_final_row_is_dropped(tmp_path):
    '''Review Focus 1: a killed acquisition leaves a half-written last row.'''
    folder = write_alignment(tmp_path / '20250408' / '151027', n_rows=3)
    dat = folder / 'scan151027.dat'
    dat.write_text(dat.read_text() + '2.000000\t0.500000\r\n', newline='')
    table = parse_dat(dat, 'alignment')
    assert table.n_rows == 3


def test_alignment_header_only_dat_is_rejected(tmp_path):
    '''Review Focus 5: stopped before the first exposure.'''
    folder = write_alignment(tmp_path / '20250408' / '151027', n_rows=0)
    with pytest.raises(ValueError, match='151027'):
        parse_dat(folder / 'scan151027.dat', 'alignment')


def test_discover_scans_descends_one_year_level(tmp_path):
    write_alignment(tmp_path / '2026' / '20260108' / '151027', n_rows=2)
    write_alignment(tmp_path / '2026' / '20260109' / '090000', n_rows=2)
    assert [s.source for s in discover_scans(tmp_path)] == [
        '20260108/151027', '20260109/090000']


def test_discover_scans_year_folder_may_disagree_with_the_dates_in_it(tmp_path):
    '''Review Focus 2: PilatusPumpProbe\\2025\\20260108 is real.'''
    write_alignment(tmp_path / '2025' / '20260108' / '151027', n_rows=2)
    assert [s.source for s in discover_scans(tmp_path)] == ['20260108/151027']


def test_discover_scans_mixes_year_level_and_bare_date_dirs(tmp_path):
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)
    write_alignment(tmp_path / '2026' / '20260108' / '090000', n_rows=2)
    assert len(discover_scans(tmp_path)) == 2


def test_discover_scans_sorts_days_across_year_folders_by_date(tmp_path):
    '''A year folder's name need not agree with the dates inside it.'''
    write_alignment(tmp_path / '2025' / '20260108' / '151027', n_rows=2)
    write_alignment(tmp_path / '2026' / '20260107' / '090000', n_rows=2)
    assert [s.source for s in discover_scans(tmp_path)] == [
        '20260107/090000', '20260108/151027']


def test_discover_scans_does_not_descend_two_year_levels(tmp_path):
    write_alignment(tmp_path / '2026' / '2025' / '20260108' / '151027', n_rows=2)
    assert discover_scans(tmp_path) == []


def test_discover_scans_filters_by_inclusive_date_range(tmp_path):
    for day in ('20250407', '20250408', '20250409', '20250410'):
        write_alignment(tmp_path / day / '151027', n_rows=2)
    got = discover_scans(tmp_path, start='20250408', end='20250409')
    assert [s.date for s in got] == ['20250408', '20250409']


def test_discover_scans_open_ended_range(tmp_path):
    for day in ('20250407', '20250408', '20250409'):
        write_alignment(tmp_path / day / '151027', n_rows=2)
    assert len(discover_scans(tmp_path, start='20250408')) == 2
    assert len(discover_scans(tmp_path, end='20250408')) == 2


def test_discover_scans_never_opens_a_day_outside_the_range(tmp_path):
    '''The cost property, pinned on access rather than on the result.'''
    for day in ('20250407', '20250408', '20250409'):
        write_alignment(tmp_path / day / '151027', n_rows=2)
    seen = []

    def counting(path):
        seen.append(path)
        return path.iterdir()

    discover_scans(tmp_path, start='20250408', end='20250408', lister=counting)
    assert tmp_path / '20250407' not in seen
    assert tmp_path / '20250409' not in seen
    assert tmp_path / '20250408' in seen


def test_scan_temperatures_reads_the_last_row(tmp_path):
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    write_scan_temperatures(folder, rows=[(12.15, 15.25), (108.00, 108.77)])
    assert parse_scan_temperatures(folder / 'scan data000051.txt') == (
        108.00, 108.77, '20151226', '000051')


def test_scan_temperatures_splits_rows_on_whitespace_not_tabs(tmp_path):
    '''The header is tab-separated; the rows are not. split('\\t') gives 2.'''
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    path = write_scan_temperatures(folder)
    assert path.read_text(encoding='windows-1252').splitlines()[0].count('\t') == 3
    assert parse_scan_temperatures(path)[:2] == (108.00, 108.77)


def test_scan_temperatures_last_row_has_no_trailing_tab(tmp_path):
    '''Earlier rows of a real cryostat log carry a trailing tab; its last
    (the scan's own) row does not.'''
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    path = write_scan_temperatures(folder)
    rows = [line for line in path.read_text(encoding='windows-1252').splitlines()
            if line and not line.startswith('%')]
    assert rows[0].endswith('\t')
    assert not rows[-1].endswith('\t')


def test_scan_temperatures_absent_file_is_none(tmp_path):
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    assert parse_scan_temperatures(folder / 'scan data000051.txt') is None


def test_scan_temperatures_non_numeric_row_is_none(tmp_path, caplog):
    '''An optional enrichment must never abort a whole proposal import.'''
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    path = write_scan_temperatures(folder)
    path.write_text(path.read_text(encoding='windows-1252').replace(
        '108.00 108.77', 'OVER ----'), encoding='windows-1252', newline='')
    with caplog.at_level(logging.WARNING):
        assert parse_scan_temperatures(path) is None
    assert 'scan data000051.txt' in caplog.text


def test_scan_temperatures_short_row_is_none(tmp_path, caplog):
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    path = write_scan_temperatures(folder)
    path.write_text(path.read_text(encoding='windows-1252').replace(
        '108.00 108.77 20151226 000051', '108.00 108.77'),
        encoding='windows-1252', newline='')
    with caplog.at_level(logging.WARNING):
        assert parse_scan_temperatures(path) is None
    assert 'scan data000051.txt' in caplog.text


def test_scan_temperatures_header_only_warns(tmp_path, caplog):
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    path = write_scan_temperatures(folder, rows=[])
    with caplog.at_level(logging.WARNING):
        assert parse_scan_temperatures(path) is None
    assert 'scan data000051.txt' in caplog.text


def test_scan_temperatures_header_only_is_none(tmp_path):
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    path = write_scan_temperatures(folder, rows=[])
    assert parse_scan_temperatures(path) is None


def test_scan_folder_knows_its_temperature_path(tmp_path):
    folder = write_srsm(tmp_path / '20151226' / '000051', n_rows=2)
    sf = discover_scans(tmp_path)[0]
    assert sf.temperature_path.name == 'scan data000051.txt'
    assert sf.temperature_path.parent == folder


def test_discover_scans_names_an_unreadable_directory(tmp_path):
    '''Review Focus 5: a share that drops mid-walk.'''
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)

    def failing(path):
        if path.name == '20250408':
            raise PermissionError(13, 'denied')
        return path.iterdir()

    with pytest.raises(OSError, match='20250408'):
        discover_scans(tmp_path, lister=failing)


def test_discover_scans_raises_rather_than_swallows_a_dropped_share(tmp_path, monkeypatch):
    '''``is_dir()`` treats Windows' ERROR_NOT_READY -- what a dropped network
    share raises -- as "doesn't exist" and swallows it, which would otherwise
    turn a real I/O failure into a silent "unclassifiable scan folder".'''
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)
    real_stat = Path.stat

    def failing_stat(self, *a, **kw):
        if self.name == '151027':
            raise OSError(21, 'The device is not ready')
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, 'stat', failing_stat)
    with pytest.raises(OSError, match='151027'):
        discover_scans(tmp_path)


def test_discover_scans_resolves_the_stamped_root(tmp_path, monkeypatch):
    '''Two spellings of one directory must compare equal through ``root``.'''
    write_alignment(tmp_path / '20250408' / '151027', n_rows=2)
    monkeypatch.chdir(tmp_path)
    scans = discover_scans(Path('.'))
    assert scans[0].root == tmp_path.resolve()
    assert scans[0].root.is_absolute()


def test_discover_scans_dedupes_the_same_day_under_two_year_folders(tmp_path, caplog):
    '''A year directory's name need not agree with the dates filed under it,
    so the same day can legitimately turn up under two of them at once.'''
    write_alignment(tmp_path / '2025' / '20260108' / '151027', n_rows=2)
    write_alignment(tmp_path / '2026' / '20260108' / '151027', n_rows=2)
    with caplog.at_level(logging.WARNING):
        scans = discover_scans(tmp_path)
    assert [s.source for s in scans] == ['20260108/151027']
    assert str(tmp_path / '2025' / '20260108') in caplog.text
    assert str(tmp_path / '2026' / '20260108') in caplog.text
