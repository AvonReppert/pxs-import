import csv
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from pxs_import.reader import read_mapping_table, renumbered
from pxs_import.format import number_scans
from pxs_import.sources import discover_all
from pxs_import.prepare import (target_url_for, spice_url_for, existing_output,
                                prepare_proposal, s3_login, main, _file_url)
from pxs_import.s3 import (manifest_url, read_manifest, write_manifest,
                           credentials_available, write_profile)
from fixtures import write_srsm, write_trss, write_alignment


def _resolved_of(command):
    '''The decoded mapping a recorded pm command carries.

    The single-call path writes it to a file (a whole proposal does not fit
    in one argv element); the per-scan S3 path passes its one entry inline.
    '''
    for arg in command:
        if arg.startswith('resolved_file='):
            return json.loads(Path(arg[len('resolved_file='):]).read_text(encoding='utf-8'))
        if arg.startswith('resolved='):
            return json.loads(arg[len('resolved='):])
    raise AssertionError(f'no resolved mapping in {command}')


def _proposal(root):
    write_srsm(root/'20260108'/'115550', n_rows=2); write_trss(root/'20260203'/'120401', n_rows=2)
    return root


def test_target_urls(tmp_path):
    root = tmp_path/'202601_PtAu'
    assert target_url_for(root, single_file=False).endswith('/202601_PtAu/scans/{scan}.h5')
    assert target_url_for(root, single_file=True).endswith('/202601_PtAu/202601_PtAu.h5#{scan}')
    assert target_url_for(root, single_file=False).startswith('file:///')
    assert target_url_for(root, single_file=True, output_root=tmp_path/'out').endswith('/out/202601_PtAu.h5#{scan}')


def test_prepare_builds_command_and_mapping(tmp_path):
    root = _proposal(tmp_path/'P'); calls = []
    res = prepare_proposal(root, runner=lambda cmd, **kw: calls.append(cmd), pm=['pm'])
    # '-u' and '-P' are global pm options and must precede the 'scan' subcommand.
    assert calls[0].index('-u') < calls[0].index('scan')
    assert calls[0].index('-P') < calls[0].index('scan')
    # '-W frame_guard=None' is always present; the 'hdf5' writer hint is single-file only.
    assert 'hdf5' not in calls[0] and 'frame_guard=None' in calls[0]
    assert 'photon_energy=8048.0' in calls[0] and calls[0][calls[0].index('-u')+1] == res.target_url
    assert [k for k, _ in res.mapping] == ['r0001', 'r0002'] and (root/'scan-mapping.csv').exists()


def test_prepare_single_file_adds_writer_hint(tmp_path):
    root = _proposal(tmp_path/'P'); calls = []
    prepare_proposal(root, single_file=True, runner=lambda cmd, **kw: calls.append(cmd))
    assert calls[0][calls[0].index('-W')+1] == 'hdf5' and '#{scan}' in calls[0][calls[0].index('-u')+1]
    # '-W frame_guard=None' is mandatory in every mode, not just the default.
    assert 'frame_guard=None' in calls[0]


def test_prepare_refuses_existing_output_unless_forced(tmp_path):
    root = _proposal(tmp_path/'P'); (root/'scans').mkdir(); (root/'scans'/'r0001.h5').write_bytes(b'')
    with pytest.raises(FileExistsError):
        prepare_proposal(root, runner=lambda cmd, **kw: None)
    prepare_proposal(root, force=True, runner=lambda cmd, **kw: None)
    assert existing_output(root, single_file=False) is None


def test_main_dry_run_exit_code(tmp_path, monkeypatch):
    root = _proposal(tmp_path/'P')
    monkeypatch.setattr('pxs_import.prepare.subprocess.run', lambda cmd, **kw: None)
    assert main([str(root), '--dry-run', '--photon-energy', '8041']) == 0


def test_dry_run_force_never_removes_existing_output(tmp_path):
    '''A forced dry run must never touch the previous import output -- only
    log what it would remove -- and still passes -d through to pm.'''
    root = _proposal(tmp_path/'P'); (root/'scans').mkdir()
    (root/'scans'/'r0001.h5').write_bytes(b'previous import')
    res = prepare_proposal(root, dry_run=True, force=True,
                            runner=lambda cmd, **kw: None)
    assert (root/'scans'/'r0001.h5').read_bytes() == b'previous import'
    assert '-d' in res.command[0]


