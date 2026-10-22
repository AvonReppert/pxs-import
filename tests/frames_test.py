import logging, numpy, pytest, tifffile
from pxs_import.format import parse_dat
from pxs_import.frames import plan_frames, load_stack
from fixtures import write_srsm, write_trss, write_xrss, write_alignment

def test_unreferenced_tifs_are_ignored(tmp_path, caplog):
    d = write_trss(tmp_path/'20260203'/'120401', n_rows=4, extra_tifs=3)
    plan = plan_frames(d, parse_dat(d/'scans120401.dat', 'tRSS'), 'tRSS')
    assert plan.n_frames == 4 and len(plan.unreferenced) == 3 and not plan.truncated
    assert plan.folder == d

def test_missing_referenced_frames_become_nan(tmp_path, caplog):
    d = write_trss(tmp_path/'20260212'/'180247', n_rows=5, shape=(3, 4), missing=(1, 3))
    plan = plan_frames(d, parse_dat(d/'scans180247.dat', 'tRSS'), 'tRSS')
    with caplog.at_level(logging.WARNING):
        stack, missing = load_stack(plan)
    assert stack.dtype == numpy.float32 and stack.shape == (5, 3, 4)
    assert missing.tolist() == [False, True, False, True, False]
    assert numpy.isnan(stack[1]).all() and stack[2, 0, 0] == 2.0
    assert 'image00001.tif' in caplog.text and 'image00003.tif' in caplog.text

def test_xrss_truncation_uses_contiguous_run(tmp_path):
    d = write_xrss(tmp_path/'20260213'/'125733', n_rows=10, n_tifs=4)
    plan = plan_frames(d, parse_dat(d/'allSteps125733.dat', 'xRSS'), 'xRSS')
    assert plan.truncated and plan.n_frames == 4 and plan.n_rows == 10
    assert load_stack(plan)[0].shape[0] == 4

def test_shape_mismatch_names_the_file(tmp_path):                      # Review Focus 4
    d = write_srsm(tmp_path/'20260819'/'140224', n_rows=3, shape=(3, 4))
    tifffile.imwrite(d/'image00002.tif', numpy.zeros((5, 5), dtype=numpy.int32))
    plan = plan_frames(d, parse_dat(d/'rockingcurves140224.dat', 'sRSM'), 'sRSM')
    with pytest.raises(ValueError, match='image00002.tif'):
        load_stack(plan)

def test_all_missing_names_the_folder(tmp_path):
    d = write_trss(tmp_path/'20260220'/'180247', n_rows=2, missing=(0, 1))
    plan = plan_frames(d, parse_dat(d/'scans180247.dat', 'tRSS'), 'tRSS')
    with pytest.raises(ValueError, match='180247'):
        load_stack(plan)


def test_alignment_frames_planned_by_index(tmp_path):
    folder = write_alignment(tmp_path/'20250408'/'151027', n_rows=4)
    table = parse_dat(folder/'scan151027.dat', 'alignment')
    plan = plan_frames(folder, table, 'alignment')
    assert plan.n_frames == 4
    assert not plan.truncated
    assert [f.name for f in plan.files] == [f'image{i:05d}.tif' for i in range(4)]


def test_alignment_unreferenced_tifs_are_reported_not_loaded(tmp_path, caplog):
    '''Review Focus 2: an aborted run leaves TIFFs past the last .dat row.'''
    folder = write_alignment(tmp_path/'20250408'/'151027', n_rows=4, n_tifs=7)
    table = parse_dat(folder/'scan151027.dat', 'alignment')
    with caplog.at_level(logging.WARNING):
        plan = plan_frames(folder, table, 'alignment')
    assert plan.n_frames == 4
    assert len(plan.unreferenced) == 3
    assert 'unreferenced' in caplog.text
