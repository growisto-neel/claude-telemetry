#!/usr/bin/env bash
#
# Cross-compile the hook for every platform the team runs, into bin/.
#
# The binaries are committed to this repository on purpose. `/plugin marketplace
# add` clones the repo and runs what it finds; there is no build or install step
# it could trigger, so anything not already in the tree does not exist as far as
# a colleague's laptop is concerned. Committing ~15 MB of binaries is the price
# of the plugin needing no toolchain, no interpreter, and no network beyond the
# clone itself.
#
# Nobody should have to run this by hand. .github/workflows/build.yml does it on
# every push that touches the Go source and commits the result. Run it locally
# only to test a change before pushing.
#
# Usage:
#   ./build.sh            # build every target
#   ./build.sh darwin-arm64   # build one, by its bin/ suffix

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v go >/dev/null 2>&1; then
    echo "go is not on PATH. Install Go 1.21+ (brew install go, apt install golang-go)," >&2
    echo "or just push and let .github/workflows/build.yml do it." >&2
    exit 1
fi

# GOOS/GOARCH pairs, and nothing else. The launcher derives the same names from
# uname, so adding a platform here is the only change needed to support it.
#
# Four targets, not six. linux-arm64 is omitted because nobody on the team runs
# Linux on ARM -- no Raspberry Pi, no ARM server -- and an unused 2.5 MB binary
# in every clone is not free. windows-arm64 is omitted because Windows on ARM
# runs x64 binaries under built-in emulation, so windows-amd64 covers it.
#
# If either assumption stops holding, the machine is not silently missed: the
# launcher writes a NO_BINARY marker naming the target it wanted, and
# /growisto-telemetry reports it. Adding the line back here is the whole fix.
TARGETS=(
    linux-amd64
    darwin-amd64
    darwin-arm64
    windows-amd64
)

if [ "$#" -gt 0 ]; then
    TARGETS=("$@")
fi

mkdir -p bin

# -s -w strip the symbol table and DWARF debug info, which roughly halves each
# binary. Nobody is going to attach a debugger to a copy of this that is running
# on someone else's laptop; the log is the debugging interface.
#
# CGO_ENABLED=0 is what makes these genuinely static. With cgo on, the darwin
# and linux builds link against the host libc and can fail to start on an older
# machine than the one that built them -- which is exactly the silent, machine-
# specific failure this rewrite exists to remove.
LDFLAGS="-s -w"

for target in "${TARGETS[@]}"; do
    goos="${target%%-*}"
    goarch="${target##*-}"
    out="bin/growisto-hook-$target"
    [ "$goos" = "windows" ] && out="$out.exe"

    printf 'building %-24s' "$target"
    CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
        go build -trimpath -ldflags "$LDFLAGS" -o "$out" ./cmd/growisto-hook
    chmod +x "$out"
    printf '%s\n' "$(du -h "$out" | cut -f1)"
done

echo
echo "Built $(ls -1 bin/growisto-hook-* | wc -l | tr -d ' ') binaries."
echo
echo "git does not record the executable bit the way you might expect on every"
echo "platform, so mark them explicitly before committing:"
echo
echo "  git add bin/ && git update-index --chmod=+x bin/growisto-hook-*"
echo
echo "The launcher also chmods them at runtime if that is missed, but a correct"
echo "mode in the tree is one less thing to go wrong quietly."
