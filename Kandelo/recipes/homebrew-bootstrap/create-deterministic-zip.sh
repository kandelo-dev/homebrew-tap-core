#!/usr/bin/env bash
#
# Create a byte-reproducible ZIP from an already-staged directory tree.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "create-deterministic-zip: usage: create-deterministic-zip.sh <staging-dir> <output.zip>" >&2
    exit 2
fi

STAGING_DIR="$1"
OUTPUT_FILE="$2"

if [ ! -d "$STAGING_DIR" ] || [ -L "$STAGING_DIR" ]; then
    echo "create-deterministic-zip: staging path must be a real directory: $STAGING_DIR" >&2
    exit 1
fi

STAGING_DIR="$(cd "$STAGING_DIR" && pwd -P)"
OUTPUT_PARENT="$(dirname "$OUTPUT_FILE")"
mkdir -p "$OUTPUT_PARENT"
OUTPUT_PARENT="$(cd "$OUTPUT_PARENT" && pwd -P)"
OUTPUT_FILE="$OUTPUT_PARENT/$(basename "$OUTPUT_FILE")"

case "$OUTPUT_FILE/" in
    "$STAGING_DIR/"*)
        echo "create-deterministic-zip: output must be outside the staging tree: $OUTPUT_FILE" >&2
        exit 1
        ;;
esac

TMP_DIR="$(mktemp -d "$OUTPUT_FILE.tmp.XXXXXX")"
trap 'rm -rf -- "$TMP_DIR"' EXIT
MIRROR_DIR="$TMP_DIR/staging"
ENTRY_LIST="$TMP_DIR/entries.txt"
TMP_OUTPUT="$TMP_DIR/archive.zip"
mkdir -p "$MIRROR_DIR"

has_executable_mode_bit() {
    local mode
    mode="$(LC_ALL=C stat -f '%Lp' "$1" 2>/dev/null || true)"
    if [[ ! "$mode" =~ ^[0-7]+$ ]]; then
        mode="$(LC_ALL=C stat -c '%a' "$1" 2>/dev/null || true)"
    fi
    if [[ ! "$mode" =~ ^[0-7]+$ ]]; then
        echo "create-deterministic-zip: could not read mode: $1" >&2
        return 2
    fi
    (( (8#$mode & 8#111) != 0 ))
}

# WHY: a private normalized mirror keeps source mtimes, umask, ACLs, and
# enumeration order from changing the package bytes while preserving the only
# permission distinction Homebrew needs at runtime: executable versus data.
cd "$STAGING_DIR"
while IFS= read -r -d '' path; do
    relative="${path#./}"
    if [[ "$relative" == *$'\n'* ]]; then
        echo "create-deterministic-zip: ZIP entry names must not contain newlines: $relative" >&2
        exit 1
    fi
    destination="$MIRROR_DIR/$relative"
    if [ -L "$path" ]; then
        mkdir -p "$(dirname "$destination")"
        (umask 000; cp -P "$path" "$destination")
    elif [ -d "$path" ]; then
        mkdir -p "$destination"
    elif [ -f "$path" ]; then
        mkdir -p "$(dirname "$destination")"
        cp "$path" "$destination"
        if has_executable_mode_bit "$path"; then
            chmod 0755 "$destination"
        else
            mode_status=$?
            if [ "$mode_status" -eq 1 ]; then
                chmod 0644 "$destination"
            else
                exit "$mode_status"
            fi
        fi
    else
        echo "create-deterministic-zip: unsupported special file: $relative" >&2
        exit 1
    fi
done < <(LC_ALL=C find . -mindepth 1 -print0 | LC_ALL=C sort -z)

entry_count=0
cd "$MIRROR_DIR"
while IFS= read -r -d '' path; do
    relative="${path#./}"
    if [ -L "$path" ]; then
        TZ=UTC touch -h -t 200001010000.00 "$path"
    elif [ -d "$path" ]; then
        chmod 0755 "$path"
        TZ=UTC touch -t 200001010000.00 "$path"
    elif [ -f "$path" ]; then
        TZ=UTC touch -t 200001010000.00 "$path"
    fi
    printf '%s\n' "$relative" >> "$ENTRY_LIST"
    entry_count=$((entry_count + 1))
done < <(LC_ALL=C find . -mindepth 1 -print0 | LC_ALL=C sort -z)

if [ "$entry_count" -eq 0 ]; then
    echo "create-deterministic-zip: staging tree is empty: $STAGING_DIR" >&2
    exit 1
fi

# -X strips host-specific fields, -y stores links as links, and the canonical
# list prevents a filesystem walk from reintroducing host enumeration order.
env -u SOURCE_DATE_EPOCH -u ZIP -u ZIPOPT LC_ALL=C TZ=UTC \
    zip -X -y -6 -q "$TMP_OUTPUT" -@ < "$ENTRY_LIST"
chmod 0644 "$TMP_OUTPUT"
mv -f "$TMP_OUTPUT" "$OUTPUT_FILE"
