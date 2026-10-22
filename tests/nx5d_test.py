'''End-to-end tests driving :func:`pxs_import.reader.scans_from_pxs` through
nx5d's ``pm scan import`` CLI and loading the result back with nx5d's
``Proposal``, proving both target layouts of spec Sec 6.1 (one ``.h5`` per
scan; one ``.h5`` per proposal with ``#{scan}`` groups) load with unmodified
nx5d.
'''

import shutil
import subprocess
import sys
from pathlib import Path

import numpy
import pytest

nx5d = pytest.importorskip('nx5d')
pytestmark = pytest.mark.nx5d

from fixtures import write_srsm, write_trss, write_xrss

#: The ``pm`` console script has no ``__main__`` guard, so it cannot be run as
#: ``-m nx5d.pmtool.pm``. Resolve the installed console script instead
#: (``.venv/Scripts/pm.exe`` on Windows), falling back to calling its
#: ``main()`` directly if no script is found on PATH.
_PM = (shutil.which('pm', path=str(Path(sys.executable).parent))
       or shutil.which('pm'))
PM_CMD = [_PM] if _PM else [sys.executable, '-c',
                             'import sys; from nx5d.pmtool.pm import main; '
                             'sys.exit(main() or 0)']


def run_pm_import(source: Path, url: str, *, writer: str | None = None,
                   extra_reader_kw: dict = {}) -> subprocess.CompletedProcess:
    '''Run ``pm scan import`` against ``source`` -> ``url`` via the xraydc
    UDKM reader. Always forces ``frame_guard=None`` on the writer: nx5d's
    writer otherwise looks for the ``k-frame-guard`` attribute on the
    still-empty target and refuses with ``RefuseFrameStorage``. ``-u``
    (and other global options) must precede the ``scan import`` subcommand;
    ``-R``/``-W`` follow it. Reused by Task 9's tests.
    '''
    reader_kw = [f'{k}={v}' for k, v in extra_reader_kw.items()]
    writer_kw = ([writer] if writer else []) + ['frame_guard=None']
    cmd = [*PM_CMD, '-u', url, 'scan', 'import', str(source),
           '-R', 'pxs_import.reader:scans_from_pxs', *reader_kw,
           '-W', *writer_kw]
    return subprocess.run(cmd, capture_output=True, text=True)


def _proposal(root):
    write_trss(root/'20260108'/'120821', n_rows=3, correction=0.0,
               params={'Comments': ['16nm Pt on MgO'],
                       'Delay Vector': '-5:0.5:40',
                       # '#' and spaces in an attribute name: fine in HDF5,
                       # and xRSS files really do log '# of Loops'.
                       '# of Loops': '80'})
    write_srsm(root/'20260108'/'115550', n_rows=2)
    write_xrss(root/'20260108'/'100331', n_rows=5, n_tifs=3)
    return root


def _check(prop):
    assert sorted(prop.ls()['key']) == ['r0001', 'r0002', 'r0003']
    raw = prop.r0003.raw                      # the tRSS scan
    for v in ('loop_delay', 'delay', 'loop_theta', 'theta', 'ttheta', 'crystal_v', 'crystal_calib',
              'cntrate', 'repeats', 'image_number', 'curve', 'images', 'frame_missing'):
        assert v in raw and raw[v].sizes['index'] == 3, v
    numpy.testing.assert_allclose(raw.ttheta.values, 2 * raw.theta.values)
    assert prop.r0003.summary['type'] == 'tRSS' and prop.r0003.summary['num_frames'] == 3
    # Folder + notes land in `description`; nothing else but `integration`
    # reaches the summary, so the listing's `others` column stays short.
    assert prop.r0003.summary['description'] == '20260108/120821 16nm Pt on MgO'
    assert prop.r0003.summary['integration'] == 2.0
    assert 'notes' not in prop.r0003.summary
    # A resolved k-primary:0 -- the error nx5d would otherwise report through
    # `description`, which this importer overwrites.
    assert prop.r0003.summary['scan_dims'] == [['delay']]
    # The parameters*.txt entries survive the pm write and come back on the
    # Dataset, exotic key names included.
    attrs = prop.r0003.raw.attrs
    assert attrs['Delay Vector'] == '-5:0.5:40'      # no number in it: raw
    assert attrs['# of Loops'] == 80                 # typed, and int
    assert list(attrs['ROI']) == [0, 0, 487, 195]    # lists survive HDF5 too
    assert attrs['Exposure Time'] == 2.0
    assert attrs['notes'] == '16nm Pt on MgO'
    x = prop.r0001.raw                        # the truncated xRSS scan
    assert x.counts.sizes['index'] == 3 and x.counts_full.shape == (5,)
    assert prop.r0002.summary['description'] == '20260108/115550'


def test_per_scan_files(tmp_path):
    src = _proposal(tmp_path/'P'); out = tmp_path/'P'/'scans'
    r = run_pm_import(src, f'file:///{out.as_posix()}/{{scan}}.h5')
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in out.glob('*.h5')) == ['r0001.h5', 'r0002.h5', 'r0003.h5']
    _check(nx5d.Proposal(url=f'file:///{out.as_posix()}/{{scan}}.h5'))


def test_single_file_layout_loads(tmp_path):
    '''Both an explicit ``writer='hdf5'`` hint (what Task 9's
    ``prepare.prepare_proposal`` always passes for this layout) and
    plain extension auto-detection load correctly. Auto-detection does not
    actually fail here: nx5d splits the target URL on ``#`` into
    ``target_outer_inner`` *before* substituting ``{scan}``, so it sees a
    clean ``...P.h5`` outer path and resolves ``write_to_h5`` (an alias of
    ``write_to_hdf5``) regardless of the hint.
    '''
    src = _proposal(tmp_path/'P')
    for name, writer in (('hint.h5', 'hdf5'), ('auto.h5', None)):
        h5 = tmp_path/'P'/name
        r = run_pm_import(src, f'file:///{h5.as_posix()}#{{scan}}', writer=writer)
        assert r.returncode == 0, r.stderr
        _check(nx5d.Proposal(url=f'file:///{h5.as_posix()}#{{scan}}'))
