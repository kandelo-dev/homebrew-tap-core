#!/usr/bin/env bash
set -euo pipefail

archive=""
expected_archive_sha256=""
expected_commit=""
expected_tree=""
destination_root=""

while (($#)); do
  case "$1" in
    --archive)
      archive="${2:-}"
      shift 2
      ;;
    --expected-archive-sha256)
      expected_archive_sha256="${2:-}"
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
    --destination-root)
      destination_root="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown shared-realm restore argument: $1" >&2
      exit 2
      ;;
  esac
done

[[ "$archive" = /* && "$destination_root" = /* ]]
[[ "$expected_archive_sha256" =~ ^[0-9a-f]{64}$ ]]
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$expected_tree" =~ ^[0-9a-f]{40}$ ]]
test -f "$archive" && test ! -L "$archive"
test ! -e "$destination_root" && test ! -L "$destination_root"
test "$(basename "$destination_root")" = "kandelo-source"

archive_bytes="$(stat -c '%s' "$archive")"
[[ "$archive_bytes" =~ ^[0-9]+$ ]]
((archive_bytes > 0 && archive_bytes <= 8 * 1024 * 1024 * 1024))
actual_archive_sha256="$(sha256sum "$archive" | awk '{print $1}')"
test "$actual_archive_sha256" = "$expected_archive_sha256"

destination_parent="$(dirname "$destination_root")"
test -d "$destination_parent" && test ! -L "$destination_parent"
destination_parent="$(cd "$destination_parent" && pwd -P)"
destination_root="$destination_parent/kandelo-source"
stage="$(mktemp -d "$destination_parent/.abi-staging-realm.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT

python3 - "$archive" <<'PY'
import posixpath
import subprocess
import sys
import tarfile

members = 0
regular_bytes = 0
manifest_count = 0
process = subprocess.Popen(
    ["zstd", "-q", "-d", "-c", sys.argv[1]],
    stdout=subprocess.PIPE,
)
assert process.stdout is not None
with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
    for member in archive:
        members += 1
        if members > 2_000_000:
            raise SystemExit("shared realm archive has too many entries")
        name = member.name
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or posixpath.normpath(name) != name
            or name == ".."
            or name.startswith("../")
            or "/../" in name
        ):
            raise SystemExit(f"shared realm archive path is invalid: {name!r}")
        if name == "realm-manifest.json":
            manifest_count += 1
            if not member.isfile() or member.size > 16 * 1024:
                raise SystemExit("shared realm manifest is not one bounded regular file")
        elif name != "kandelo-source" and not name.startswith("kandelo-source/"):
            raise SystemExit(f"shared realm archive leaves its root: {name!r}")
        if not (
            member.isfile()
            or member.isdir()
            or member.issym()
            or member.islnk()
        ):
            raise SystemExit(f"shared realm archive has a special entry: {name!r}")
        if member.isfile():
            regular_bytes += member.size
            if regular_bytes > 16 * 1024 * 1024 * 1024:
                raise SystemExit("shared realm archive expands beyond its bound")
        if member.issym():
            normalized_link = posixpath.normpath(member.linkname)
            if (
                member.linkname == normalized_link
                and normalized_link.startswith("/nix/store/")
                and "\\" not in normalized_link
            ):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
            if target != "kandelo-source" and not target.startswith("kandelo-source/"):
                raise SystemExit(f"shared realm symlink leaves its root: {name!r}")
        if member.islnk():
            target = posixpath.normpath(member.linkname)
            if target != "kandelo-source" and not target.startswith("kandelo-source/"):
                raise SystemExit(f"shared realm hardlink leaves its root: {name!r}")
if manifest_count != 1:
    raise SystemExit("shared realm archive must contain one manifest")
process.stdout.close()
if process.wait() != 0:
    raise SystemExit("shared realm archive decompression failed")
PY

zstd -q -d -c "$archive" |
  tar -C "$stage" --no-same-owner --no-same-permissions -xf -
manifest="$stage/realm-manifest.json"
source_root="$stage/kandelo-source"
test -f "$manifest" && test ! -L "$manifest"
test -d "$source_root" && test ! -L "$source_root"

EXPECTED_COMMIT="$expected_commit" EXPECTED_TREE="$expected_tree" \
  MANIFEST="$manifest" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["MANIFEST"])
raw = path.read_bytes()
if len(raw) > 16 * 1024 or not raw.endswith(b"\n"):
    raise SystemExit("shared realm manifest framing is invalid")
document = json.loads(raw)
expected = {
    "kind": "kandelo-abi-staging-shared-homebrew-realm",
    "schema": 1,
    "source": {
        "commit": os.environ["EXPECTED_COMMIT"],
        "tree": os.environ["EXPECTED_TREE"],
    },
}
if document != expected:
    raise SystemExit("shared realm manifest identity differs")
canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
if raw != canonical:
    raise SystemExit("shared realm manifest is not canonical JSON")
PY

test "$(git -C "$source_root" rev-parse HEAD)" = "$expected_commit"
test "$(git -C "$source_root" rev-parse 'HEAD^{tree}')" = "$expected_tree"
git -C "$source_root" diff --quiet --ignore-submodules=all --
git -C "$source_root" diff --cached --quiet --ignore-submodules=all --
if git -C "$source_root" config --local --get-regexp \
  '^http\..*\.extraheader$' >/dev/null 2>&1; then
  echo "restored shared realm retains a credential header" >&2
  exit 2
fi

mv "$source_root" "$destination_root"
test -d "$destination_root" && test ! -L "$destination_root"