def test_target_url_for_relative_path_resolves_against_cwd():
    '''A relative proposal_root must not be taken literally into the URL
    (which would anchor it at the filesystem root, or -- for '.' in
    single-file mode -- produce an empty file name); it is resolved first.'''
    expected = Path('mydata').resolve().as_posix()
    assert target_url_for(Path('mydata'), single_file=False) == f'file:///{expected}/scans/{{scan}}.h5'
    assert target_url_for(Path('mydata'), single_file=True) == f'file:///{expected}/mydata.h5#{{scan}}'


def test_file_url_does_not_double_the_posix_root_slash():
    '''A POSIX absolute path's as_posix() already carries the leading slash
    (unlike a resolved Windows path, which starts with a drive letter), so
    naively gluing 'file:///' + as_posix() doubles it into 'file:////...'.
    Seen for real on a JupyterHub container: pxs-prepare's reported target
    was 'file:////home/jovyan/work/Proposals/202601_PtAu/...'.'''
    posix = PurePosixPath('/home/jovyan/work/Proposals/202601_PtAu')
    assert _file_url(posix) == 'file:///home/jovyan/work/Proposals/202601_PtAu'


def test_s3_target_urls(tmp_path):
    root = tmp_path/'202601_PtAu'
    assert target_url_for(root, s3_bucket='pxsrepo') == 's3://pxsrepo/202601_PtAu/{scan}.zarr'
    assert spice_url_for(root, root/'spice', s3_bucket='pxsrepo') == 's3://pxsrepo/202601_PtAu/spice'
    assert ':///' not in target_url_for(root, s3_bucket='pxsrepo')
    assert target_url_for(root, single_file=True, s3_bucket='pxsrepo').endswith('{scan}.zarr')


@pytest.mark.parametrize('argv', [
    ['P', '--s3', '--single-file'], ['P', '--s3', '--output-root', 'o'],
    ['--s3-login', '--force'], ['P', '--s3-bucket', 'b'], ['P', '--force-spice'],
])
def test_rejected_flag_combinations(argv):
    with pytest.raises(SystemExit):
        main(argv)


def _fake_pm(fs, target_url, calls=None):
    '''Runner stub standing in for `pm`: writes the store the real one would.

    The import loop post-checks that the store it just asked for actually
    holds arrays, so a no-op runner is indistinguishable from a `pm` that
    exited 0 having written nothing -- which is a failure, not a pass.
    '''
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        scan = next(a.split('=', 1)[1] for a in cmd if a.startswith('only='))
        fs.pipe_file(target_url.format(scan=scan) + '/instrument/pilatus/images/zarr.json', b'{}')
    return run


def test_s3_loop_one_command_per_scan_with_only(tmp_path, fake_fs):
    root = _proposal(tmp_path/'202601_PtAu'); calls = []
    url = 's3://pxsrepo/202601_PtAu/{scan}.zarr'
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, pm=['pm'],
                           runner=_fake_pm(fake_fs, url, calls))
    assert len(calls) == 2 and res.imported == ['r0001', 'r0002'] and res.skipped == []
    assert 'only=r0001' in calls[0] and 'only=r0002' in calls[1]
    assert calls[0][calls[0].index('-W')+1] == 'zarr' and 'overwrite_data=True' in calls[0]
    assert calls[0][calls[0].index('-u')+1] == url


def test_s3_skips_manifested_scans_and_reimports_on_source_mismatch(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); url = manifest_url('pxsrepo', 'P')
    write_manifest(fake_fs, url, {'r0001': {'source': '20260108/115550', 'imported': 'T'},
                                  'r0002': {'source': 'WRONG', 'imported': 'T'}})
    calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                           runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr', calls))
    assert res.skipped == ['r0001'] and res.imported == ['r0002'] and len(calls) == 1


