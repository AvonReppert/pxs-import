import json, uuid, pytest
from pxs_import.spice import load_records, target_key_for, migrate_spice, fix_recipes

def _rec(d, handle, anchor, data=None, rev=1):
    p = d / str(uuid.uuid4())
    p.write_text(json.dumps({'handle': handle, 'anchor': anchor, 'revision': rev, 'data': data or {}}))
    return p

@pytest.fixture
def spice(tmp_path):
    d = tmp_path/'spice'; d.mkdir(); (d/'.ipynb_checkpoints').mkdir()
    (d/'notes.txt').write_text('not json')
    (d/'bogus_list').write_text('[1, 2, 3]')                       # F2: not a dict
    (d/'no_anchor').write_text(json.dumps({'handle': 'x'}))        # F2: dict, no anchor
    _rec(d, 'exp_info', '20260108/143930', rev=5); _rec(d, 'exp_info', '20260108/143930', rev=4)
    _rec(d, 'qargs', '20260108/120821'); _rec(d, 'offsets', '')
    _rec(d, 'recipes', '', {'sRSM': 'pymod:///xraydc.cooking#sRSM', 'xRSS': 'pymod:///xraydc.cooking#TRSS_2d_fix'})
    return d

MAPPING = {'20260108/100331': 'r0001', '20260108/120821': 'r0002', '20260108/143930': 'r0003'}

def test_load_records_skips_dirs_and_non_json(spice):              # F2
    assert len(load_records(spice)) == 5
    migrate_spice(spice, MAPPING, dry_run=True)        # must not raise on the bad files

def test_target_key_for(spice):
    assert target_key_for('20260108/143930', MAPPING) == 'r0003'
    assert target_key_for('20260108/130000', MAPPING) == 'r0003'
    assert target_key_for('', MAPPING) == '' and target_key_for('r0007', MAPPING) == 'r0007'
    with pytest.raises(ValueError, match='20260108/150000'):                 # Review Focus 5
        target_key_for('20260108/150000', MAPPING)

def test_target_key_for_non_string_anchor_passes_through(spice):
    assert target_key_for(5, MAPPING) == 5
    _rec(spice, 'misc', 5)
    changes = migrate_spice(spice, MAPPING)
    assert not any(c.handle == 'misc' for c in changes)

def test_migrate_rewrites_anchors_once_with_backup(spice):
    changes = migrate_spice(spice, MAPPING)
    assert sorted((c.handle, c.new_anchor) for c in changes) == [('exp_info', 'r0003'), ('exp_info', 'r0003'), ('qargs', 'r0002')]
    assert (spice.parent/'spice.pre-migration').is_dir()
    assert {r['anchor'] for _, r in load_records(spice)} == {'', 'r0002', 'r0003'}
    assert migrate_spice(spice, MAPPING) == []          # idempotent

def test_migrate_dry_run_writes_nothing(spice):
    assert len(migrate_spice(spice, MAPPING, dry_run=True)) == 3
    assert not (spice.parent/'spice.pre-migration').exists()
    assert '20260108/143930' in {r['anchor'] for _, r in load_records(spice)}

def test_fix_recipes_removes_xrss(spice):
    assert fix_recipes(spice) is True
    recipes = [r for _, r in load_records(spice) if r['handle'] == 'recipes'][0]['data']
    assert 'xRSS' not in recipes and 'sRSM' in recipes
    assert fix_recipes(spice) is False

def test_fix_recipes_backs_up_before_writing(tmp_path):             # F3
    d = tmp_path/'spice'; d.mkdir()
    rec_path = _rec(d, 'recipes', '',
                     {'sRSM': 'pymod:///xraydc.cooking#sRSM',
                      'xRSS': 'pymod:///xraydc.cooking#TRSS_2d_fix'})
    assert fix_recipes(d) is True
    backup_dir = d.parent/'spice.pre-migration'
    assert backup_dir.is_dir()
    backup_record = json.loads((backup_dir/rec_path.name).read_text())
    assert 'xRSS' in backup_record['data']
