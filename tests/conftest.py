"""Shared pytest fixtures.

``FakeFS``, the dict-backed fsspec stand-in the ``--s3`` tests share
(``s3_test.py``, ``prepare_test.py``), lives here rather than being
duplicated per file. The ``sys.path`` insert below is the other thing shared
across modules: it is what lets ``tools_test.py`` import ``tools.thin_corpus``,
a developer script outside the installed package.
"""

import sys
from pathlib import Path

import pytest

# `tools/` holds developer scripts that are deliberately not part of the
# installed package -- `tools_test.py` imports the corpus thinner from there.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeFS:
    """Dict-backed stand-in for the handful of fsspec methods we call."""

    def __init__(self):
        self.store = {}

    @staticmethod
    def _key(path):
        return path[5:] if path.startswith('s3://') else path

    def pipe_file(self, path, value):
        self.store[self._key(path)] = bytes(value)

    pipe = pipe_file

    def cat_file(self, path):
        try:
            return self.store[self._key(path)]
        except KeyError:
            raise FileNotFoundError(path) from None

    def exists(self, path):
        key = self._key(path)
        return key in self.store or any(k.startswith(key + '/') for k in self.store)

    def find(self, path):
        key = self._key(path).rstrip('/')
        return sorted(k for k in self.store if k == key or k.startswith(key + '/'))

    def rm(self, path, recursive=False):
        for key in self.find(path):
            del self.store[key]


@pytest.fixture
def fake_fs():
    return FakeFS()
