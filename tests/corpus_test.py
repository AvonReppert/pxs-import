'''Corpus regression tests against the real UDKM proposals.

Every assertion here is measured off real measurement data, not invented:
parameter vocabularies, frame-plan counts, date-range totals and the
cryostat log's own stamps. The data they read is the thinned corpus under
``tests/corpus/``, which ships with the repository -- so unlike in xraydc,
where it was gitignored and deselected, the ``corpus`` marker is now a label
rather than a gate and these run by default. ``tools/thin_corpus.py`` says
exactly what was carried out of the real proposals and what was replaced.
'''

import pytest, numpy, shutil
from pathlib import Path
from pxs_import.format import (discover_scans, number_scans, parse_dat,
                               parse_parameters, numeric_value,
                               parse_scan_temperatures)
from pxs_import.reader import IDENTIFIER_KEYS, build_scan_h5like
from pxs_import.frames import plan_frames, load_stack
from pxs_import.spice import load_records, migrate_spice

pytestmark = pytest.mark.corpus

NB = Path(__file__).resolve().parent / 'corpus'
needs = pytest.mark.skipif(not (NB/'202602_FeAu'/'20260203').exists(), reason='corpus not present')


@needs
@pytest.mark.parametrize('proposal,n,types', [('202601_PtAu', 15, (5, 6, 4)),
                                              ('202602_FeAu', 38, (18, 11, 9)),
                                              ('202608_DyCo5Pt', 18, (16, 2, 0))])
def test_all_scans_classify(proposal, n, types):
    scans = discover_scans(NB/proposal)
    assert len(scans) == n
    assert tuple(sum(s.scan_type == t for s in scans) for t in ('sRSM', 'tRSS', 'xRSS')) == types


@needs
@pytest.mark.parametrize('scan_type,expected', [
    ('sRSM', {'Date Time', 'Start Position', 'End Position', '2 Theta Correction',
              'Step Width', 'Number of Loops', 'Exposure Time', 'File Name',
              'Tmp Path', 'Data Path', 'ROI', 'Calib'}),
    ('tRSS', {'Date Time', 'Delay Vector', 'Delay Loop', 'Delay Order', 'Theta Vector',
              'Theta Loop', 'Theta Order', '2 Theta Correction', 'Inner Parameter',
              'Exposure Time', 'Calibration', 'File Name', 'Tmp Path', 'Data Path', 'ROI'}),
    ('xRSS', {'Date Time', 'Selected Stage', 'Stage Unit', 'Start Position',
              'End Position', 'Step Width', '# of Loops', 'Velocity', 'Acceleration',
              'Exposure Time', 'ROI', 'Pump Region', 'Norm Region', 'Filename',
              'Tmp Path', 'Data Path', 'Calibration'}),
])
def test_parameter_vocabulary_is_fixed_per_scan_type(scan_type, expected):
    """Every real parameters*.txt of one type logs exactly the same keys.

    71 files across the three proposals. The importer does not rely on this
    (it writes whatever it finds), but a change here means the acquisition
    software changed, which is worth knowing. Note the cross-type drift the
    sets encode: `File Name` vs `Filename`, `Calib` vs `Calibration`,
    `Number of Loops` vs `# of Loops` vs `Delay Loop`/`Theta Loop`.
    """
    seen = 0
    for proposal in ('202601_PtAu', '202602_FeAu', '202608_DyCo5Pt'):
        for sf in discover_scans(NB/proposal):
            if sf.scan_type != scan_type:
                continue
            params = parse_parameters(sf.params_path)
            assert set(params) - {'Comments'} == expected, sf.source
            seen += 1
    assert seen > 0


@needs
def test_numeric_companions_are_all_or_nothing_per_key():
    """Which keys are written as numbers, over all 71 real files.

    Every key is numeric for all of its scans or for none -- no key is
    numeric only sometimes, so analysis code can rely on the companion
    being there for the keys below. Types are consistent per key too.
    """
    numeric, string_only = {}, set()
    for proposal in ('202601_PtAu', '202602_FeAu', '202608_DyCo5Pt'):
        for sf in discover_scans(NB/proposal):
            for key, value in parse_parameters(sf.params_path).items():
                if key == 'Comments':
                    continue
                parsed = None if key in IDENTIFIER_KEYS else numeric_value(value)
                (string_only.add(key) if parsed is None
                 else numeric.setdefault(key, []).append(parsed))

    assert not (set(numeric) & string_only), 'key numeric for some scans only'
    assert set(numeric) == {
        '# of Loops', '2 Theta Correction', 'Acceleration', 'Calib', 'Calibration',
        'Delay Loop', 'End Position', 'Exposure Time', 'Norm Region',
        'Number of Loops', 'Pump Region', 'ROI', 'Start Position', 'Step Width',
        'Theta Loop', 'Theta Vector', 'Velocity'}
    assert string_only == {
        'Data Path', 'Date Time', 'Delay Order', 'Delay Vector', 'File Name',
        'Filename', 'Inner Parameter', 'Selected Stage', 'Stage Unit',
        'Theta Order', 'Tmp Path'}
    for key, parsed in numeric.items():
        assert len({type(v).__name__ for v in parsed}) == 1, key


