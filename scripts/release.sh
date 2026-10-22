#!/bin/bash

set -euo pipefail

VERSION=${1:-}
PYPROJECT_FILE="pyproject.toml"
VERSION_FILE="src/pxs_import/_version.py"

# Release checklist (maintainer)
# 1) Merge all desired changes into master.
# 2) Ensure CI on master is green.
# 3) Ensure PYPI_RELEASE_TOKEN is valid in CI/CD variables.
# 4) Ensure local master is up to date and clean.
# 5) Run: ./scripts/release.sh X.Y.Z
# 6) Confirm with "y" when prompted.
# 7) Verify release jobs and published artifacts.

# --- 1. Validate argument ---
if [ -z "$VERSION" ]; then
  echo "Usage: ./scripts/release.sh 0.2.3"
  exit 1
fi

# --- 2. Validate SemVer (X.Y.Z) ---
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Error: Version must be in format X.Y.Z (e.g. 0.2.3)"
  exit 1
fi

# --- 3. Validate Git repository ---
if ! git rev-parse --git-dir > /dev/null 2>&1; then
  echo "Error: Not a git repository"
  exit 1
fi

# --- 4. Validate branch ---
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "master" ]; then
  echo "Error: You must be on 'master' branch (current: $CURRENT_BRANCH)"
  exit 1
fi

# --- 5. Ensure clean working tree ---
if [ -n "$(git status --porcelain)" ]; then
  echo "Error: Working directory is not clean"
  git status
  exit 1
fi

# --- 6. Validate remote sync status ---
git fetch origin

LOCAL=$(git rev-parse master)
REMOTE=$(git rev-parse origin/master)

if [ "$LOCAL" != "$REMOTE" ]; then
  echo "Error: Local master is not in sync with origin/master"
  echo "Run: git pull origin master"
  exit 1
fi

# --- 7. Ensure tag does not already exist ---
if git rev-parse "v$VERSION" >/dev/null 2>&1; then
  echo "Error: Tag v$VERSION already exists"
  exit 1
fi

# --- 8. Validate setuptools_scm write_to target ---
if [ -f "$PYPROJECT_FILE" ]; then
  SCM_WRITE_TO=$(grep -E '^[[:space:]]*write_to[[:space:]]*=[[:space:]]*"[^"]+"' "$PYPROJECT_FILE" | sed -E 's/.*"([^"]+)".*/\1/' | head -n1 || true)
  if [ -n "$SCM_WRITE_TO" ] && [ "$SCM_WRITE_TO" != "$VERSION_FILE" ]; then
    echo "Error: release.sh expects $VERSION_FILE but pyproject.toml write_to is $SCM_WRITE_TO"
    echo "Please align VERSION_FILE in release.sh or write_to in pyproject.toml"
    exit 1
  fi
fi

# --- 9. Validate version in _version.py (if present) ---
if [ -f "$VERSION_FILE" ]; then
  CODE_VERSION=$(grep -Eo "__version__[[:space:]]*=[[:space:]]*(version[[:space:]]*=[[:space:]]*)?['\"][^'\"]+['\"]" "$VERSION_FILE" | head -n1 | sed -E "s/.*['\"]([^'\"]+)['\"].*/\1/")

  if [ -z "${CODE_VERSION:-}" ]; then
    echo "Error: Could not parse __version__ from $VERSION_FILE"
    exit 1
  fi

  if [ "$CODE_VERSION" != "$VERSION" ]; then
    echo "Error: Version mismatch"
    echo "Code version: $CODE_VERSION"
    echo "Release version: $VERSION"
    exit 1
  fi
else
  echo "Info: $VERSION_FILE not present (normal before build with setuptools_scm)."
  echo "Info: Proceeding; release version will be defined by tag v$VERSION."
fi

# --- 10. Confirmation ---
echo "About to release v$VERSION from master"
read -r -p "Continue? (y/n) " CONFIRM
if [ "$CONFIRM" != "y" ]; then
  echo "Aborted"
  exit 1
fi

# --- 11. Create tag ---
git tag -a "v$VERSION" -m "Release v$VERSION"

# --- 12. Push ---
git push origin master
git push origin "v$VERSION"

echo "Release v$VERSION successfully created."
