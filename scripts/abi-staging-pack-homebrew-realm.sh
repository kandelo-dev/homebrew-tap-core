#!/usr/bin/env bash
set -euo pipefail

source_root=""
expected_commit=""
expected_tree=""
archive=""

while (($#)); do
  case "$1" in
    --source-root)
      source_root="${2:-}"
      shift 2
      ;;
    --expected-commit)
      expected_commit="${2:-}"
      shift 2
      ;;
    --expected-tree)
      expected_tree="${2:-}"
      shift 2
      ;;
    --archive)
      archive="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown shared-realm pack argument: $1" >&2
      exit 2
      ;;
  esac
done

[[ "$source_root" = /* && "$archive" = /* ]]
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$expected_tree" =~ ^[0-9a-f]{40}$ ]]
test -d "$source_root" && test ! -L "$source_root"
test ! -e "$archive" && test ! -L "$archive"

source_root="$(cd "$source_root" && pwd -P)"
source_parent="$(dirname "$source_root")"
source_name="$(basename "$source_root")"
[[ "$source_name" =~ ^[A-Za-z0-9._-]+$ ]]
archive_parent="$(dirname "$archive")"
test -d "$archive_parent" && test ! -L "$archive_parent"
archive_parent="$(cd "$archive_parent" && pwd -P)"
archive="$archive_parent/$(basename "$archive")"

test "$(git -C "$source_root" rev-parse HEAD)" = "$expected_commit"
test "$(git -C "$source_root" rev-parse 'HEAD^{tree}')" = "$expected_tree"
git -C "$source_root" diff --quiet --ignore-submodules=all --
git -C "$source_root" diff --cached --quiet --ignore-submodules=all --
if git -C "$source_root" config --local --get-regexp \
  '^http\..*\.extraheader$' >/dev/null 2>&1; then
  echo "shared realm source retains a credential header" >&2
  exit 2
fi
while IFS= read -r remote_url; do
  case "$remote_url" in
    *://*@*)
      echo "shared realm source retains a credentialed remote" >&2
      exit 2
      ;;
  esac
done < <(git -C "$source_root" remote get-url --all origin 2>/dev/null || true)

manifest_root="$(mktemp -d "${TMPDIR:-/tmp}/abi-staging-realm-manifest.XXXXXX")"
archive_tmp="$archive.tmp.$$"
trap 'rm -rf -- "$manifest_root"; rm -f -- "$archive_tmp"' EXIT

EXPECTED_COMMIT="$expected_commit" EXPECTED_TREE="$expected_tree" \
  python3 - <<'PY' >"$manifest_root/realm-manifest.json"
import json
import os

document = {
    "kind": "kandelo-abi-staging-shared-homebrew-realm",
    "schema": 1,
    "source": {
        "commit": os.environ["EXPECTED_COMMIT"],
        "tree": os.environ["EXPECTED_TREE"],
    },
}
print(json.dumps(document, sort_keys=True, separators=(",", ":")))
PY

tar \
  --sort=name \
  --mtime='@0' \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --transform="s|^${source_name}$|kandelo-source|" \
  --transform="s|^${source_name}/|kandelo-source/|" \
  -C "$source_parent" \
  -cf - "$source_name" \
  -C "$manifest_root" realm-manifest.json |
  zstd -T0 -3 -q -o "$archive_tmp"

test -f "$archive_tmp" && test ! -L "$archive_tmp"
archive_bytes="$(stat -c '%s' "$archive_tmp")"
[[ "$archive_bytes" =~ ^[0-9]+$ ]]
((archive_bytes > 0 && archive_bytes <= 8 * 1024 * 1024 * 1024))
chmod 0444 "$archive_tmp"
mv "$archive_tmp" "$archive"