@needs
def test_120401_excludes_unreferenced_frames():
    d = NB/'202602_FeAu'/'20260203'/'120401'
    plan = plan_frames(d, parse_dat(d/'scans120401.dat', 'tRSS'), 'tRSS')
    assert plan.n_frames == 1364 and len(plan.unreferenced) == 751


@needs
def test_180247_nan_fills_seven_gaps():
    d = NB/'202602_FeAu'/'20260212'/'180247'
    plan = plan_frames(d, parse_dat(d/'scans180247.dat', 'tRSS'), 'tRSS')
    stack, missing = load_stack(plan)
    assert stack.shape[0] == 172 and missing.sum() == 7 and stack.dtype == numpy.float32


@needs
def test_125733_truncates_to_489():
    d = NB/'202602_FeAu'/'20260213'/'125733'
    plan = plan_frames(d, parse_dat(d/'allSteps125733.dat', 'xRSS'), 'xRSS')
    assert plan.truncated and plan.n_frames == 489 and plan.n_rows == 1729


@needs
def test_dyco5pt_spice_migration_is_noop(tmp_path):
    spice = tmp_path/'spice'; shutil.copytree(_original_spice('202608_DyCo5Pt'), spice)
    mapping = {sf.source: k for k, sf in number_scans(discover_scans(NB/'202608_DyCo5Pt'))}
    assert migrate_spice(spice, mapping) == [] and not (tmp_path/'spice.pre-migration').exists()


def _original_spice(proposal):
    """The proposal's spice as it was before any import.

    `pxs-prepare` migrates `spice/` in place and keeps the original in
    `spice.pre-migration/`, which `_ensure_backup` never overwrites. So once a
    proposal has been imported, `spice/` holds run-number anchors and only the
    backup still holds the date/time ones these tests migrate from.
    """
    backup = NB/proposal/'spice.pre-migration'
    return backup if backup.is_dir() else NB/proposal/'spice'


@needs
def test_ptau_spice_migration_rewrites_eleven(tmp_path):
    spice = tmp_path/'spice'; shutil.copytree(_original_spice('202601_PtAu'), spice)
    mapping = {sf.source: k for k, sf in number_scans(discover_scans(NB/'202601_PtAu'))}
    changes = migrate_spice(spice, mapping)
    assert len(changes) == 11
    assert {c.new_anchor for c in changes if c.old_anchor == '20260108/143930'} == {mapping['20260108/143930']}


@needs
@pytest.mark.slow
def test_ptau_end_to_end_cooks_srsm_and_refuses_xrss(tmp_path):
    nx5d = pytest.importorskip('nx5d')
    # nx5d resolves `pymod:///xraydc.cooking#sRSM` at cook time, so a missing
    # xraydc would surface here as a cook failure rather than as a skip.
    pytest.importorskip('xraydc')
    from pxs_import.prepare import prepare_proposal
    spice = tmp_path/'spice'; shutil.copytree(NB/'202601_PtAu'/'spice', spice)   # never touch the real spice
    res = prepare_proposal(NB/'202601_PtAu', output_root=tmp_path, spice_dir=spice)  # reads TIFFs in place
    prop = nx5d.Proposal(url=res.target_url, spice=f'file:///{spice.as_posix()}')
    srsm = next(k for k, sf in res.mapping if sf.scan_type == 'sRSM')
    cooked = getattr(prop, srsm).cooked
    assert {'qx', 'qy', 'qz'} <= set(cooked.dims) | set(cooked.coords)
    xrss = next(k for k, sf in res.mapping if sf.scan_type == 'xRSS')
    with pytest.raises(RuntimeError):
        getattr(prop, xrss).cooked
    scan = getattr(prop, next(k for k, sf in res.mapping if sf.source == '20260108/120821'))
    notes = '16nm Pt on MgO 157mW 930x880µm² 10.2mJ/cm²'
    # The notes reach the listing only through `description`, folder-prefixed.
    assert scan.summary['description'] == '20260108/120821 ' + notes
    assert 'notes' not in scan.summary and scan.raw.attrs['notes'] == notes
    # A real tRSS-only parameter key, carried verbatim off the real file.
    # Off a real file, through the real pm writer: strings where there is
    # no number, parsed values where there is one.
    assert scan.raw.attrs['Inner Parameter'] == 'Theta'
    assert scan.raw.attrs['Delay Vector'] == '-5:0.5:40'
    assert scan.raw.attrs['Theta Vector'] == 19.48
    assert list(scan.raw.attrs['ROI']) == [0, 0, 487, 195]
    assert scan.raw.attrs['File Name'] == '120821'


@needs
@pytest.mark.parametrize('start,end,n', [('20250408', '20250408', 27),
                                         ('20250409', '20250410', 16),
                                         ('20250424', None, 20),
                                         (None, '20250409', 40)])
