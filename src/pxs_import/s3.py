'''S3 target support for the UDKM importer: credentials, objects, manifest.

Everything `pxs-prepare --s3` needs to talk to an S3 bucket lives here, so
that :mod:`pxs_import.prepare` keeps its orchestration role and stays
readable.

Two layering rules hold for this module, both load-bearing:

- It never imports ``nx5d``. The coupling to nx5d is one-directional and
  resolved by string, through the ``pm`` console script -- see the project's
  ``CLAUDE.md``.
- ``s3fs``/``fsspec`` are imported *inside* the functions that need them,
  never at module scope, so that a plain ``import pxs_import`` does not
  drag an optional dependency (and a botocore import chain) into
  ``sys.modules``. A test in ``tests/packaging_test.py`` pins that.

The credential helpers write a standard AWS profile (``~/.aws/credentials``
plus ``~/.aws/config``) rather than exporting environment variables, because
botocore resolves endpoint and region from a profile as well as from the
environment. That is what lets a bare ``nx5d.Proposal('s3://...')`` in a
notebook work with no preamble.
'''

import configparser
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

S3_BUCKET_DEFAULT = 'pxsrepo'
S3_ENDPOINT_DEFAULT = 'https://s3.rz.uni-potsdam.de'
S3_REGION_DEFAULT = 'US'


def aws_paths(home: Path | None = None) -> tuple[Path, Path]:
    '''Locate the AWS credentials and config files.

    Args:
        home: Home directory to resolve against. Defaults to
            :meth:`pathlib.Path.home`; tests pass a ``tmp_path``.

    Returns:
        ``(credentials_path, config_path)``, both under ``<home>/.aws/``.
        Neither is required to exist.
    '''
    aws = (Path.home() if home is None else Path(home)) / '.aws'
    return aws / 'credentials', aws / 'config'


def _read_ini(path: Path) -> configparser.ConfigParser:
    '''Parse one INI file, tolerating absence but not corruption.

    Args:
        path: File to read.

    Returns:
        The parsed parser; empty if ``path`` does not exist.

    Raises:
        ValueError: ``path`` exists but cannot be parsed, naming it.
    '''
    parser = configparser.ConfigParser()
    if not path.exists():
        return parser
    try:
        parser.read(path, encoding='utf-8')
    except configparser.Error as exc:
        raise ValueError(f'{path}: cannot parse, leaving it untouched: {exc}') from exc
    return parser


def _config_section(profile: str) -> str:
    '''Section name a profile takes in ``~/.aws/config``.

    ``default`` is spelled bare there; every other profile is prefixed with
    ``profile``. ``~/.aws/credentials`` uses the bare name in both cases.

    Args:
        profile: Profile name.

    Returns:
        The section name to use in the config file.
    '''
    return 'default' if profile == 'default' else f'profile {profile}'


def profile_configured(profile: str = 'default', *, home: Path | None = None) -> bool:
    '''Report whether a usable credential profile is already on disk.

    Used as a precondition for ``--s3`` so that a first run without setup
    fails with a message naming ``--s3-login`` rather than with a raw
    botocore ``NoCredentialsError``.

    Args:
        profile: Profile name to look for.
        home: Home directory, as in :func:`aws_paths`.

    Returns:
        True if the credentials file has that profile with a non-empty
        ``aws_access_key_id``. A missing or unparseable credentials file
        reads as False rather than raising.
    '''
    credentials, _ = aws_paths(home)
    try:
        parser = _read_ini(credentials)
    except ValueError:
        return False
    return bool(parser.has_section(profile)
                and parser.get(profile, 'aws_access_key_id', fallback=''))


def credentials_available(profile: str | None = None, *, home: Path | None = None) -> bool:
    '''Report whether botocore will find *some* credential for an S3 call.

    Broader than :func:`profile_configured`, and the right check for the
    ``--s3`` precondition: botocore resolves environment variables first and
    only then ``~/.aws/credentials``, keyed by ``AWS_PROFILE``. Checking the
    file alone would refuse two setups that work -- keys exported into the
    environment, and an ``AWS_PROFILE`` naming a profile other than
    ``default`` -- with a message asserting something false about the
    machine, and would point at ``--s3-login``, which fixes neither.

    Args:
        profile: Explicitly requested profile (``--s3-profile``), or
            ``None`` to follow the same fallbacks botocore would.
        home: Home directory, as in :func:`aws_paths`.

    Returns:
        True if ``AWS_ACCESS_KEY_ID`` is set, or the effective profile has
        credentials on disk.
    '''
    if os.environ.get('AWS_ACCESS_KEY_ID'):
        return True
    return profile_configured(profile or os.environ.get('AWS_PROFILE') or 'default',
                               home=home)


