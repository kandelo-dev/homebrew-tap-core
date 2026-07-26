#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${WASM_POSIX_DEP_SOURCE_DIR:-}"
RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:-}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:-}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:-}"
PACKAGE_NAME="${WASM_POSIX_DEP_NAME:-}"
PACKAGE_VERSION="${WASM_POSIX_DEP_VERSION:-}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:-}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:-}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:-}"
RUBY="${HOMEBREW_BOOTSTRAP_RUBY:-}"

for required in SOURCE_DIR RECIPE_DIR WORK_DIR OUT_DIR PACKAGE_NAME PACKAGE_VERSION \
    TARGET_ARCH SOURCE_URL SOURCE_SHA256 RUBY; do
    if [ -z "${!required}" ]; then
        echo "homebrew-bootstrap: $required is required" >&2
        exit 2
    fi
done

LOCK="$RECIPE_DIR/source-lock.json"
VERIFY="$RECIPE_DIR/verify-source-lock.rb"
PATCH="$RECIPE_DIR/patches/0001-add-kandelo-wasm-bottle-tags.patch"
LICENSE_EVIDENCE="$RECIPE_DIR/PATCH-LICENSE.md"
ZIPPER="$RECIPE_DIR/create-deterministic-zip.sh"
for input in "$LOCK" "$VERIFY" "$PATCH" "$LICENSE_EVIDENCE" "$ZIPPER"; do
    if [ ! -f "$input" ] || [ -L "$input" ]; then
        echo "homebrew-bootstrap: recipe input must be a regular non-symlink file: $input" >&2
        exit 2
    fi
done
if [ ! -x "$RUBY" ]; then
    echo "homebrew-bootstrap: native Homebrew Ruby is unavailable: $RUBY" >&2
    exit 2
fi
for tool in git zip; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "homebrew-bootstrap: declared native build tool is unavailable: $tool" >&2
        exit 2
    }
done

"$RUBY" "$VERIFY" \
    --lock "$LOCK" \
    --package-name "$PACKAGE_NAME" \
    --package-version "$PACKAGE_VERSION" \
    --target-arch "$TARGET_ARCH" \
    --source-url "$SOURCE_URL" \
    --source-sha256 "$SOURCE_SHA256" \
    --source-dir "$SOURCE_DIR" \
    --patch "$PATCH" \
    --license-evidence "$LICENSE_EVIDENCE"

BUILD_DIR="$WORK_DIR/homebrew-bootstrap"
if [ -e "$BUILD_DIR" ] || [ -L "$BUILD_DIR" ]; then
    echo "homebrew-bootstrap: private build directory already exists: $BUILD_DIR" >&2
    exit 1
fi
mkdir -m 0700 "$BUILD_DIR"
OBJECT_STORE="$BUILD_DIR/source.git"
INDEX_FILE="$BUILD_DIR/source.index"
STAGE_DIR="$BUILD_DIR/patched-source"
ARCHIVE="$BUILD_DIR/homebrew-bootstrap.zip"
ENVIRONMENT="$BUILD_DIR/homebrew-brew.env"
PROVENANCE="$BUILD_DIR/provenance.json"
HOOKS_DIR="$BUILD_DIR/hooks"
TEMPLATE_DIR="$BUILD_DIR/template"
mkdir -m 0700 "$HOOKS_DIR" "$TEMPLATE_DIR" "$STAGE_DIR"

# WHY: Git is only a content-addressed tree builder here. Removing ambient
# configuration prevents an upstream attribute, local hook, credential helper,
# fsmonitor, URL rewrite, or caller-provided Git setting from becoming package
# authority or executing during the source-tree proof.
while IFS= read -r git_variable; do
    case "$git_variable" in
        GIT_*) unset "$git_variable" ;;
    esac
done < <(compgen -A variable)
unset SSH_ASKPASS GH_TOKEN GITHUB_TOKEN HOMEBREW_GITHUB_API_TOKEN \
    HOMEBREW_GITHUB_PACKAGES_TOKEN HOMEBREW_DOCKER_REGISTRY_TOKEN
export GIT_ATTR_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_OPTIONAL_LOCKS=0
export GIT_PAGER=cat
export GIT_TERMINAL_PROMPT=0
export GIT_INDEX_FILE="$INDEX_FILE"

GIT_ISOLATION_ARGS=(
    -c "core.hooksPath=$HOOKS_DIR"
    -c core.fsmonitor=false
    -c core.untrackedCache=false
    -c core.attributesFile=/dev/null
    -c core.excludesFile=/dev/null
    -c core.autocrlf=false
    -c core.filemode=true
    -c credential.helper=
    -c credential.interactive=false
    -c http.extraHeader=
)
isolated_git() {
    command git "${GIT_ISOLATION_ARGS[@]}" --git-dir="$OBJECT_STORE" "$@"
}

isolated_git init --bare -q --template="$TEMPLATE_DIR"

# -f is intentional: GitHub's verified source archive contains a few files
# ignored by Homebrew's own .gitignore. The exact tree OID below proves that
# neither those rules nor filesystem extraction changed the upstream tree.
isolated_git --work-tree="$SOURCE_DIR" add -f -A -- .
UPSTREAM_TREE="$(isolated_git write-tree)"
EXPECTED_UPSTREAM_TREE="$("$RUBY" "$VERIFY" --lock "$LOCK" --field source.tree_git_oid)"
if [ "$UPSTREAM_TREE" != "$EXPECTED_UPSTREAM_TREE" ]; then
    echo "homebrew-bootstrap: verified source archive reconstructed Git tree $UPSTREAM_TREE, expected $EXPECTED_UPSTREAM_TREE" >&2
    exit 1