def test_date_range_over_nbo2(start, end, n):
    '''A real proposal holding all four acquisition programs' output.

    Counts measured with discover_scans, not guessed: the day totals are
    27/13/3/15/5 over 20250408..20250425, 63 scans in all.
    '''
    got = discover_scans(NB/'202504_NbO2', start=start, end=end)
    assert len(got) == n
    assert all((start or '0') <= s.date <= (end or '9') for s in got)


@needs
def test_date_range_opens_no_day_outside_it():
    '''The cost property, on real data: an out-of-range day is never read.'''
    seen = []

    def counting(path):
        seen.append(path.name)
        return path.iterdir()

    discover_scans(NB/'202504_NbO2', start='20250410', end='20250410', lister=counting)
    assert '20250410' in seen
    assert not {'20250408', '20250409', '20250424', '20250425'} & set(seen)


needs_cryo = pytest.mark.skipif(not (NB/'20151226').exists(),
                                 reason='cryostat corpus not present')


@needs_cryo
def test_cryostat_corpus_last_row_belongs_to_its_own_scan():
    '''Six real sRSM scans from one cooldown; the log grows by a row each time.'''
    expected = {'000051': (108.00, 108.77), '003154': (104.98, 105.75),
                '010312': (101.98, 102.75), '013435': (99.01, 99.79),
                '020602': (96.03, 96.79), '023739': (93.00, 93.81)}
    scans = discover_scans(NB/'20151226')
    assert len(scans) == 6
    for sf in scans:
        assert sf.scan_type == 'sRSM', sf.source
        ta, tb, day, time = parse_scan_temperatures(sf.temperature_path)
        assert (day, time) == (sf.date, sf.time), sf.source
        assert (round(ta, 2), round(tb, 2)) == expected[sf.time], sf.source


@needs_cryo
def test_cryostat_corpus_temperatures_reach_the_scan():
    '''End to end on real files, including the space-vs-tab row separator.'''
    sf = next(s for s in discover_scans(NB/'20151226') if s.time == '000051')
    table = parse_dat(sf.dat_path, sf.scan_type)
    plan = plan_frames(sf.path, table, sf.scan_type)
    stack, missing = load_stack(plan)
    h5 = build_scan_h5like(sf, table, parse_parameters(sf.params_path),
                           plan, stack, missing)
    assert h5.attrs['k-header:tsample'] == 108.77
    numpy.testing.assert_allclose(h5['measurement/tsample_readback'][()], 108.77)
    numpy.testing.assert_allclose(h5['measurement/tcryo_readback'][()], 108.00)


needs_alignment = pytest.mark.skipif(not (NB/'20250408').exists(),
                                     reason='alignment corpus not present')


@needs_alignment
@pytest.mark.parametrize('date_dir,n', [('20250408', 15), ('20250424', 4)])
def test_alignment_corpus_classifies_and_parses(date_dir, n):
    scans = discover_scans(NB / date_dir)
    assert len(scans) == n
    assert {s.scan_type for s in scans} == {'alignment'}
    for folder in scans:
        table = parse_dat(folder.dat_path, 'alignment')
        assert list(table.columns)[:2] == ['Loop', 'Position']


@needs_alignment
def test_alignment_corpus_stage_vocabulary():
    '''Both acquisition programs, and the motors each of them writes.'''
    seen = {}
    for date_dir in ('20250408', '20250424'):
        seen[date_dir] = {(parse_parameters(f.params_path)['Stage'],
                           parse_parameters(f.params_path)['Unit'])
                          for f in discover_scans(NB / date_dir)}
    assert seen['20250408'] == {('X Linear', 'mm'), ('Y Linear', 'mm'),
                                ('Z Linear', 'mm'), ('Omega', 'deg')}
    assert seen['20250424'] == {('Horizontal', 'steps'), ('Vertical', 'steps')}


@needs_alignment
def test_alignment_corpus_rows_match_frames():
    '''Every alignment scan stops early, and never mid-frame.'''
    for date_dir in ('20250408', '20250424'):
        for folder in discover_scans(NB / date_dir):
            table = parse_dat(folder.dat_path, 'alignment')
            n_tifs = len(list(folder.path.glob('image*.tif')))
            assert table.n_rows == n_tifs, folder.source


@needs_alignment
@pytest.mark.parametrize('date_dir,n', [('20250408', 15), ('20250424', 4)])
def test_alignment_corpus_end_to_end(date_dir, n):
    '''The spec's actual end state: import like any other scan.

    The other corpus tests stop at parse_dat; this one runs the reader nx5d
    calls, on real files, and checks what a listing and a notebook would see.
    '''
    from pxs_import.reader import scans_from_pxs
    built = list(scans_from_pxs(NB / date_dir))
    assert len(built) == n
    for key, h5 in built:
        where = key['udkm_source']
        assert key['scan_type'] == 'alignment', where
        assert h5.attrs['k-primary:0'] == 'measurement/position', where
        assert h5.attrs['k-header:stage'], where
        assert h5.attrs['k-header:unit'], where
        assert 'measurement_full' not in h5, where
        assert 'truncated' not in h5.attrs, where
        n_rows = h5['measurement/position'].shape[0]
        assert h5['instrument/pilatus/images'].shape[0] == n_rows, where
