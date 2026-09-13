#!/usr/bin/env bash
# Re-copies the pipeline files this Space vendors from the device repo, so the
# browser demo provably runs the board's code and not a fork of it.
#
#   ./scripts/sync_from_board.sh            # copy, overwriting local copies
#   ./scripts/sync_from_board.sh --check    # diff only; non-zero exit on drift
#
# Point BOARD_REPO at a checkout of Hackestiu/cultura-viva-uno-q.
set -euo pipefail

BOARD_REPO="${BOARD_REPO:-../../cultura-viva-uno-q}"
SRC="$BOARD_REPO/python"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FILES=(
  core/__init__.py
  core/vision_module.py
  core/model_module.py
  hw/__init__.py
  hw/microphone_module.py
  hw/audio_playback_module.py
  logging_setup.py
  models/knowledge/element_sheets.json
  models/knowledge/knowledge_base.json
  models/vision/park_guell/labels.json
  models/vision/sagrada_familia/labels.json
)

if [[ ! -d "$SRC" ]]; then
  echo "Board repo not found at $BOARD_REPO — set BOARD_REPO=/path/to/cultura-viva-uno-q" >&2
  exit 2
fi

if [[ "${1:-}" == "--check" ]]; then
  drift=0
  for f in "${FILES[@]}"; do
    if ! diff -q "$SRC/$f" "$HERE/$f" >/dev/null 2>&1; then
      echo "DRIFT  $f"
      diff -u "$SRC/$f" "$HERE/$f" || true
      drift=1
    fi
  done
  [[ $drift -eq 0 ]] && echo "In sync with $BOARD_REPO"
  exit $drift
fi

for f in "${FILES[@]}"; do
  mkdir -p "$HERE/$(dirname "$f")"
  cp "$SRC/$f" "$HERE/$f"
  echo "synced  $f"
done
echo
echo "config.py, bootstrap.py and app.py are Space-specific and are NOT synced."
