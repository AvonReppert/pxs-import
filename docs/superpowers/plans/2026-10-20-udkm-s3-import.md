# UDKM → S3 Import Implementation Plan

> **Historical note.** This document was written while the importer lived in
> `xraydc`, as `src/xraydc/io/udkm_*.py`. Every module, command and function
> name in it has been rewritten to the one this package uses --
> `pxs_import.format`, `pxs_import.reader`, `scans_from_pxs`, `pxs-prepare`
> and so on; nothing else was touched. The three names that deliberately kept
> their `udkm` spelling are unchanged here too: the `.udkm-import.json` S3
> manifest, the `UDKM_BASE` environment variable, and the `udkm_source` scan
> key nx5d sees.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `pxs-prepare --s3`, importing a UDKM proposal to one Zarr store per scan on S3, resumable and with credentials stored once by `--s3-login`.

**Architecture:** A new `src/pxs_import/s3.py` owns every S3 and credential concern behind plain functions taking an fsspec filesystem. `prepare.py` grows an S3 branch in `target_url_for`, new CLI flags, and a per-scan `pm` loop driven by a manifest object on S3. `scans_from_pxs` gains an `only=` filter so each `pm` invocation reads one scan's TIFFs rather than all of them. No nx5d change.

**Tech Stack:** Python ≥3.10, `fsspec`/`s3fs`, `zarr` 3.x (via nx5d's writer, not directly), `configparser`, `getpass`, pytest.

**Spec:** [docs/superpowers/specs/2026-10-20-udkm-s3-import-design.md](../specs/2026-10-20-udkm-s3-import-design.md)

## Global Constraints

- `xraydc` must never `import nx5d`. `prepare` shells out to the `pm` console script only.
- `import xraydc` must not pull `s3fs`, `fsspec` or `boto*` into `sys.modules`. All S3 imports are function-local, matching how `signal.py` treats `xrayutilities`.
- S3 URLs use the two-slash form: `s3://pxsrepo/<proposal>/…`, never `s3:///pxsrepo/…`.
- Defaults, verbatim from the spec: bucket `pxsrepo`, endpoint `https://s3.rz.uni-potsdam.de`, region `US`, profile `default`, manifest object `.udkm-import.json`, manifest `version` `1`.
- `AWS_CA_BUNDLE` is never set by this code.
- The secret is read with `getpass` and must never be echoed, logged, or interpolated into an exception message.
- `s3fs` is added to the `udkm` **and** `test` extras in `pyproject.toml` (CI installs only `test`).
- Docstrings: Google-style, `'''` quotes, matching the surrounding `io/` modules.
- Tests are compact in the house style of `tests/prepare_test.py` — several assertions per test, `tmp_path`, no network.

## Review Focus

- **`--s3` before `--s3-login` has ever run** → an actionable error naming `--s3-login`, not a raw botocore `NoCredentialsError`. (Task 5)
- **`--s3-login` over a malformed `~/.aws/credentials`** → refuse and leave the file untouched; never truncate a file it could not parse. (Task 2)
- **A manifest whose `version` is not 1** → treated as empty with a warning, never misread as v1. (Task 3)
- **A proposal with no local `spice/` folder** → `--s3` still completes; no spice push is attempted and the printed line says spice was not uploaded. (Task 6)
- **A `pm`/network failure partway through the loop** → the manifest retains every scan completed before it, and the error names the failing scan. (Task 5)

---

### Task 1: Reader-side `only=` filter

**Files:**
- Modify: `src/pxs_import/reader.py:288-348`
- Test: `tests/reader_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `scans_from_pxs(source, *, photon_energy=8048.0, scan_format='r{iscan:04d}', mapping_file=None, only: str | None = None)`. `only` is one scan key or a comma-separated list. Applied immediately after `number_scans`, before `parse_dat`/`plan_frames`/`load_stack`. `only=None` is today's behaviour. An unknown key raises `ValueError` naming it and listing the valid keys. When `only` is set, the mapping table is not printed to stderr (`reader.py:333`) and not logged.
- `iscan` in the yielded key stays the scan's position in the **full** mapping, not in the filtered subset.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/reader_test.py -k only -v`
Expected: FAIL — `scans_from_pxs() got an unexpected keyword argument 'only'`

- [ ] **Step 3: Add the `only` parameter to `scans_from_pxs`**

Parse `only` into an ordered list of keys; validate every key against the mapping before the loop and raise `ValueError` listing unknown ones; filter `mapping` to the selected keys while keeping each scan's original 1-based `iscan`; guard the `logger.info`/`print` pair at `reader.py:332-333` on `only is None`. Extend the docstring's `Args:` and `Raises:`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/reader_test.py -v`
Expected: PASS, including the pre-existing reader tests.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/reader.py tests/reader_test.py
git commit -m "feat(udkm): only= scan filter on the reader, applied before frame loading"
```

---

### Task 2: Credential profile files

**Files:**
- Create: `src/pxs_import/s3.py`
- Create: `tests/s3_test.py`

**Interfaces:**
- Produces, in `s3.py`:
  ```python
  S3_BUCKET_DEFAULT = 'pxsrepo'
  S3_ENDPOINT_DEFAULT = 'https://s3.rz.uni-potsdam.de'
  S3_REGION_DEFAULT = 'US'

  def aws_paths(home: Path | None = None) -> tuple[Path, Path]      # (credentials, config)
  def profile_configured(profile: str = 'default', *, home=None) -> bool
  def write_profile(key_id: str, secret: str, *, endpoint=S3_ENDPOINT_DEFAULT,
                    region=S3_REGION_DEFAULT, profile='default',
                    home: Path | None = None) -> tuple[Path, Path]
  ```
- `home` defaults to `Path.home()`; tests pass `tmp_path`.
- `write_profile` writes `[<profile>]` into `credentials` and `[<profile>]` (for `default`) or `[profile <profile>]` (otherwise) into `config`, via `configparser`, preserving all other sections. It creates `~/.aws/` if missing, and after writing attempts owner-only permissions, logging a warning on failure rather than raising.
- A `credentials` or `config` file that `configparser` cannot parse makes `write_profile` raise `ValueError` naming the file, having written nothing.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/s3_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pxs_import.s3'`

- [ ] **Step 3: Implement the three functions and the constants in `src/pxs_import/s3.py`**

Module docstring states the layering rule from Global Constraints. Read both files with `configparser.ConfigParser()` before modifying either, so a parse error aborts before any write.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/s3_test.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/s3.py tests/s3_test.py
git commit -m "feat(udkm): ~/.aws profile writer for the S3 target"
```

---

### Task 3: S3 object helpers and the manifest

**Files:**
- Modify: `src/pxs_import/s3.py`
- Test: `tests/s3_test.py`

**Interfaces:**
- Consumes: Task 2's constants.
- Produces:
  ```python
  MANIFEST_NAME = '.udkm-import.json'
  MANIFEST_VERSION = 1

  def filesystem(profile: str | None = None)                 # fsspec S3 filesystem; s3fs imported here
  def manifest_url(bucket: str, proposal: str) -> str
  def read_manifest(fs, url: str) -> dict[str, dict]          # {} when absent/unparseable/wrong version
  def write_manifest(fs, url: str, scans: dict[str, dict]) -> None
  def prefix_exists(fs, url: str) -> bool
  def delete_prefix(fs, url: str) -> None                     # no-op when absent
  def store_has_arrays(fs, url: str) -> bool
  def upload_dir(fs, local: Path, url: str) -> int            # returns file count
  ```
- `read_manifest` returns the inner `scans` mapping, `{key: {'source': str, 'imported': str}}`. A missing object returns `{}` silently; an unparseable one or a `version` other than `MANIFEST_VERSION` returns `{}` and logs a warning naming the url.
- `write_manifest` writes `{'version': MANIFEST_VERSION, 'scans': scans}` as JSON.
- `store_has_arrays` returns true when the store at `url` contains at least one Zarr array; implement by listing for a `zarr.json` below the root prefix. Do not import `zarr`.
- Tests use a fake filesystem object (a small class over a `dict[str, bytes]` implementing the `fs` methods used), not a real endpoint.

- [ ] **Step 1: Write the failing tests**

```python
def test_manifest_roundtrip_and_absent(fake_fs):
    url = manifest_url('pxsrepo', '202601_PtAu')
    assert url == 's3://pxsrepo/202601_PtAu/.udkm-import.json'
    assert read_manifest(fake_fs, url) == {}
    write_manifest(fake_fs, url, {'r0001': {'source': '20260108/115550', 'imported': 'T'}})
    assert read_manifest(fake_fs, url)['r0001']['source'] == '20260108/115550'

def test_manifest_unparseable_or_future_version_reads_empty(fake_fs, caplog):
    url = manifest_url('pxsrepo', 'P')
    fake_fs.pipe(url, b'{not json')
    assert read_manifest(fake_fs, url) == {} and 'P' in caplog.text
    fake_fs.pipe(url, b'{"version": 2, "scans": {"r0001": {}}}')
    assert read_manifest(fake_fs, url) == {}

def test_prefix_exists_and_delete(fake_fs):
    url = 's3://pxsrepo/P/r0001.zarr'
    assert not prefix_exists(fake_fs, url)
    delete_prefix(fake_fs, url)                      # no-op, must not raise
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/s3_test.py -v`
Expected: FAIL — the new names are not defined.

- [ ] **Step 3: Implement the helpers and a `fake_fs` fixture**

`filesystem(profile)` imports `s3fs` inside the function and returns `s3fs.S3FileSystem(profile=profile)` when a profile is named, otherwise with no profile argument so botocore's default chain applies. The `fake_fs` fixture lives in `tests/s3_test.py`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/s3_test.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/s3.py tests/s3_test.py
git commit -m "feat(udkm): S3 manifest, prefix and upload helpers"
```

---

### Task 4: S3 target URLs and the CLI surface

**Files:**
- Modify: `src/pxs_import/prepare.py:51-77` (`target_url_for`), `:224-270` (`main`)
- Test: `tests/prepare_test.py`

**Interfaces:**
- Consumes: `s3.S3_BUCKET_DEFAULT`.
- Produces:
  ```python
  def target_url_for(proposal_root, *, single_file=False, output_root=None,
                     s3_bucket: str | None = None) -> str
  def spice_url_for(proposal_root, spice_dir: Path, *, s3_bucket: str | None = None) -> str
  ```
  With `s3_bucket` set, `target_url_for` returns `s3://<bucket>/<proposal>/{scan}.zarr` and `spice_url_for` returns `s3://<bucket>/<proposal>/spice`, both ignoring `single_file`/`output_root`. Without it, today's `file:///` forms.
- New `main` flags: `--s3`, `--s3-bucket` (default `S3_BUCKET_DEFAULT`), `--s3-profile` (default `None`), `--force-spice`, `--s3-login`. Rejections via `parser.error`: `--s3 --single-file`, `--s3 --output-root`, `--s3-login` with any flag other than `--s3-profile`, and `--s3-bucket`/`--force-spice` without `--s3`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py -k "s3 or rejected" -v`
Expected: FAIL — `target_url_for() got an unexpected keyword argument 's3_bucket'`

- [ ] **Step 3: Implement the S3 branch and the argument surface**

`--s3-login` is detected before `proposal_root` is required, so make `proposal_root` `nargs='?'` and `parser.error` when it is absent without `--s3-login`.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py -v`
Expected: PASS, including the pre-existing `file:///` URL tests.

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/prepare.py tests/prepare_test.py
git commit -m "feat(udkm): S3 target URLs and --s3 CLI surface"
```

---

### Task 5: The per-scan import loop

**Files:**
- Modify: `src/pxs_import/prepare.py:30-47` (`PrepareResult`), `:124-222` (`prepare_proposal`)
- Test: `tests/prepare_test.py`

**Interfaces:**
- Consumes: Task 1's `only=`, Task 3's `filesystem`/`manifest_url`/`read_manifest`/`write_manifest`/`prefix_exists`/`delete_prefix`/`store_has_arrays`, Task 4's URL builders and flags.
- Produces: `prepare_proposal(..., s3_bucket: str | None = None, s3_profile: str | None = None, force_spice: bool = False, fs=None)`. `fs` is the injection point for tests; when `None` and `s3_bucket` is set it comes from `s3.filesystem(s3_profile)`.
- `PrepareResult` gains `spice_url: str`, `skipped: list[str]`, `imported: list[str]`; `command` becomes `list[list[str]]` in every mode (local mode holds exactly one).
- Per scan to import, in mapping order: `delete_prefix` if `prefix_exists`; run the command below; require the runner to succeed **and** `store_has_arrays`; then append `{scan: {'source': folder.source, 'imported': <UTC ISO8601>}}` and `write_manifest`.
  ```
  [*pm, '-P', '-u', target_url, 'scan', 'import', str(proposal_root),
   '-R', 'pxs_import.reader:scans_from_pxs', f'photon_energy={photon_energy}',
   f'only={scan}', '-W', 'zarr', 'overwrite_data=True', 'frame_guard=None']
  ```
- Skip rule: skip iff the key is in the manifest **and** its `source` equals the current mapping's. `force=True` skips nothing.
- A failing scan raises `RuntimeError` naming it, after the manifest holds every scan completed before it.
- When `s3_bucket` is set and no credential profile is configured (`s3.profile_configured`), raise `RuntimeError` telling the user to run `pxs-prepare --s3-login`, before any S3 call.
- `dry_run` with `s3_bucket`: read the manifest, compute the skip set, build the commands, and return without deleting, running or writing anything.
- `existing_output` and the `--force` rmtree path stay local-only; they are not consulted when `s3_bucket` is set.

- [ ] **Step 1: Write the failing tests**

```python
def test_s3_loop_one_command_per_scan_with_only(tmp_path, fake_fs):
    root = _proposal(tmp_path/'202601_PtAu'); calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, pm=['pm'],
                           runner=lambda cmd, **kw: calls.append(cmd))
    assert len(calls) == 2 and res.imported == ['r0001', 'r0002'] and res.skipped == []
    assert 'only=r0001' in calls[0] and 'only=r0002' in calls[1]
    assert calls[0][calls[0].index('-W')+1] == 'zarr' and 'overwrite_data=True' in calls[0]
    assert calls[0][calls[0].index('-u')+1] == 's3://pxsrepo/202601_PtAu/{scan}.zarr'

