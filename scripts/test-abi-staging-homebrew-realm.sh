#!/usr/bin/env bash
set -euo pipefail

tap_root="${KANDELO_TAP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
pack="$tap_root/scripts/abi-staging-pack-homebrew-realm.sh"
restore="$tap_root/scripts/abi-staging-restore-homebrew-realm.sh"

test_root="$(mktemp -d "${TMPDIR:-/tmp}/abi-staging-shared-realm-test.XXXXXX")"
trap 'rm -rf -- "$test_root"' EXIT

source_root="$test_root/source"
mkdir -p "$source_root"
git -C "$source_root" init -q -b main
git -C "$source_root" config user.name "Realm Test"
git -C "$source_root" config user.email "realm-test@example.invalid"
mkdir -p "$source_root/host/wasm" "$source_root/tools/bin"
printf 'tracked source\n' >"$source_root/README.md"
git -C "$source_root" add README.md
git -C "$source_root" commit -q -m fixture

printf 'vfs-bytes\n' >"$source_root/host/wasm/rootfs.vfs"
printf '#!/usr/bin/env sh\nexit 0\n' >"$source_root/tools/bin/wasm-fork-instrument"
chmod 0555 "$source_root/tools/bin/wasm-fork-instrument"
ln -s wasm-fork-instrument "$source_root/tools/bin/wasm-local-root-spill"
ln "$source_root/tools/bin/wasm-fork-instrument" \
  "$source_root/tools/bin/wasm-fork-instrument-hardlink"

source_commit="$(git -C "$source_root" rev-parse HEAD)"
source_tree="$(git -C "$source_root" rev-parse 'HEAD^{tree}')"
archive="$test_root/shared-realm.tar.zst"

"$pack" \
  --source-root "$source_root" \
  --expected-commit "$source_commit" \
  --expected-tree "$source_tree" \
  --archive "$archive"

archive_sha256="$(sha256sum "$archive" | awk '{print $1}')"
for consumer in one two; do
  destination="$test_root/$consumer/kandelo-source"
  mkdir -p "$(dirname "$destination")"
  "$restore" \
    --archive "$archive" \
    --expected-archive-sha256 "$archive_sha256" \
    --expected-commit "$source_commit" \
    --expected-tree "$source_tree" \
    --destination-root "$destination"
  test "$(git -C "$destination" rev-parse HEAD)" = "$source_commit"
  test "$(git -C "$destination" rev-parse 'HEAD^{tree}')" = "$source_tree"
  test "$(cat "$destination/host/wasm/rootfs.vfs")" = "vfs-bytes"
  test -x "$destination/tools/bin/wasm-fork-instrument"
  test -L "$destination/tools/bin/wasm-local-root-spill"
  test "$(readlink "$destination/tools/bin/wasm-local-root-spill")" = \
    "wasm-fork-instrument"
  test "$(stat -c '%i' "$destination/tools/bin/wasm-fork-instrument")" = \
    "$(stat -c '%i' "$destination/tools/bin/wasm-fork-instrument-hardlink")"
done

if "$restore" \
  --archive "$archive" \
  --expected-archive-sha256 "$(printf '0%.0s' {1..64})" \
  --expected-commit "$source_commit" \
  --expected-tree "$source_tree" \
  --destination-root "$test_root/bad-digest/kandelo-source"; then
  echo "restore accepted a different archive digest" >&2
  exit 1
fi

mkdir -p "$test_root/bad-commit"
if "$restore" \
  --archive "$archive" \
  --expected-archive-sha256 "$archive_sha256" \
  --expected-commit "$(printf '1%.0s' {1..40})" \
  --expected-tree "$source_tree" \
  --destination-root "$test_root/bad-commit/kandelo-source"; then
  echo "restore accepted a different source commit" >&2
  exit 1
fi

malicious_tar="$test_root/traversal.tar"
malicious_archive="$test_root/traversal.tar.zst"
MALICIOUS_TAR="$malicious_tar" python3 - <<'PY'
import io
import os
import tarfile

with tarfile.open(os.environ["MALICIOUS_TAR"], "w") as archive:
    payload = b"outside\n"
    entry = tarfile.TarInfo("../escaped.txt")
    entry.size = len(payload)
    archive.addfile(entry, io.BytesIO(payload))
PY
zstd -q -f "$malicious_tar" -o "$malicious_archive"
malicious_sha256="$(sha256sum "$malicious_archive" | awk '{print $1}')"
mkdir -p "$test_root/traversal"
if "$restore" \
  --archive "$malicious_archive" \
  --expected-archive-sha256 "$malicious_sha256" \
  --expected-commit "$source_commit" \
  --expected-tree "$source_tree" \
  --destination-root "$test_root/traversal/kandelo-source"; then
  echo "restore accepted an archive traversal" >&2
  exit 1
fi
test ! -e "$test_root/escaped.txt"

printf 'ABI staging shared Homebrew realm: PASS\n'