def test_s3_force_reimports_everything(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    write_manifest(fake_fs, manifest_url('pxsrepo', 'P'),
                   {'r0001': {'source': '20260108/115550', 'imported': 'T'}})
    calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', force=True, fs=fake_fs,
                           runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr', calls))
    assert res.skipped == [] and len(calls) == 2


def test_s3_deletes_existing_prefix_before_import(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    fake_fs.pipe('s3://pxsrepo/P/r0001.zarr/stale/zarr.json', b'{}')
    prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                     runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr'))
    assert not fake_fs.exists('s3://pxsrepo/P/r0001.zarr/stale/zarr.json')


def test_s3_failure_keeps_earlier_manifest_entries(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    good = _fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr')
    def runner(cmd, **kwargs):
        if 'only=r0002' in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        good(cmd, **kwargs)
    with pytest.raises(RuntimeError, match='r0002'):
        prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=runner)
    assert set(read_manifest(fake_fs, manifest_url('pxsrepo', 'P'))) == {'r0001'}


def test_s3_empty_store_after_zero_exit_is_a_failure(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    with pytest.raises(RuntimeError, match='r0001'):
        prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=lambda cmd, **kw: None)
    assert read_manifest(fake_fs, manifest_url('pxsrepo', 'P')) == {}


def test_s3_without_configured_profile_names_the_login_command(tmp_path, monkeypatch):
    monkeypatch.setattr('pxs_import.prepare.s3.credentials_available',
                        lambda *a, **k: False)
    with pytest.raises(RuntimeError, match='--s3-login'):
        prepare_proposal(_proposal(tmp_path/'P'), s3_bucket='pxsrepo')


def test_s3_dry_run_touches_nothing(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', dry_run=True, fs=fake_fs,
                           runner=lambda cmd, **kw: calls.append(cmd))
    assert calls == [] and len(res.command) == 2
    assert read_manifest(fake_fs, manifest_url('pxsrepo', 'P')) == {}


def test_s3_uploads_spice(tmp_path, fake_fs, capsys):
    root = _proposal(tmp_path/'P'); (root/'spice').mkdir(); (root/'spice'/'a.json').write_text('{}')
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                           runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr'))
    assert fake_fs.exists('s3://pxsrepo/P/spice/a.json')
    assert res.spice_url == 's3://pxsrepo/P/spice'
    assert "Proposal(url='s3://pxsrepo/P/{scan}.zarr', spice='s3://pxsrepo/P/spice')" in capsys.readouterr().out


def test_s3_refuses_existing_remote_spice_unless_forced(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); (root/'spice').mkdir(); (root/'spice'/'a.json').write_text('{}')
    fake_fs.pipe('s3://pxsrepo/P/spice/old.json', b'{}')
    runner = _fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr')
    with pytest.raises(FileExistsError, match='force-spice'):
        prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=runner)
    assert not fake_fs.exists('s3://pxsrepo/P/spice/a.json')
    prepare_proposal(root, s3_bucket='pxsrepo', force_spice=True, force=True, fs=fake_fs,
                     runner=runner)
    assert fake_fs.exists('s3://pxsrepo/P/spice/a.json')


def test_s3_without_local_spice_completes(tmp_path, fake_fs, capsys):
    res = prepare_proposal(_proposal(tmp_path/'P'), s3_bucket='pxsrepo', fs=fake_fs,
                           runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr'))
    assert res.spice_changes == [] and 'not uploaded' in capsys.readouterr().out


class _OkFS:
    def ls(self, path, **kwargs):
        return []


def _ok_fs():
    return _OkFS()


def _scripted(answers):
    '''A `prompt` that replays scripted answers; '' means "take the default".'''
    pending = iter(answers)
    def prompt(label, *, secret=False, default=None):
        value = next(pending)
        return default if (value == '' and default is not None) else value
    return prompt


def test_s3_login_writes_profile_and_never_echoes_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr('pxs_import.prepare.s3.filesystem', lambda p=None: _ok_fs())
    rc = s3_login(home=tmp_path, prompt=_scripted(['AKIA', 'topsecret', '', '']))
    assert rc == 0 and 'topsecret' in (tmp_path/'.aws'/'credentials').read_text()
    assert 'topsecret' not in capsys.readouterr().out


def test_s3_login_reports_bad_credentials_and_names_the_files(tmp_path, capsys, monkeypatch):
    def boom(p=None):
        raise OSError('403')
    monkeypatch.setattr('pxs_import.prepare.s3.filesystem', boom)
    assert s3_login(home=tmp_path, prompt=_scripted(['AKIA', 's', '', ''])) == 1
    assert 'credentials' in capsys.readouterr().out


def test_s3_profile_reaches_the_pm_subprocess(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); seen = []
    base = _fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr')
    def runner(cmd, **kwargs):
        seen.append(kwargs); base(cmd, **kwargs)
    prepare_proposal(root, s3_bucket='pxsrepo', s3_profile='pxs', fs=fake_fs, runner=runner)
    assert seen and all(kw['env']['AWS_PROFILE'] == 'pxs' for kw in seen)


def test_credentials_available_accepts_env_keys_and_aws_profile(tmp_path, monkeypatch):
    monkeypatch.delenv('AWS_ACCESS_KEY_ID', raising=False)
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    assert not credentials_available(home=tmp_path)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'AKIA')
    assert credentials_available(home=tmp_path)
    monkeypatch.delenv('AWS_ACCESS_KEY_ID')
    write_profile('AKIA', 's', profile='other', home=tmp_path)
    assert not credentials_available(home=tmp_path)
    monkeypatch.setenv('AWS_PROFILE', 'other')
    assert credentials_available(home=tmp_path)


def test_s3_dry_run_prints_the_commands(tmp_path, fake_fs, capsys):
    prepare_proposal(_proposal(tmp_path/'P'), s3_bucket='pxsrepo', dry_run=True,
                     fs=fake_fs, runner=lambda cmd, **kw: None)
    out = capsys.readouterr().out
    assert 'only=r0001' in out and 'only=r0002' in out


def test_s3_reports_skipped_scans(tmp_path, fake_fs, capsys):
    root = _proposal(tmp_path/'P')
    write_manifest(fake_fs, manifest_url('pxsrepo', 'P'),
                   {'r0001': {'source': '20260108/115550', 'imported': 'T'}})
    prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                     runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr'))
    out = capsys.readouterr().out
    assert 'skipped 1' in out and 'r0001' in out


def test_s3_force_keeps_manifest_entries_for_scans_not_yet_reimported(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); url = manifest_url('pxsrepo', 'P')
    write_manifest(fake_fs, url, {'r0001': {'source': '20260108/115550', 'imported': 'T'},
                                  'r0002': {'source': '20260203/120401', 'imported': 'T'}})
    good = _fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr')
    def runner(cmd, **kwargs):
        if 'only=r0002' in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        good(cmd, **kwargs)
    with pytest.raises(RuntimeError, match='r0002'):
        prepare_proposal(root, s3_bucket='pxsrepo', force=True, fs=fake_fs, runner=runner)
    assert set(read_manifest(fake_fs, url)) == {'r0001', 'r0002'}


def test_mapping_table_records_the_source_root(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    write_alignment(a/'20250408'/'120000', n_rows=2)
    write_alignment(b/'20250408'/'110000', n_rows=2)
    ws = tmp_path/'prop'
    prepare_proposal(ws, sources=[a, b], pm=['pm'], runner=lambda cmd, **kw: None)
    rows = list(csv.DictReader((ws/'scan-mapping.csv').open(newline='', encoding='utf-8')))
    assert [r['root'] for r in rows] == [str(b.resolve()), str(a.resolve())]


def test_read_mapping_table_tolerates_the_old_three_column_form(tmp_path):
    path = tmp_path/'scan-mapping.csv'
    path.write_text('scan,source,type\nr0001,20250408/151027,alignment\n')
    assert read_mapping_table(path) == {'20250408/151027': 'r0001'}
    assert read_mapping_table(tmp_path/'absent.csv') == {}


def test_renumbered_reports_moved_scans(tmp_path):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'120000', n_rows=2)
    write_alignment(src/'20250408'/'160000', n_rows=2)
    mapping = number_scans(discover_all([src]))
    previous = {'20250408/120000': 'r0001', '20250408/160000': 'r0003'}
    assert renumbered(previous, mapping) == [('20250408/160000', 'r0003', 'r0002')]


def test_reimport_with_shifted_numbers_is_refused_without_force(tmp_path):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'120000', n_rows=2)
    ws = tmp_path/'prop'
    prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)
    write_alignment(src/'20250408'/'110000', n_rows=2)      # lands before r0001
    with pytest.raises(RuntimeError, match='20250408/120000'):
        prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)