def test_s3_skips_manifested_scans_and_reimports_on_source_mismatch(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); url = manifest_url('pxsrepo', 'P')
    write_manifest(fake_fs, url, {'r0001': {'source': '20260108/115550', 'imported': 'T'},
                                  'r0002': {'source': 'WRONG', 'imported': 'T'}})
    calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs,
                           runner=lambda cmd, **kw: calls.append(cmd))
    assert res.skipped == ['r0001'] and res.imported == ['r0002'] and len(calls) == 1

def test_s3_force_reimports_everything(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    write_manifest(fake_fs, manifest_url('pxsrepo', 'P'),
                   {'r0001': {'source': '20260108/115550', 'imported': 'T'}})
    calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', force=True, fs=fake_fs,
                           runner=lambda cmd, **kw: calls.append(cmd))
    assert res.skipped == [] and len(calls) == 2

def test_s3_deletes_existing_prefix_before_import(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    fake_fs.pipe('s3://pxsrepo/P/r0001.zarr/stale/zarr.json', b'{}')
    prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=lambda cmd, **kw: None)
    assert not fake_fs.exists('s3://pxsrepo/P/r0001.zarr/stale/zarr.json')

def test_s3_failure_keeps_earlier_manifest_entries(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P')
    def runner(cmd, **kw):
        if 'only=r0002' in cmd: raise subprocess.CalledProcessError(1, cmd)
    with pytest.raises(RuntimeError, match='r0002'):
        prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=runner)
    assert set(read_manifest(fake_fs, manifest_url('pxsrepo', 'P'))) == {'r0001'}

