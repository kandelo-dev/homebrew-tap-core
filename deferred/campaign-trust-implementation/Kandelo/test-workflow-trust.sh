#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -B "$ROOT/scripts/test_finalize_main_shell_mirror_caller.py"
ruby "$ROOT/Kandelo/test-workflow-trust.rb"
