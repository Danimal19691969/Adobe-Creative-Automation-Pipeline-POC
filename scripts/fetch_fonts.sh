#!/usr/bin/env bash
# Download Montserrat-Bold and OpenSans-Regular from the canonical Google Fonts
# repos on GitHub (SIL Open Font License — safe to vendor).
# Run from repo root: bash scripts/fetch_fonts.sh
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p fonts

if [ ! -f fonts/Montserrat-Bold.ttf ]; then
  echo "Downloading Montserrat-Bold.ttf..."
  curl -fsSL -o fonts/Montserrat-Bold.ttf \
    "https://raw.githubusercontent.com/googlefonts/Montserrat/master/fonts/ttf/Montserrat-Bold.ttf"
fi

if [ ! -f fonts/OpenSans-Regular.ttf ]; then
  echo "Downloading OpenSans-Regular.ttf..."
  curl -fsSL -o fonts/OpenSans-Regular.ttf \
    "https://raw.githubusercontent.com/googlefonts/opensans/main/fonts/ttf/OpenSans-Regular.ttf"
fi

echo "Fonts ready:"
ls -1 fonts/