def test_reimport_with_shifted_numbers_proceeds_under_force(tmp_path, caplog):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'120000', n_rows=2)
    ws = tmp_path/'prop'
    prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)
    write_alignment(src/'20250408'/'110000', n_rows=2)
    with caplog.at_level(logging.WARNING):
        prepare_proposal(ws, sources=[src], force=True, pm=['pm'],
                          runner=lambda cmd, **kw: None)
    assert '20250408/120000' in caplog.text


def test_manifest_records_the_root_and_old_entries_still_skip(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    url = manifest_url('pxsrepo', 'P')
    prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                      runner=_fake_pm(fake_fs, target_url_for(root, s3_bucket='pxsrepo')))
    assert read_manifest(fake_fs, url)['r0001']['root'] == str(root.resolve())

    # An entry written before `root` existed must still be honoured.
    old = read_manifest(fake_fs, url)
    for entry in old.values():
        entry.pop('root')
    write_manifest(fake_fs, url, old)
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                            runner=lambda cmd, **kw: pytest.fail('should have skipped'))
    assert res.skipped


def test_manifest_skip_also_checks_root_not_just_source(tmp_path, fake_fs):
    '''Two scans with the same YYYYMMDD/HHMMSS from different acquisition
    folders are only told apart by `root` -- the skip check must not re-use a
    manifest entry recorded under a different one.'''
    root = _proposal(tmp_path/'P')
    url = manifest_url('pxsrepo', 'P')
    write_manifest(fake_fs, url, {'r0001': {'source': '20260108/115550', 'imported': 'T',
                                            'root': str(tmp_path/'elsewhere')}})
    calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                           runner=_fake_pm(fake_fs, 's3://pxsrepo/P/{scan}.zarr', calls))
    assert res.skipped == [] and 'r0001' in res.imported
    assert read_manifest(fake_fs, url)['r0001']['root'] == str(root.resolve())


