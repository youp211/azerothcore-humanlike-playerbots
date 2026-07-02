#!/usr/bin/env bash
# Build AzerothCore (Playerbot fork) + mod-playerbots + mod-ollama-chat.
# Idempotent: first run clones everything, later runs just rebuild.
# Usage: ./build.sh [--update]   (--update: git pull core + modules first)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="$ROOT/azerothcore-wotlk"
PREFIX="$ROOT/server"
JOBS=6   # not 8: PCH-heavy compile can OOM 32 GB at -j8

if [ ! -d "$CORE" ]; then
    git clone https://github.com/liyunfan1223/azerothcore-wotlk.git --branch=Playerbot "$CORE"
fi
[ -d "$CORE/modules/mod-playerbots" ] || \
    git clone https://github.com/liyunfan1223/mod-playerbots.git --branch=master "$CORE/modules/mod-playerbots"
[ -d "$CORE/modules/mod-ollama-chat" ] || \
    git clone https://github.com/DustinHendrickson/mod-ollama-chat.git "$CORE/modules/mod-ollama-chat"

if [ "${1:-}" = "--update" ]; then
    git -C "$CORE" pull
    git -C "$CORE/modules/mod-playerbots" pull
    git -C "$CORE/modules/mod-ollama-chat" pull
fi

mkdir -p "$CORE/build"
cd "$CORE/build"
cmake .. \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_C_COMPILER=/usr/bin/clang \
    -DCMAKE_CXX_COMPILER=/usr/bin/clang++ \
    -DCMAKE_BUILD_TYPE=Release \
    -DWITH_WARNINGS=0 \
    -DTOOLS_BUILD=none \
    -DSCRIPTS=static \
    -DMODULES=static

make -j "$JOBS"
make install

echo "Build complete. Binaries in $PREFIX/bin, configs in $PREFIX/etc."
