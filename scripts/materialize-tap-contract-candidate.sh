#!/usr/bin/env bash

# Materialize untrusted pull-request contracts without executing their code.

set -euo pipefail

: "${GH_TOKEN:?}"
: "${GITHUB_ENV:?}"
: "${GITHUB_WORKSPACE:?}"
: "${HEAD_REPOSITORY:?}"
: "${HEAD_SHA:?}"
: "${RUNNER_TEMP:?}"

[[ "$HEAD_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
    echo "::error::invalid pull-request head repository"
    exit 2
}
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || {
    echo "::error::invalid pull-request head SHA"
    exit 2
}

authority_relative="Kandelo/prefix-campaign-authority.json"
completion_relative="Kandelo/campaigns/prefix-v1/completion.json"
manifest_relative="Kandelo/campaigns/prefix-v1/manifest.json"
source_relative="Kandelo/campaigns/prefix-v1/source"
schema_relative="Kandelo/campaigns/prefix-v1/completion.schema.json"

authority="$GITHUB_WORKSPACE/$authority_relative"
completion="$GITHUB_WORKSPACE/$completion_relative"
# WHY: the trusted base chooses the lifecycle. An untrusted pull request may
# not retire the campaign early or restore write authority after retirement.
if [ -f "$authority" ] && [ ! -e "$completion" ]; then
    lifecycle="active"
elif [ ! -e "$authority" ] && [ -f "$completion" ]; then
    lifecycle="retired"
else
    echo "::error::base has an ambiguous prefix-campaign lifecycle"
    exit 2
fi

candidate_root="$(mktemp -d "$RUNNER_TEMP/tap-contract-candidate.XXXXXX")"
tree_json="$(mktemp "$RUNNER_TEMP/tap-contract-tree.XXXXXX.json")"
gh api --method GET \
    -H "Accept: application/vnd.github+json" \
    "repos/${HEAD_REPOSITORY}/git/trees/${HEAD_SHA}?recursive=1" \
    >"$tree_json"

manifest_blob=""
source_tree=""
completion_blob=""
if [ "$lifecycle" = "active" ]; then
    manifest_blob="$(git rev-parse "HEAD:$manifest_relative")"
    source_tree="$(git rev-parse "HEAD:$source_relative")"
else
    completion_blob="$(git rev-parse "HEAD:$completion_relative")"
fi
schema_blob="$(git rev-parse "HEAD:$schema_relative")"

jq -e \
    --arg completion_blob "$completion_blob" \
    --arg lifecycle "$lifecycle" \
    --arg manifest_blob "$manifest_blob" \
    --arg schema_blob "$schema_blob" \
    --arg source_tree "$source_tree" '
      def entry($path):
        [.tree[] | select(.path == $path) | {path, mode, type}];
      def identity($path):
        [.tree[] | select(.path == $path) | {path, mode, sha, type}];
      def object($path; $mode; $type):
        {path: $path, mode: $mode, type: $type};
      .truncated == false and
      ([.tree[] |
        select(.path | startswith(".github/workflows/")) |
        select(.type != "tree") |
        {path, mode, type}] | sort_by(.path)) ==
        ([
          object(".github/workflows/base-contract-checks.yml";
            "100644"; "blob"),
          object(".github/workflows/contract-checks.yml";
            "100644"; "blob"),
          object(".github/workflows/dry-run-bottles.yml";
            "100644"; "blob"),
          object(".github/workflows/maintain-bottles.yml";
            "100644"; "blob"),
          object(".github/workflows/publish-bottles.yml";
            "100644"; "blob"),
          object(".github/workflows/publish-main-shell-mirror.yml";
            "100644"; "blob"),
          object(".github/workflows/repository-namespace-canary.yml";
            "100644"; "blob")
        ] +
        (if $lifecycle == "active" then [
          object(".github/workflows/prefix-campaign-bottles.yml";
            "100644"; "blob")
        ] else [] end) | sort_by(.path)) and
      entry("Kandelo/test-workflow-trust.rb") == [
        object("Kandelo/test-workflow-trust.rb"; "100644"; "blob")
      ] and
      entry("Kandelo/test-workflow-trust.sh") == [
        object("Kandelo/test-workflow-trust.sh"; "100755"; "blob")
      ] and
      entry("scripts/materialize-tap-contract-candidate.sh") == [
        object("scripts/materialize-tap-contract-candidate.sh";
          "100755"; "blob")
      ] and
      entry("scripts/prefix-campaign-controller.py") == [
        object("scripts/prefix-campaign-controller.py";
          "100644"; "blob")
      ] and
      entry("scripts/prefix-campaign-source.py") == [
        object("scripts/prefix-campaign-source.py"; "100644"; "blob")
      ] and
      entry("scripts/test_prefix_campaign_controller.py") == [
        object("scripts/test_prefix_campaign_controller.py";
          "100644"; "blob")
      ] and
      entry("scripts/test_prefix_campaign_source.py") == [
        object("scripts/test_prefix_campaign_source.py";
          "100644"; "blob")
      ] and
      identity("Kandelo/campaigns/prefix-v1/completion.schema.json") == [
        {
          path: "Kandelo/campaigns/prefix-v1/completion.schema.json",
          mode: "100644",
          sha: $schema_blob,
          type: "blob"
        }
      ] and
      (if $lifecycle == "active" then
        entry("Kandelo/prefix-campaign-authority.json") == [
          object("Kandelo/prefix-campaign-authority.json";
            "100644"; "blob")
        ] and
        entry("Kandelo/campaigns/prefix-v1/completion.json") == [] and
        identity("Kandelo/campaigns/prefix-v1/manifest.json") == [
          {
            path: "Kandelo/campaigns/prefix-v1/manifest.json",
            mode: "100644",
            sha: $manifest_blob,
            type: "blob"
          }
        ] and
        identity("Kandelo/campaigns/prefix-v1/source") == [
          {
            path: "Kandelo/campaigns/prefix-v1/source",
            mode: "040000",
            sha: $source_tree,
            type: "tree"
          }
        ]
      else
        entry("Kandelo/prefix-campaign-authority.json") == [] and
        entry("Kandelo/campaigns/prefix-v1/manifest.json") == [] and
        entry("Kandelo/campaigns/prefix-v1/source") == [] and
        identity("Kandelo/campaigns/prefix-v1/completion.json") == [
          {
            path: "Kandelo/campaigns/prefix-v1/completion.json",
            mode: "100644",
            sha: $completion_blob,
            type: "blob"
          }
        ]
      end)
    ' "$tree_json" >/dev/null || {
        echo "::error::candidate workflow or trust-root set changed"
        exit 2
    }