def _harden(path: Path) -> None:
    '''Best-effort owner-only permissions on a freshly written secret.

    Args:
        path: File to restrict. A failure is logged at WARNING and
            swallowed -- POSIX mode bits are only partially meaningful on
            Windows, which is this importer's primary platform, and refusing
            to store credentials there would be worse than storing them with
            default ACLs.
    '''
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning('%s: could not restrict permissions: %s', path, exc)


def write_profile(key_id: str, secret: str, *,
                   endpoint: str = S3_ENDPOINT_DEFAULT,
                   region: str = S3_REGION_DEFAULT,
                   profile: str = 'default',
                   home: Path | None = None) -> tuple[Path, Path]:
    '''Write one AWS profile, preserving every other profile in both files.

    The key and secret go to ``~/.aws/credentials``; the endpoint and region
    go to ``~/.aws/config``, where botocore (>= 1.31) reads ``endpoint_url``
    per profile. Both files are parsed *before* either is written, so a
    corrupt file aborts the whole operation rather than leaving one of the
    two rewritten.

    Args:
        key_id: ``aws_access_key_id`` value.
        secret: ``aws_secret_access_key`` value. Never logged.
        endpoint: ``endpoint_url`` for the config profile.
        region: ``region`` for the config profile.
        profile: Profile name. ``'default'`` is what lets a bare
            ``Proposal('s3://...')`` work with no ``AWS_PROFILE`` set.
        home: Home directory, as in :func:`aws_paths`.

    Returns:
        ``(credentials_path, config_path)``, the two files written.

    Raises:
        ValueError: Either file exists but cannot be parsed. Nothing is
            written in that case.
    '''
    credentials, config = aws_paths(home)
    cred_parser = _read_ini(credentials)
    conf_parser = _read_ini(config)

    if not cred_parser.has_section(profile):
        cred_parser.add_section(profile)
    cred_parser.set(profile, 'aws_access_key_id', key_id)
    cred_parser.set(profile, 'aws_secret_access_key', secret)

    section = _config_section(profile)
    if not conf_parser.has_section(section):
        conf_parser.add_section(section)
    conf_parser.set(section, 'endpoint_url', endpoint)
    conf_parser.set(section, 'region', region)

    credentials.parent.mkdir(parents=True, exist_ok=True)
    for path, parser in ((credentials, cred_parser), (config, conf_parser)):
        with open(path, 'w', encoding='utf-8') as handle:
            parser.write(handle)
    _harden(credentials)

    return credentials, config


MANIFEST_NAME = '.udkm-import.json'
MANIFEST_VERSION = 1


def filesystem(profile: str | None = None):
    '''Open an S3 filesystem through fsspec.

    ``s3fs`` is imported here rather than at module scope, so that a plain
    ``import pxs_import`` stays free of it (see the module docstring).

    Args:
        profile: AWS profile name, or ``None`` to let botocore's standard
            chain pick one (which is what makes the ``default`` profile
            written by :func:`write_profile` work with no ``AWS_PROFILE``).

    Returns:
        An ``s3fs.S3FileSystem``. Endpoint and region come from the
        profile's ``~/.aws/config`` section, not from arguments.
    '''
    import s3fs
    return s3fs.S3FileSystem(profile=profile) if profile else s3fs.S3FileSystem()


def _key(url: str) -> str:
    '''Strip the ``s3://`` scheme, leaving ``bucket/key``.

    Args:
        url: URL or bare key.

    Returns:
        The key form, which is what fsspec listings return.
    '''
    return url[len('s3://'):] if url.startswith('s3://') else url


def manifest_url(bucket: str, proposal: str) -> str:
    '''Build the URL of a proposal's import manifest.

    Args:
        bucket: Bucket name.
        proposal: Proposal folder name.

    Returns:
        ``s3://<bucket>/<proposal>/.udkm-import.json``.
    '''
    return f's3://{bucket}/{proposal}/{MANIFEST_NAME}'


