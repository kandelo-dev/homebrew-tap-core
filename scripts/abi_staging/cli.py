"""Command-line entrypoint for protected observe-only request reconciliation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from .canonical import canonical_bytes
from .github_public import DiscoveredRequestV1, GitHubPublicClient, PublicGitHubError
from .formula_inventory import FormulaInventoryError, write_formula_inventory_fixture
from .reconcile import (
    ReconciliationDecisionV1,
    ReconciliationError,
    load_reconciliation_activation,
    reconcile_request,
)
from .request import RequestValidationError, load_request_issuer_policy
from .policy import (
    PolicyError,
    check_policy_files,
    write_formula_capture_catalog,
)


TAP_ROOT = Path(__file__).resolve().parents[2]


def _decision_mapping(
    discovered: DiscoveredRequestV1,
    decision: ReconciliationDecisionV1,
) -> dict[str, Any]:
    return {
        "action": decision.action,
        "asset_url": discovered.asset_url,
        "blockers": list(decision.blockers),
        "claim_key": decision.claim_key,
        "current_for_pull_request": decision.current_for_pull_request,
        "exact_head": discovered.request["build_source"]["commit"],
        "lifecycle": {
            "current_head": decision.lifecycle.current_head,
            "merged_commit": decision.lifecycle.merged_commit,
            "state": decision.lifecycle.state,
        },
        "permitted_work": list(decision.permitted_work),
        "request_digest": decision.request_digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.abi_staging.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("scan")
    reconcile = subcommands.add_parser("reconcile")
    reconcile.add_argument("--request-asset-url", required=True)
    policy_check = subcommands.add_parser("policy-check")
    policy_check.add_argument("--tap-root", required=True)
    policy_generate = subcommands.add_parser("policy-generate")
    policy_generate.add_argument("--tap-root", required=True)
    policy_generate.add_argument("--out", required=True)
    formula_fixture = subcommands.add_parser("formula-inventory-fixture")
    formula_fixture.add_argument("--tap-root", required=True)
    formula_fixture.add_argument("--out", required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command in {"policy-check", "policy-generate", "formula-inventory-fixture"}:
            tap_root = Path(args.tap_root).resolve(strict=True)
            if tap_root != TAP_ROOT.resolve(strict=True):
                raise PolicyError("--tap-root must name this protected tap checkout")
            if args.command == "policy-check":
                check_policy_files(tap_root)
            elif args.command == "policy-generate":
                write_formula_capture_catalog(tap_root, Path(args.out))
            else:
                write_formula_inventory_fixture(tap_root, Path(args.out))
            return 0
        policy = load_request_issuer_policy(
            TAP_ROOT / "Kandelo/staging/request-issuers.toml",
            expected_tap="kandelo-dev/homebrew-tap-core",
        )
        load_reconciliation_activation(
            TAP_ROOT / "Kandelo/staging/reconciliation-activation.toml"
        )
        client = GitHubPublicClient(policy)
        if args.command == "scan":
            discovered = client.scan()
        else:
            discovered = (client.discover_url(args.request_asset_url),)
        decisions = []
        for request in discovered:
            number = request.request["pull_request"]["number"]
            lifecycle = client.pull_request_lifecycle(number)
            decisions.append(_decision_mapping(request, reconcile_request(request, lifecycle)))
        sys.stdout.buffer.write(canonical_bytes({"decisions": decisions, "mode": "observe"}))
        return 0
    except (
        PolicyError,
        FormulaInventoryError,
        PublicGitHubError,
        ReconciliationError,
        RequestValidationError,
    ) as error:
        print(f"abi-staging {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
