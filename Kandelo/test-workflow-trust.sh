#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ruby "$ROOT/Kandelo/test-workflow-trust.rb"