def test_s3_empty_store_after_zero_exit_is_a_failure(tmp_path, fake_fs, monkeypatch):
    root = _proposal(tmp_path/'P')
    monkeypatch.setattr('pxs_import.prepare.s3.store_has_arrays', lambda fs, url: False)
    with pytest.raises(RuntimeError, match='r0001'):
        prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=lambda cmd, **kw: None)
    assert read_manifest(fake_fs, manifest_url('pxsrepo', 'P')) == {}

def test_s3_without_configured_profile_names_the_login_command(tmp_path, monkeypatch):
    monkeypatch.setattr('pxs_import.prepare.s3.profile_configured', lambda *a, **k: False)
    with pytest.raises(RuntimeError, match='--s3-login'):
        prepare_proposal(_proposal(tmp_path/'P'), s3_bucket='pxsrepo')

def test_s3_dry_run_touches_nothing(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); calls = []
    res = prepare_proposal(root, s3_bucket='pxsrepo', dry_run=True, fs=fake_fs,
                           runner=lambda cmd, **kw: calls.append(cmd))
    assert calls == [] and len(res.command) == 2
    assert read_manifest(fake_fs, manifest_url('pxsrepo', 'P')) == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py -k s3 -v`
Expected: FAIL — `prepare_proposal() got an unexpected keyword argument 's3_bucket'`

- [ ] **Step 3: Implement the loop**

Import `s3` as a module in `prepare.py` (not its names) so the monkeypatch targets above resolve. Move the `fake_fs` fixture from `tests/s3_test.py` into a `conftest.py` under `tests/` so both modules share it — this is the repo's first `conftest.py`; note it in the commit message.

- [ ] **Step 4: Update the one existing assertion that `command`'s new shape breaks**

`tests/prepare_test.py:63` becomes `assert '-d' in res.command[0]`.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: PASS, no regressions in the local-mode tests.

- [ ] **Step 6: Commit**

```bash
git add src/pxs_import/prepare.py tests/prepare_test.py tests/s3_test.py tests/conftest.py
git commit -m "feat(udkm): resumable per-scan S3 import loop with manifest"
```

---

### Task 6: Spice push and the final output line

**Files:**
- Modify: `src/pxs_import/prepare.py` (end of `prepare_proposal`)
- Test: `tests/prepare_test.py`

**Interfaces:**
- Consumes: Task 3's `prefix_exists`/`upload_dir`, Task 4's `spice_url_for`, Task 5's `force_spice`.
- `fix_recipes`/`migrate_spice` keep their local `Path` signatures and run first, unchanged. The push is a separate step after them.
- A non-empty remote spice prefix without `force_spice` raises `FileExistsError` naming the prefix and `--force-spice`, and uploads nothing. `main` catches it and returns `1`, matching how it already handles `FileExistsError`.
- No local `spice/` directory: no push, no raise, and the printed line notes spice was not uploaded.
- The final print becomes `Proposal(url='<target_url>', spice='<spice_url>')` using `spice_url_for`.

- [ ] **Step 1: Write the failing tests**

```python
def test_s3_uploads_spice(tmp_path, fake_fs, capsys):
    root = _proposal(tmp_path/'P'); (root/'spice').mkdir(); (root/'spice'/'a.json').write_text('{}')
    res = prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=lambda cmd, **kw: None)
    assert fake_fs.exists('s3://pxsrepo/P/spice/a.json')
    assert res.spice_url == 's3://pxsrepo/P/spice'
    assert "Proposal(url='s3://pxsrepo/P/{scan}.zarr', spice='s3://pxsrepo/P/spice')" in capsys.readouterr().out

