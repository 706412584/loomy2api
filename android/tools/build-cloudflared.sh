#!/usr/bin/env bash
# Rebuild the Android cloudflared binary shipped as
# app/src/main/jniLibs/arm64-v8a/libcloudflared.so
#
# Why a custom build: the stock Linux release cannot resolve Cloudflare's SRV
# records on Android (Go's SRV lookup always reads /etc/resolv.conf, which
# Android does not have). Cross-compiling with the NDK and cgo gives us a
# binary whose normal DNS lookups use Android's resolver; the app additionally
# passes explicit --edge addresses so the SRV lookup is never needed.
#
# Requirements: Go 1.27+, Android NDK (set NDK or edit the path below), and
# network access to GitHub / a Go module proxy.
set -euo pipefail

VERSION="${CLOUDFLARED_VERSION:-2026.10.1}"
NDK="${NDK:-/d/android/ndk/28.2.13676358}"
TOOLCHAIN="$NDK/toolchains/llvm/prebuilt/windows-x86_64/bin"
OUT="$(cd "$(dirname "$0")/.." && pwd)/app/src/main/jniLibs/arm64-v8a/libcloudflared.so"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> fetching cloudflared $VERSION"
curl -fsSL "https://github.com/cloudflare/cloudflared/archive/refs/tags/$VERSION.tar.gz" \
  | tar -xz -C "$WORK"
SRC="$WORK/cloudflared-$VERSION"

echo "==> building for android/arm64"
cd "$SRC"
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
export CGO_ENABLED=1 GOOS=android GOARCH=arm64
export CC="$TOOLCHAIN/aarch64-linux-android26-clang"
go build -trimpath \
  -ldflags "-s -w -X 'main.Version=$VERSION-android' -X 'main.BuildTime=$(date -u +%F)'" \
  -o "$OUT" ./cmd/cloudflared

mkdir -p "$(dirname "$OUT")"
echo "==> wrote $OUT ($(du -h "$OUT" | cut -f1))"