def test_empty_import_is_refused_and_keeps_the_previous_mapping(tmp_path):
    '''A typo in --from must not look like a successful import.'''
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'151027', n_rows=2)
    ws = tmp_path/'prop'
    prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)
    before = (ws/'scan-mapping.csv').read_text()
    with pytest.raises(RuntimeError, match='no scans'):
        prepare_proposal(ws, sources=[src], start='20300101', force=True,
                          pm=['pm'], runner=lambda cmd, **kw: None)
    assert (ws/'scan-mapping.csv').read_text() == before


def test_main_fills_in_today_when_to_is_omitted(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr('pxs_import.prepare.prepare_proposal',
                         lambda *a, **kw: seen.update(kw))
    main([str(tmp_path/'prop'), '--source', str(tmp_path/'src'), '--from', '2025-04-08'])
    assert seen['end'] == datetime.now().strftime('%Y%m%d')


def test_sources_and_range_reach_stderr(tmp_path, capsys):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'151027', n_rows=2)
    prepare_proposal(tmp_path/'prop', sources=[src], start='20250408',
                      pm=['pm'], runner=lambda cmd, **kw: None)
    err = capsys.readouterr().err
    assert str(src) in err and '20250408' in err


def test_renumbering_is_detected_before_the_existing_output_guard(tmp_path):
    '''Otherwise a real re-import hits FileExistsError first and never sees it.'''
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'120000', n_rows=2)
    ws = tmp_path/'prop'
    prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)
    (ws/'scans').mkdir(parents=True, exist_ok=True)
    (ws/'scans'/'r0001.h5').write_text('')         # a real previous import
    write_alignment(src/'20250408'/'110000', n_rows=2)
    with pytest.raises(RuntimeError, match='change number'):
        prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)


def test_single_call_passes_the_mapping_as_a_file(tmp_path):
    '''Windows caps a command line at 32767 characters; a mapping has no cap.'''
    src = tmp_path/'src'
    for i in range(3):
        write_alignment(src/'20250408'/f'12000{i}', n_rows=2)
    ws = tmp_path/'prop'
    calls = []
    prepare_proposal(ws, sources=[src], pm=['pm'],
                      runner=lambda cmd, **kw: calls.append(cmd))
    arg = next(a for a in calls[0] if a.startswith('resolved_file='))
    payload = json.loads(Path(arg[len('resolved_file='):]).read_text(encoding='utf-8'))
    assert [t[0] for t in payload] == ['r0001', 'r0002', 'r0003']


def test_workspace_is_created_even_with_output_root(tmp_path):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'151027', n_rows=2)
    ws = tmp_path/'prop'
    prepare_proposal(ws, sources=[src], output_root=tmp_path/'out',
                      pm=['pm'], runner=lambda cmd, **kw: None)
    assert ws.is_dir()


