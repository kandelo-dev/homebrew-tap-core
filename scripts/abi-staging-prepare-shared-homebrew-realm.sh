#!/usr/bin/env bash
set -euo pipefail

source_root=""
playwright_browsers=""

while (($#)); do
  case "$1" in
    --source-root)
      source_root="${2:-}"
      shift 2
      ;;
    --playwright-browsers)
      playwright_browsers="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown shared-realm preparation argument: $1" >&2
      exit 2
      ;;
  esac
done

[[ "$source_root" = /* && "$playwright_browsers" = /* ]]
test -d "$source_root" && test ! -L "$source_root"
test ! -e "$playwright_browsers" && test ! -L "$playwright_browsers"
source_root="$(cd "$source_root" && pwd -P)"
test "$(git -C "$source_root" rev-parse --show-toplevel)" = "$source_root"

metadata_root="$source_root/.ci-homebrew-realm"
package_cache="$source_root/.ci-test-binary-cache"
test ! -e "$metadata_root" && test ! -L "$metadata_root"
test ! -e "$package_cache" && test ! -L "$package_cache"
test ! -e "$source_root/binaries" && test ! -L "$source_root/binaries"
mkdir -m 0700 "$metadata_root" "$package_cache" "$package_cache/programs"
mkdir -m 0700 "$source_root/binaries"

cd "$source_root"
bash scripts/build-musl.sh
bash scripts/build-musl.sh --arch wasm64posix
bash packages/registry/kernel/build-kernel.sh
bash scripts/build-fork-instrument-tool.sh
bash scripts/build-local-root-spill-tool.sh

host="$(rustc -vV | sed -n 's/^host: //p')"
[[ "$host" =~ ^[A-Za-z0-9_.+-]+$ ]]
cargo build --release -p xtask --target "$host" --quiet
xtask="$source_root/target/$host/release/xtask"
test -x "$xtask" && test ! -L "$xtask"
formula_test_index="$source_root/target/$host/release/formula-test-program-packages.json"
WASM_POSIX_DEPS_REGISTRY="$source_root/packages/registry" \
  "$xtask" build-deps program-index-selected \
    --source-repo-root "$source_root" \
    rootfs "$formula_test_index"
test -f "$formula_test_index" && test ! -L "$formula_test_index"
printf '%s\n' "$host" >"$metadata_root/host-target"

PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --no-audit --no-fund
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
  npm --prefix host ci --no-audit --no-fund
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
  npm --prefix apps/browser-demos ci --no-audit --no-fund

empty_rootfs="$(mktemp "${TMPDIR:-/tmp}/abi-staging-empty-rootfs.XXXXXX.ts")"
trap 'rm -f -- "$empty_rootfs"' EXIT
cat >"$empty_rootfs" <<'EMPTY_ROOTFS'
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

async function main(): Promise<void> {
  const root = process.argv[2];
  if (!root) throw new Error("Kandelo root is required");
  const memory = await import(
    pathToFileURL(join(root, "host/src/vfs/memory-fs.ts")).href
  );
  const abi = await import(
    pathToFileURL(join(root, "host/src/generated/abi.ts")).href
  );
  const fs = memory.MemoryFileSystem.create(
    new SharedArrayBuffer(2 * 1024 * 1024),
  );
  const image = await fs.saveImage({
    metadata: {
      version: 1,
      kernelAbi: abi.ABI_VERSION,
      createdBy: "abi-staging-candidate-formula-test",
    },
    normalizeTimestampsMs: 0,
  });
  writeFileSync(join(root, "host/wasm/rootfs.vfs"), image);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
EMPTY_ROOTFS
node --experimental-wasm-exnref --import tsx/esm "$empty_rootfs" "$source_root"

node_bin="$(command -v node)"
case "$node_bin" in
  /nix/store/*/bin/node) ;;
  *) echo "shared realm resolved an undeclared Node: $node_bin" >&2; exit 2 ;;
esac
printf '%s\n' "$node_bin" >"$metadata_root/node-bin"
PLAYWRIGHT_BROWSERS_PATH="$playwright_browsers" \
  "$node_bin" \
    "$source_root/apps/browser-demos/node_modules/playwright/cli.js" \
    install chromium

# Generated SDK projections may contain absolute links to the producer's
# private package cache. Make those projections portable before packing: keep
# exact Nix-store links, rewrite links within the checkout as relative, and
# materialize every other generated external target as ordinary content.
while IFS= read -r -d '' link; do
  raw_target="$(readlink "$link")"
  case "$raw_target" in
    /*)
      resolved_target="$(readlink -f "$link")"
      test -e "$resolved_target"
      case "$resolved_target" in
        "$source_root"/*)
          relative_target="$(realpath --relative-to="$(dirname "$link")" "$resolved_target")"
          rm "$link"
          ln -s "$relative_target" "$link"
          ;;
        /nix/store/*)
          ;;
        *)
          relative="${link#"$source_root"/}"
          if git -C "$source_root" ls-files --error-unmatch -- "$relative" \
            >/dev/null 2>&1; then
            echo "tracked source has a nonportable absolute symlink: $relative" >&2
            exit 2
          fi
          staged="$link.abi-staging-materialized"
          test ! -e "$staged" && test ! -L "$staged"
          cp -aL -- "$resolved_target" "$staged"
          rm "$link"
          mv "$staged" "$link"
          ;;
      esac
      ;;
  esac
done < <(find "$source_root" -type l -print0)

test -s "$metadata_root/host-target"
test -s "$metadata_root/node-bin"
test -d "$playwright_browsers" && test ! -L "$playwright_browsers"
test -f "$source_root/host/wasm/rootfs.vfs"
test -x "$source_root/tools/bin/wasm-fork-instrument"
test -x "$source_root/tools/bin/wasm-local-root-spill"
