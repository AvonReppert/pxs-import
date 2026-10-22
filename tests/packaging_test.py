'''Import hygiene.

The optional dependency is reached through ``pxs_import.s3``, whose
``s3fs``/``fsspec`` imports live inside the functions that need them. These
tests pin that: a careless move of an import to module scope makes
``pxs-prepare`` fail on a machine that installed the package without the
``[s3]`` extra, and nothing else would notice.
'''

import importlib
import subprocess
import sys

HEAVY = "{'s3fs', 'fsspec', 'boto3', 'botocore'}"


def _import_leaves_out_heavy_deps(statement):
    code = (f'import sys; {statement}; '
            f"assert not {HEAVY} & set(sys.modules), "
            f"sorted({HEAVY} & set(sys.modules))")
    return subprocess.run([sys.executable, '-c', code]).returncode


def test_core_import_does_not_pull_s3_deps():
    assert _import_leaves_out_heavy_deps('import pxs_import') == 0


def test_prepare_import_does_not_pull_s3_deps():
    # prepare.py imports pxs_import.s3 at module scope on purpose; s3 itself
    # must still keep s3fs out until a function asks for it.
    assert _import_leaves_out_heavy_deps('import pxs_import.prepare') == 0


def test_package_importable():
    assert importlib.import_module('pxs_import') is not None