def read_manifest(fs, url: str) -> dict[str, dict]:
    '''Read the per-scan completion record, degrading to empty on any doubt.

    The manifest is what distinguishes a scan that finished uploading from
    one whose prefix merely exists because a run died mid-write. Anything
    unreadable therefore reads as "nothing is known complete", which costs
    a re-upload but can never mark partial data complete.

    Args:
        fs: Filesystem, as returned by :func:`filesystem`.
        url: Manifest URL, from :func:`manifest_url`.

    Returns:
        The ``scans`` mapping, ``{scan_key: {'source': str, 'imported':
        str}}``. ``{}`` if the object is absent (silently), or if it is
        unparseable or carries a ``version`` other than
        :data:`MANIFEST_VERSION` (with a warning naming the URL).
    '''
    import json
    try:
        if not fs.exists(url):
            return {}
        raw = fs.cat_file(url)
    except (OSError, ValueError) as exc:
        logger.warning('%s: cannot read import manifest, assuming empty: %s', url, exc)
        return {}

    try:
        document = json.loads(raw)
    except ValueError as exc:
        logger.warning('%s: unparseable import manifest, assuming empty: %s', url, exc)
        return {}

    version = document.get('version') if isinstance(document, dict) else None
    if version != MANIFEST_VERSION:
        logger.warning('%s: import manifest version %r is not %d, assuming empty',
                       url, version, MANIFEST_VERSION)
        return {}

    scans = document.get('scans')
    if not isinstance(scans, dict):
        logger.warning('%s: import manifest has no "scans" mapping, assuming empty', url)
        return {}
    return scans


def write_manifest(fs, url: str, scans: dict[str, dict]) -> None:
    '''Write the per-scan completion record.

    Called after every successfully imported scan, so that an interrupted
    run loses at most the scan in flight.

    Args:
        fs: Filesystem, as returned by :func:`filesystem`.
        url: Manifest URL, from :func:`manifest_url`.
        scans: The mapping to store under ``scans``.
    '''
    import json
    body = json.dumps({'version': MANIFEST_VERSION, 'scans': scans}, indent=2)
    fs.pipe_file(url, body.encode('utf-8'))


def prefix_exists(fs, url: str) -> bool:
    '''Report whether any object lives at or below a prefix.

    Args:
        fs: Filesystem, as returned by :func:`filesystem`.
        url: Prefix URL.

    Returns:
        True if the prefix holds at least one object.
    '''
    try:
        return bool(fs.find(url))
    except FileNotFoundError:
        return False


def delete_prefix(fs, url: str) -> None:
    '''Remove every object at or below a prefix.

    Used before re-importing a scan. Overwriting in place is not enough:
    nx5d's ``write_to_astor`` skips datasets that already exist unless
    ``overwrite_data`` is set, and even then cannot drop an array whose
    shape changed between runs.

    Args:
        fs: Filesystem, as returned by :func:`filesystem`.
        url: Prefix URL. A prefix with nothing under it is a no-op.
    '''
    if not prefix_exists(fs, url):
        return
    fs.rm(url, recursive=True)


def store_has_arrays(fs, url: str) -> bool:
    '''Report whether a Zarr store holds at least one array.

    A cheap guard against ``pm`` exiting 0 having written nothing -- which
    it does when its scan glob matches nothing. Looks for a ``zarr.json``
    *below* the store root; the root's own ``zarr.json`` only says a group
    was created. ``zarr`` is deliberately not imported.

    Args:
        fs: Filesystem, as returned by :func:`filesystem`.
        url: Store URL.

    Returns:
        True if any nested ``zarr.json`` is present.
    '''
    root = _key(url).rstrip('/')
    for found in fs.find(url):
        relative = _key(found)[len(root):].lstrip('/')
        if relative.endswith('zarr.json') and '/' in relative:
            return True
    return False


def upload_dir(fs, local: Path, url: str) -> int:
    '''Upload every file under a local directory to a prefix.

    Used for the spice folder, whose records are small JSON files, so each
    one is sent whole rather than streamed.

    Args:
        fs: Filesystem, as returned by :func:`filesystem`.
        local: Local directory to upload. Its subdirectory structure is
            preserved relative to ``url``.
        url: Destination prefix.

    Returns:
        The number of files uploaded.
    '''
    local = Path(local)
    files = sorted(path for path in local.rglob('*') if path.is_file())
    base = url.rstrip('/')
    for path in files:
        fs.pipe_file(f'{base}/{path.relative_to(local).as_posix()}', path.read_bytes())
    return len(files)
