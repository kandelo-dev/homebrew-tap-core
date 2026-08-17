#!/usr/bin/env bash
set -euo pipefail

tap_root="${KANDELO_TAP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
key_script="$tap_root/scripts/abi-staging-homebrew-realm-cache-key.sh"

test_root="$(mktemp -d "${TMPDIR:-/tmp}/abi-staging-realm-cache-key-test.XXXXXX")"
trap 'rm -rf -- "$test_root"' EXIT

fixture="$test_root/tap"
mkdir -p "$fixture/scripts" "$fixture/Formula"
cp "$tap_root/scripts/abi-staging-prepare-shared-homebrew-realm.sh" \
  "$fixture/scripts/"
cp "$tap_root/scripts/abi-staging-pack-homebrew-realm.sh" \
  "$fixture/scripts/"
printf 'class Git < Formula; end\n' >"$fixture/Formula/git.rb"

kandelo_commit="$(printf '1%.0s' {1..40})"
output="$test_root/output"

derive_key() {
  : >"$output"
  "$key_script" \
    --tap-root "$fixture" \
    --kandelo-commit "$1" \
    --runner-os "$2" \
    --runner-arch "$3" \
    --github-output "$output" || return
  sed -n 's/^cache_key=//p' "$output"
}

initial_key="$(derive_key "$kandelo_commit" Linux X64)"
[[ "$initial_key" =~ ^abi-staging-homebrew-realm-v2-Linux-X64-${kandelo_commit}-[0-9a-f]{64}$ ]]

printf '# Formula-only change\n' >>"$fixture/Formula/git.rb"
test "$(derive_key "$kandelo_commit" Linux X64)" = "$initial_key"

printf '# Producer change\n' \
  >>"$fixture/scripts/abi-staging-prepare-shared-homebrew-realm.sh"
test "$(derive_key "$kandelo_commit" Linux X64)" != "$initial_key"
cp "$tap_root/scripts/abi-staging-prepare-shared-homebrew-realm.sh" \
  "$fixture/scripts/"

printf '# Packer change\n' \
  >>"$fixture/scripts/abi-staging-pack-homebrew-realm.sh"
test "$(derive_key "$kandelo_commit" Linux X64)" != "$initial_key"
cp "$tap_root/scripts/abi-staging-pack-homebrew-realm.sh" \
  "$fixture/scripts/"

test "$(derive_key "$(printf '2%.0s' {1..40})" Linux X64)" != "$initial_key"
test "$(derive_key "$kandelo_commit" macOS X64)" != "$initial_key"
test "$(derive_key "$kandelo_commit" Linux ARM64)" != "$initial_key"

: >"$output"
"$key_script" \
  --tap-root "$fixture" \
  --kandelo-commit "$kandelo_commit" \
  --runner-os Linux \
  --runner-arch X64 \
  --github-output "$output"
legacy_key="$(sed -n 's/^legacy_cache_key=//p' "$output")"
test "$legacy_key" = \
  "abi-staging-homebrew-realm-Linux-X64-${kandelo_commit}-8697f602d2abd5aefb1d9d278e532a2437a1ac06"

printf '# No longer legacy-compatible\n' \
  >>"$fixture/scripts/abi-staging-prepare-shared-homebrew-realm.sh"
: >"$output"
"$key_script" \
  --tap-root "$fixture" \
  --kandelo-commit "$kandelo_commit" \
  --runner-os Linux \
  --runner-arch X64 \
  --github-output "$output"
test -z "$(sed -n 's/^legacy_cache_key=//p' "$output")"

rm "$fixture/scripts/abi-staging-pack-homebrew-realm.sh"
ln -s "$tap_root/scripts/abi-staging-pack-homebrew-realm.sh" \
  "$fixture/scripts/abi-staging-pack-homebrew-realm.sh"
if derive_key "$kandelo_commit" Linux X64 >/dev/null 2>&1; then
  echo "realm cache key accepted a symlinked producer input" >&2
  exit 1
fi

printf 'ABI staging Homebrew realm cache key: PASS\n'