def test_s3_refuses_existing_remote_spice_unless_forced(tmp_path, fake_fs):
    root = _proposal(tmp_path/'P'); (root/'spice').mkdir(); (root/'spice'/'a.json').write_text('{}')
    fake_fs.pipe('s3://pxsrepo/P/spice/old.json', b'{}')
    with pytest.raises(FileExistsError, match='force-spice'):
        prepare_proposal(root, s3_bucket='pxsrepo', fs=fake_fs, runner=lambda cmd, **kw: None)
    assert not fake_fs.exists('s3://pxsrepo/P/spice/a.json')
    prepare_proposal(root, s3_bucket='pxsrepo', force_spice=True, force=True, fs=fake_fs,
                     runner=lambda cmd, **kw: None)
    assert fake_fs.exists('s3://pxsrepo/P/spice/a.json')

def test_s3_without_local_spice_completes(tmp_path, fake_fs, capsys):
    res = prepare_proposal(_proposal(tmp_path/'P'), s3_bucket='pxsrepo', fs=fake_fs,
                           runner=lambda cmd, **kw: None)
    assert res.spice_changes == [] and 'not uploaded' in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py -k spice -v`
Expected: FAIL — `AttributeError: 'PrepareResult' object has no attribute 'spice_url'`

- [ ] **Step 3: Implement the spice push and the output line**

- [ ] **Step 4: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pxs_import/prepare.py tests/prepare_test.py
git commit -m "feat(udkm): push local spice to S3 behind --force-spice"
```

