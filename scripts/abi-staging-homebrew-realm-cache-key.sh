#!/usr/bin/env bash
set -euo pipefail

tap_root=""
kandelo_commit=""
runner_os=""
runner_arch=""
github_output=""

while (($#)); do
  case "$1" in
    --tap-root)
      tap_root="${2:-}"
      shift 2
      ;;
    --kandelo-commit)
      kandelo_commit="${2:-}"
      shift 2
      ;;
    --runner-os)
      runner_os="${2:-}"
      shift 2
      ;;
    --runner-arch)
      runner_arch="${2:-}"
      shift 2
      ;;
    --github-output)
      github_output="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown Homebrew realm cache-key argument: $1" >&2
      exit 2
      ;;
  esac
done

[[ "$tap_root" = /* && "$github_output" = /* ]]
[[ "$kandelo_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$runner_os" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$runner_arch" =~ ^[A-Za-z0-9._-]+$ ]]
test -d "$tap_root" && test ! -L "$tap_root"
test -f "$github_output" && test ! -L "$github_output"
tap_root="$(cd "$tap_root" && pwd -P)"

producer_inputs=(
  scripts/abi-staging-prepare-shared-homebrew-realm.sh
  scripts/abi-staging-pack-homebrew-realm.sh
)
for relative in "${producer_inputs[@]}"; do
  path="$tap_root/$relative"
  test -f "$path" && test ! -L "$path"
done

producer_sha256="$({
  printf '%s\n' 'kandelo-abi-staging-homebrew-realm-producer-v1'
  for relative in "${producer_inputs[@]}"; do
    digest="$(sha256sum "$tap_root/$relative" | awk '{print $1}')"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]]
    printf '%s  %s\n' "$digest" "$relative"
  done
} | sha256sum | awk '{print $1}')"
[[ "$producer_sha256" =~ ^[0-9a-f]{64}$ ]]

cache_key="abi-staging-homebrew-realm-v2-${runner_os}-${runner_arch}-${kandelo_commit}-${producer_sha256}"
legacy_cache_key=""
# WHY: The first cache implementation keyed identical bytes by the whole tap
# commit. Migrate only that one producer-byte identity; any producer edit must
# miss rather than interpreting an older archive as compatible.
if [[ "$producer_sha256" = \
  "eeead14c5f1d64acc40d13e9ab707d7a94e9379ee76c483cf42537361b3d427e" ]]; then
  legacy_cache_key="abi-staging-homebrew-realm-${runner_os}-${runner_arch}-${kandelo_commit}-8697f602d2abd5aefb1d9d278e532a2437a1ac06"
fi

{
  echo "cache_key=$cache_key"
  echo "legacy_cache_key=$legacy_cache_key"
  echo "producer_sha256=$producer_sha256"
} >>"$github_output"