paths=(
    .github/workflows/base-contract-checks.yml
    .github/workflows/contract-checks.yml
    .github/workflows/dry-run-bottles.yml
    .github/workflows/maintain-bottles.yml
    .github/workflows/publish-bottles.yml
    .github/workflows/publish-main-shell-mirror.yml
    .github/workflows/repository-namespace-canary.yml
    Kandelo/campaigns/prefix-v1/completion.schema.json
    Kandelo/test-workflow-trust.rb
    Kandelo/test-workflow-trust.sh
    scripts/materialize-tap-contract-candidate.sh
    scripts/prefix-campaign-controller.py
    scripts/prefix-campaign-source.py
    scripts/test_prefix_campaign_controller.py
    scripts/test_prefix_campaign_source.py
)
if [ "$lifecycle" = "active" ]; then
    paths+=(
        .github/workflows/prefix-campaign-bottles.yml
        Kandelo/prefix-campaign-authority.json
    )
else
    paths+=(Kandelo/campaigns/prefix-v1/completion.json)
fi

for path in "${paths[@]}"; do
    destination="$candidate_root/$path"
    mkdir -p "$(dirname "$destination")"
    gh api --method GET \
        -H "Accept: application/vnd.github.raw+json" \
        "repos/${HEAD_REPOSITORY}/contents/${path}?ref=${HEAD_SHA}" \
        >"$destination"
done

for path in "${paths[@]}"; do
    # WHY: these bytes are later parsed as inert candidate data. Comparing
    # them to the checked-out base prevents a pull request from replacing the
    # parser, downloader, or workflow that defines its own trust boundary.
    cmp -s "$GITHUB_WORKSPACE/$path" "$candidate_root/$path" || {
        echo "::error::base-owned trust contract changed: $path"
        exit 2
    }
done
printf 'KANDELO_TAP_CONTRACT_CANDIDATE=%s\n' "$candidate_root" \
    >>"$GITHUB_ENV"