---

### Task 7: `--s3-login` wiring, packaging and docs

**Files:**
- Modify: `src/pxs_import/prepare.py` (`main`), `pyproject.toml`, `tests/packaging_test.py`, `doc/pages/`
- Test: `tests/prepare_test.py`, `tests/packaging_test.py`

**Interfaces:**
- Consumes: Task 2's `write_profile`, Task 3's `filesystem`.
- Produces: `s3_login(*, profile=None, bucket=S3_BUCKET_DEFAULT, home=None, prompt=None) -> int` in `prepare.py`. `prompt` is a callable `prompt(label: str, *, secret: bool = False, default: str | None = None) -> str`, defaulting to one that wraps `input` and, when `secret`, `getpass.getpass`, and returns `default` on empty input. The tests' `_scripted(answers)` helper returns a `prompt` that pops successive strings off `answers` and ignores the label; `_ok_fs()` returns a filesystem stub whose `ls` succeeds.
- Flow: prompt for key id (visible), secret (`getpass`), endpoint and region (defaults pre-filled, Enter accepts); confirm before overwriting an existing same-named profile; `write_profile`; then validate by listing the bucket through `filesystem(profile)`, reporting a failure as a credential problem naming both written file paths and returning `1`.
- `pyproject.toml`: add `s3fs` to `[project.optional-dependencies]` `udkm`, `test` and `all`, with a comment matching the existing `tifffile`/`silx` note about CI installing only `test`.