fi

EXPECTED_PATCH_SHA256="$("$RUBY" "$VERIFY" --lock "$LOCK" --field patch.sha256)"
ACTUAL_PATCH_SHA256="$("$RUBY" -rdigest -e 'print Digest::SHA256.file(ARGV.fetch(0)).hexdigest' "$PATCH")"
if [ "$ACTUAL_PATCH_SHA256" != "$EXPECTED_PATCH_SHA256" ]; then
    echo "homebrew-bootstrap: reviewed patch digest changed" >&2
    exit 1
fi
isolated_git apply --cached --check --whitespace=nowarn "$PATCH"
isolated_git apply --cached --whitespace=nowarn "$PATCH"

mapfile -t CHANGED_PATHS < <(
    isolated_git diff --cached --name-only "$UPSTREAM_TREE" -- | LC_ALL=C sort
)
EXPECTED_PATHS=(
    "Library/Homebrew/extend/os/mac/utils/bottles.rb"
    "Library/Homebrew/github_packages.rb"
    "Library/Homebrew/hardware.rb"
    "Library/Homebrew/utils/bottles.rb"
    "bin/brew"
)
if [ "${CHANGED_PATHS[*]}" != "${EXPECTED_PATHS[*]}" ]; then
    echo "homebrew-bootstrap: patch changed an unexpected path set" >&2
    printf '  %s\n' "${CHANGED_PATHS[@]}" >&2
    exit 1
fi

PATCHED_TREE="$(isolated_git write-tree)"
EXPECTED_PATCHED_TREE="$("$RUBY" "$VERIFY" --lock "$LOCK" --field prepared.patched_tree_git_oid)"
if [ "$PATCHED_TREE" != "$EXPECTED_PATCHED_TREE" ]; then
    echo "homebrew-bootstrap: patched Git tree $PATCHED_TREE, expected $EXPECTED_PATCHED_TREE" >&2
    exit 1
fi

COMMIT_TIMESTAMP="$("$RUBY" "$VERIFY" --lock "$LOCK" --field source.commit_timestamp)"
PATCHED_TREE_SHA256="$(
    TZ=UTC isolated_git archive --format=tar --mtime="@$COMMIT_TIMESTAMP" "$PATCHED_TREE" |
        "$RUBY" -rdigest -e 'digest = Digest::SHA256.new; while (chunk = STDIN.read(1024 * 1024)); digest.update(chunk); end; print digest.hexdigest'
)"
EXPECTED_PATCHED_TREE_SHA256="$("$RUBY" "$VERIFY" --lock "$LOCK" --field prepared.patched_tree_sha256)"
if [ "$PATCHED_TREE_SHA256" != "$EXPECTED_PATCHED_TREE_SHA256" ]; then
    echo "homebrew-bootstrap: patched tree serialization changed" >&2
    exit 1
fi

isolated_git --work-tree="$STAGE_DIR" checkout-index --all --force
"$ZIPPER" "$STAGE_DIR" "$ARCHIVE"
cat >"$ENVIRONMENT" <<'EOF'
HOMEBREW_NO_ANALYTICS=1
HOMEBREW_NO_AUTO_UPDATE=1
HOMEBREW_NO_INSTALL_FROM_API=1
HOMEBREW_AUTOMATICALLY_SET_NO_INSTALL_FROM_API=1
HOMEBREW_SYSTEM_ENV_TAKES_PRIORITY=1
HOMEBREW_KANDELO_BOTTLE_TAG=wasm32_kandelo
EOF
chmod 0644 "$ENVIRONMENT"

"$RUBY" "$VERIFY" \
    --lock "$LOCK" \
    --upstream-tree "$UPSTREAM_TREE" \
    --patched-tree "$PATCHED_TREE" \
    --patched-tree-sha256 "$PATCHED_TREE_SHA256" \
    --archive "$ARCHIVE" \
    --environment "$ENVIRONMENT" \
    --write-provenance "$PROVENANCE"
"$RUBY" "$VERIFY" \
    --lock "$LOCK" \
    --archive "$ARCHIVE" \
    --environment "$ENVIRONMENT" \
    --provenance "$PROVENANCE"

ARCHIVE_NAME="$("$RUBY" "$VERIFY" --lock "$LOCK" --field outputs.archive.path)"
ENVIRONMENT_NAME="$("$RUBY" "$VERIFY" --lock "$LOCK" --field outputs.environment.path)"
if [ -e "$OUT_DIR/$ARCHIVE_NAME" ] || [ -L "$OUT_DIR/$ARCHIVE_NAME" ] ||
   [ -e "$OUT_DIR/$ENVIRONMENT_NAME" ] || [ -L "$OUT_DIR/$ENVIRONMENT_NAME" ]; then
    echo "homebrew-bootstrap: output already exists" >&2
    exit 1
fi
cp "$ARCHIVE" "$OUT_DIR/$ARCHIVE_NAME"
cp "$ENVIRONMENT" "$OUT_DIR/$ENVIRONMENT_NAME"
chmod 0644 "$OUT_DIR/$ARCHIVE_NAME" "$OUT_DIR/$ENVIRONMENT_NAME"

echo "==> Built tap-owned Homebrew bootstrap: $ARCHIVE_NAME + $ENVIRONMENT_NAME"
