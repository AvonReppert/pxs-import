import pytest

from pxs_import.s3 import (MANIFEST_NAME, write_profile, profile_configured, manifest_url,
                           read_manifest, write_manifest, prefix_exists,
                           delete_prefix, store_has_arrays, upload_dir)


def test_write_profile_preserves_other_sections(tmp_path):
    aws = tmp_path/'.aws'; aws.mkdir()
    (aws/'credentials').write_text('[other]\naws_access_key_id = keep\n')
    (aws/'config').write_text('[profile other]\nregion = eu\n')
    write_profile('AKIA', 's3cr3t', home=tmp_path)
    cred = (aws/'credentials').read_text(); cfg = (aws/'config').read_text()
    assert 'keep' in cred and '[default]' in cred and 's3cr3t' in cred
    assert '[profile other]' in cfg and '[default]' in cfg
    assert 'https://s3.rz.uni-potsdam.de' in cfg and 'US' in cfg


def test_write_profile_named_profile_uses_profile_prefix_in_config_only(tmp_path):
    write_profile('AKIA', 's', profile='pxs', home=tmp_path)
    assert '[pxs]' in (tmp_path/'.aws'/'credentials').read_text()
    assert '[profile pxs]' in (tmp_path/'.aws'/'config').read_text()


def test_write_profile_refuses_malformed_credentials_file(tmp_path):
    aws = tmp_path/'.aws'; aws.mkdir()
    (aws/'credentials').write_text('this is not ini\n= = =\n')
    with pytest.raises(ValueError, match='credentials'):
        write_profile('AKIA', 's', home=tmp_path)
    assert (aws/'credentials').read_text() == 'this is not ini\n= = =\n'


def test_profile_configured(tmp_path):
    assert not profile_configured(home=tmp_path)
    write_profile('AKIA', 's', home=tmp_path)
    assert profile_configured(home=tmp_path) and not profile_configured('pxs', home=tmp_path)


def test_manifest_roundtrip_and_absent(fake_fs):
    url = manifest_url('pxsrepo', '202601_PtAu')
    assert url == 's3://pxsrepo/202601_PtAu/.udkm-import.json'
    assert read_manifest(fake_fs, url) == {}
    write_manifest(fake_fs, url, {'r0001': {'source': '20260108/115550', 'imported': 'T'}})
    assert read_manifest(fake_fs, url)['r0001']['source'] == '20260108/115550'


def test_manifest_unparseable_or_future_version_reads_empty(fake_fs, caplog):
    caplog.set_level('WARNING')
    url = manifest_url('pxsrepo', 'P')
    fake_fs.pipe(url, b'{not json')
    assert read_manifest(fake_fs, url) == {} and 'P' in caplog.text
    fake_fs.pipe(url, b'{"version": 2, "scans": {"r0001": {}}}')
    assert read_manifest(fake_fs, url) == {}


def test_prefix_exists_and_delete(fake_fs):
    url = 's3://pxsrepo/P/r0001.zarr'
    assert not prefix_exists(fake_fs, url)
    delete_prefix(fake_fs, url)
    fake_fs.pipe(url + '/zarr.json', b'{}')
    assert prefix_exists(fake_fs, url)
    delete_prefix(fake_fs, url); assert not prefix_exists(fake_fs, url)


def test_store_has_arrays(fake_fs):
    url = 's3://pxsrepo/P/r0001.zarr'
    fake_fs.pipe(url + '/zarr.json', b'{}')
    assert not store_has_arrays(fake_fs, url)
    fake_fs.pipe(url + '/instrument/pilatus/images/zarr.json', b'{}')
    assert store_has_arrays(fake_fs, url)


def test_upload_dir(tmp_path, fake_fs):
    d = tmp_path/'spice'; (d/'sub').mkdir(parents=True)
    (d/'a.json').write_text('{}'); (d/'sub'/'b.json').write_text('{}')
    assert upload_dir(fake_fs, d, 's3://pxsrepo/P/spice') == 2
    assert fake_fs.exists('s3://pxsrepo/P/spice/sub/b.json')


def test_manifest_name_is_not_renamed_with_the_package():
    """Live buckets already hold this file under this name.

    Renaming it with the package would make every proposal that was already
    imported look un-imported, and the next `pxs-prepare --s3` would silently
    re-upload it from scratch.
    """
    assert MANIFEST_NAME == '.udkm-import.json'
