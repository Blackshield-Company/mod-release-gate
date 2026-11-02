#!/usr/bin/env bash
# Find or fetch mod-conflict-graph for the conflicts check and print "path=DIR" (empty when the
# package is already importable or unavailable). Never fails the job: the gate degrades instead.
#
# Env: GATE_PYTHON (python3), GATE_SKIP, MCG_PATH (local checkout), MCG_SOURCE (git URL, may be empty),
#      RUNNER_TEMP (scratch dir, default /tmp).
set -uo pipefail

py="${GATE_PYTHON:-python3}"
note() { echo "::notice title=mod-release-gate::$*" >&2; }

case ",${GATE_SKIP:-}," in
  *,conflicts,*|*,\ conflicts,*) echo "path="; exit 0 ;;
esac

if [ -n "${MCG_PATH:-}" ]; then
  if [ -d "$MCG_PATH/modconflict" ]; then
    echo "path=$(cd "$MCG_PATH" && pwd)"
    exit 0
  fi
  note "mod-conflict-graph-path '$MCG_PATH' has no modconflict/ package; trying other sources"
fi

if "$py" -c 'import modconflict.modcheck' >/dev/null 2>&1; then
  echo "path="
  exit 0
fi

if [ -z "${MCG_SOURCE:-}" ]; then
  note "mod-conflict-graph not available and no source configured; conflict check will be skipped"
  echo "path="
  exit 0
fi

dest="${RUNNER_TEMP:-/tmp}/mod-conflict-graph"
rm -rf "$dest"
mkdir -p "$dest"

# 1) pip into a private target dir (no site-packages changes, no dependencies).
if timeout 180 nice "$py" -m pip install --quiet --disable-pip-version-check --no-deps \
     --target "$dest/site" "git+$MCG_SOURCE" >/dev/null 2>&1 && [ -d "$dest/site/modconflict" ]; then
  echo "path=$dest/site"
  exit 0
fi

# 2) Shallow clone; the package runs from a source checkout without installation.
if command -v git >/dev/null 2>&1 && \
   timeout 120 nice git clone --quiet --depth 1 "$MCG_SOURCE" "$dest/src" >/dev/null 2>&1 && \
   [ -d "$dest/src/modconflict" ]; then
  echo "path=$dest/src"
  exit 0
fi

note "could not install mod-conflict-graph from $MCG_SOURCE; conflict check will be skipped"
echo "path="
