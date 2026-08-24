#!/bin/sh
set -eu

REPOSITORY=${CODEROOK_REPOSITORY:-kyletser/coderook}
VERSION=${CODEROOK_VERSION:-}
INSTALL_ROOT=${CODEROOK_INSTALL_ROOT:-"$HOME/.local/share/coderook"}
BIN_DIR=${CODEROOK_BIN_DIR:-"$HOME/.local/bin"}

if [ -z "$VERSION" ]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPOSITORY/releases/latest" |
    sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' |
    head -n 1)
fi
if [ -z "$VERSION" ]; then
  echo "Could not resolve a CodeRook release version." >&2
  exit 1
fi

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) TARGET=linux-x86_64 ;;
  Linux-aarch64|Linux-arm64) TARGET=linux-arm64 ;;
  Darwin-x86_64) TARGET=macos-x86_64 ;;
  Darwin-arm64) TARGET=macos-arm64 ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

ASSET="coderook-$TARGET.tar.gz"
BASE_URL="https://github.com/$REPOSITORY/releases/download/$VERSION"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

curl -fsSL "$BASE_URL/$ASSET" -o "$TEMP_DIR/$ASSET"
curl -fsSL "$BASE_URL/SHA256SUMS" -o "$TEMP_DIR/SHA256SUMS"
EXPECTED=$(awk -v asset="$ASSET" '$2 == asset || $2 == "*" asset {print $1}' "$TEMP_DIR/SHA256SUMS")
if [ -z "$EXPECTED" ]; then
  echo "Checksum entry for $ASSET is missing." >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL=$(sha256sum "$TEMP_DIR/$ASSET" | awk '{print $1}')
else
  ACTUAL=$(shasum -a 256 "$TEMP_DIR/$ASSET" | awk '{print $1}')
fi
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "Checksum verification failed for $ASSET." >&2
  exit 1
fi

VERSION_DIR="$INSTALL_ROOT/$VERSION"
mkdir -p "$VERSION_DIR" "$BIN_DIR"
tar -xzf "$TEMP_DIR/$ASSET" -C "$VERSION_DIR"
ln -sfn "$VERSION_DIR/coderook-$TARGET/coderook" "$BIN_DIR/coderook"

echo "CodeRook $VERSION installed at $VERSION_DIR"
echo "Run: $BIN_DIR/coderook"