def test_base_route_without_from_is_refused(tmp_path):
    '''The program folders hold every beamtime since 2011.

    A RuntimeError, not a SystemExit: `prepare_proposal` is the function a
    GUI would call directly, and it must hand a refusal back as something
    its caller can catch, not force a process exit.
    '''
    ws = tmp_path/'prop'; ws.mkdir()
    base = tmp_path/'base'
    write_alignment(base/'PilatusScanGoniometer'/'20250408'/'151027', n_rows=2)
    with pytest.raises(RuntimeError, match='date'):
        prepare_proposal(ws, base=base, pm=['pm'], runner=lambda cmd, **kw: None)


def test_main_still_exits_1_on_base_route_without_from(tmp_path, capsys):
    '''The CLI wrapper still turns the refusal into a clean exit, same as before.'''
    ws = tmp_path/'prop'; ws.mkdir()
    base = tmp_path/'base'
    write_alignment(base/'PilatusScanGoniometer'/'20250408'/'151027', n_rows=2)
    assert main([str(ws), '--base', str(base)]) == 1
    assert 'date' in capsys.readouterr().err


def test_sources_merge_into_one_resolved_mapping(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    write_alignment(a/'20250408'/'120000', n_rows=2)
    write_alignment(b/'20250408'/'110000', n_rows=2)
    calls = []
    prepare_proposal(tmp_path/'prop', sources=[a, b], pm=['pm'],
                      runner=lambda cmd, **kw: calls.append(cmd))
    triples = _resolved_of(calls[0])
    assert [t[0] for t in triples] == ['r0001', 'r0002']
    assert Path(triples[0][1]).name == '110000'     # chronological across sources


def test_date_range_reaches_the_resolved_mapping(tmp_path):
    src = tmp_path/'src'
    for day in ('20250407', '20250408', '20250409'):
        write_alignment(src/day/'151027', n_rows=2)
    calls = []
    prepare_proposal(tmp_path/'prop', sources=[src], start='20250408', end='20250408',
                      pm=['pm'], runner=lambda cmd, **kw: calls.append(cmd))
    triples = _resolved_of(calls[0])
    assert len(triples) == 1 and '20250408' in triples[0][1]


def test_workspace_is_created_when_absent(tmp_path):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'151027', n_rows=2)
    ws = tmp_path/'brand-new'
    assert not ws.exists()
    prepare_proposal(ws, sources=[src], pm=['pm'], runner=lambda cmd, **kw: None)
    assert ws.is_dir()


def test_main_normalises_iso_dates(tmp_path, monkeypatch):
    src = tmp_path/'src'
    write_alignment(src/'20250408'/'151027', n_rows=2)
    write_alignment(src/'20250409'/'090000', n_rows=2)
    seen = {}
    monkeypatch.setattr('pxs_import.prepare.prepare_proposal',
                         lambda *a, **kw: seen.update(kw))
    main([str(tmp_path/'prop'), '--source', str(src),
          '--from', '2025-04-08', '--to', '2025-04-08', '--dry-run'])
    assert seen['start'] == '20250408' and seen['end'] == '20250408'


def test_the_cli_names_itself_pxs_prepare(capsys):
    '''`--help` and every argparse error print the program's own name.

    `udkm-prepare` is the name this command had inside xraydc. After a plain
    `pip install pxs-import` no command by that name exists, so a usage line
    or an error that says `udkm-prepare` sends the reader to something they
    cannot run.
    '''
    with pytest.raises(SystemExit):
        main(['--help'])
    assert capsys.readouterr().out.startswith('usage: pxs-prepare')

    with pytest.raises(SystemExit):
        main(['--not-an-option'])
    assert capsys.readouterr().err.startswith('usage: pxs-prepare')


def test_no_module_sends_the_user_to_the_old_console_script():
    '''The rename has to reach the strings, not just the entry point.

    Scanned over the whole package rather than over `prepare.py`: the name
    also rides in a runtime error telling a first-time `--s3` user what to
    run, and in docstrings that mkdocstrings publishes as the API reference.
    '''
    import pxs_import

    offenders = [path.name for path in Path(pxs_import.__file__).parent.glob('*.py')
                 if 'udkm-prepare' in path.read_text(encoding='utf-8')]
    assert offenders == []