- [ ] **Step 1: Write the failing tests**

```python
def test_s3_login_writes_profile_and_never_echoes_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr('pxs_import.prepare.s3.filesystem', lambda p=None: _ok_fs())
    rc = s3_login(home=tmp_path, prompt=_scripted(['AKIA', 'topsecret', '', '']))
    assert rc == 0 and 'topsecret' in (tmp_path/'.aws'/'credentials').read_text()
    assert 'topsecret' not in capsys.readouterr().out

def test_s3_login_reports_bad_credentials_and_names_the_files(tmp_path, capsys, monkeypatch):
    def boom(p=None): raise OSError('403')
    monkeypatch.setattr('pxs_import.prepare.s3.filesystem', boom)
    assert s3_login(home=tmp_path, prompt=_scripted(['AKIA', 's', '', ''])) == 1
    assert 'credentials' in capsys.readouterr().out

def test_core_import_does_not_pull_s3_deps():           # in packaging_test.py
    code = ("import sys, xraydc; "
            "assert not {'s3fs', 'fsspec', 'boto3', 'botocore'} & set(sys.modules)")
    assert subprocess.run([sys.executable, '-c', code]).returncode == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/prepare_test.py -k login tests/packaging_test.py -v`
Expected: FAIL — `cannot import name 's3_login'`

- [ ] **Step 3: Implement `s3_login`, wire `--s3-login` into `main`, and add `s3fs` to the three extras**

- [ ] **Step 4: Run the whole suite and the corpus suite**

Run: `.venv/Scripts/python -m pytest -v` then `.venv/Scripts/python -m pytest -m corpus -v`
Expected: both PASS. The corpus run matters because Task 1 changed `scans_from_pxs`, and corpus tests are deselected by default.

- [ ] **Step 5: Document the new flags**

Add a short "Importing to S3" section to the `pxs-prepare` page under `doc/pages/`, covering `--s3-login` once, `--s3` per proposal, and the resulting `Proposal(...)` URLs. Do not put any key material in the docs.

- [ ] **Step 6: Commit**

```bash
git add src/pxs_import/prepare.py pyproject.toml tests/ doc/
git commit -m "feat(udkm): --s3-login credential setup, s3fs extra, docs"
```

---

## Verification

After Task 7, confirm against the real proposal before calling this done:

```bash
.venv/Scripts/pxs-prepare notebooks/202601_PtAu --s3 --dry-run
```

Expected: the mapping table, a skip set, and 19 `pm` commands each carrying its own `only=r####`, with nothing written to S3. Then run it for real and report the measured per-scan overhead (spec §6.4) and whether a killed-and-resumed run picks up where it stopped.
