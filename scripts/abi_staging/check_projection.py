"""Protected collection of public facts for the Kandelo exact-head Check."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

from .canonical import canonical_bytes, canonical_sha256
from .contract import load_canonical_mapping
from .coordination import prepare_tap_plan_contracts
from .github_public import DiscoveredRequestV1
from .github_public import GitHubPublicClient
from .inventory import PublicSchedulingInventoryV1
from .inventory import scan_scheduling_inventory
from .oci import (
    OciTransportV1,
    UrllibOciTransportV1,
    fetch_public_record,
    list_public_record_locators,
)
from .override import CAPTURE_AUTHORIZATION_MEDIA_TYPE, OVERRIDE_RECEIPT_MEDIA_TYPE
from .plan import (
    load_formula_requirements,
    plan_exact_tap_request,
    snapshot_tap_source,
)
from .policy import (
    candidate_repository,
    load_tap_staging_policy,
    load_verification_tests,
)
from .product import select_product_input_build_spec
from .product_evidence import (
    candidate_product_repository,
    inspect_candidate_product_repository,
    inspect_product_evidence_repository,
)
from .reconcile import PullRequestLifecycleV1, reconcile_request
from .request import load_request_issuer_policy, validate_request
from .scheduler import SchedulingDecisionV1, schedule_ready_batch


SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
CONTEXT_FIELDS = frozenset(
    {
        "repository",
        "pull_request_number",
        "exact_head",
        "current_requirements_sha256",
        "current_policy_version",
        "current_policy_sha256",
        "current_guard_registry_version",
        "current_guard_registry_sha256",
    }
)


class CheckProjectionCollectionError(ValueError):
    """Raised when public facts cannot form one bounded projection input."""


def _plain(value: Any) -> Any:
    return json.loads(canonical_bytes(value))


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise CheckProjectionCollectionError(f"{field} must be UTC RFC 3339")
    for grammar in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            parsed = datetime.strptime(value, grammar).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return parsed
    raise CheckProjectionCollectionError(f"{field} must be UTC RFC 3339")


def _checked_record_locator(locator: Mapping[str, Any]) -> tuple[str, str]:
    if frozenset(locator) != frozenset(
        {"repository", "digest", "immutable_reference"}
    ):
        raise CheckProjectionCollectionError("public record locator fields changed")
    repository = locator["repository"]
    digest_value = locator["digest"]
    reference = locator["immutable_reference"]
    if not isinstance(repository, str) or not repository.startswith("ghcr.io/"):
        raise CheckProjectionCollectionError("public record repository is not GHCR")
    match = OCI_DIGEST.fullmatch(digest_value) if isinstance(digest_value, str) else None
    if match is None:
        raise CheckProjectionCollectionError("public record manifest digest is invalid")
    digest = match.group(1)
    if reference != f"{repository}@sha256:{digest}":
        raise CheckProjectionCollectionError("public record reference is not immutable")
    return digest, reference


def public_record_envelope(
    *, kind: str, locator: Mapping[str, Any], record: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind canonical record bytes separately from their OCI manifest identity."""

    if not isinstance(kind, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", kind) is None:
        raise CheckProjectionCollectionError("public record kind is not stable")
    digest, reference = _checked_record_locator(locator)
    normalized = _plain(record)
    return {
        "kind": kind,
        "digest": digest,
        "record_sha256": canonical_sha256(normalized),
        "immutable_reference": reference,
        "record": normalized,
    }


def inventory_claims_request(
    inventory: PublicSchedulingInventoryV1, request_digest: str
) -> bool:
    if not isinstance(inventory, PublicSchedulingInventoryV1):
        raise CheckProjectionCollectionError("public inventory changed type")
    if SHA256.fullmatch(request_digest) is None:
        raise CheckProjectionCollectionError("request claim digest is invalid")
    return any(
        fact.request_sha256 == request_digest
        for collection in (
            inventory.records.attempts,
            inventory.records.candidates,
            inventory.records.verifications,
        )
        for fact in collection
    )


def _fact_record_link(
    kind: str, digest: str, locators: Mapping[str, Mapping[str, str]]
) -> dict[str, str] | None:
    locator = locators.get(digest)
    if locator is None:
        return None
    manifest_digest, reference = _checked_record_locator(locator)
    return {
        "kind": kind,
        "digest": manifest_digest,
        "immutable_reference": reference,
    }


def scheduling_blockers(
    scheduling: SchedulingDecisionV1,
    *,
    inventory: PublicSchedulingInventoryV1,
    required_subjects: Sequence[str],
) -> list[dict[str, Any]]:
    """Project only terminal required scheduler blockers and their exact records."""

    if not isinstance(scheduling, SchedulingDecisionV1):
        raise CheckProjectionCollectionError("scheduling decision changed type")
    required = set(required_subjects)
    if len(required) != len(required_subjects):
        raise CheckProjectionCollectionError("required subjects repeat")
    result = []
    for blocked in scheduling.blocked:
        if blocked.subject not in required or blocked.next_action in {"wait", "retry"}:
            continue
        verification = sorted(
            (
                fact
                for fact in inventory.records.verifications
                if fact.request_sha256 == scheduling.request_sha256
                and fact.subject == blocked.subject
                and fact.guard_code == blocked.guard_code
            ),
            key=lambda fact: (-fact.attempt_ordinal, fact.record_sha256),
        )
        attempts = sorted(
            (
                fact
                for fact in inventory.records.attempts
                if fact.request_sha256 == scheduling.request_sha256
                and fact.subject == blocked.subject
                and fact.guard_code == blocked.guard_code
            ),
            key=lambda fact: (-fact.retry_ordinal, fact.record_sha256),
        )
        link = None
        if verification:
            link = _fact_record_link(
                "verification",
                verification[0].record_sha256,
                inventory.verification_locators,
            )
        elif attempts:
            link = _fact_record_link(
                "attempt-outcome",
                attempts[0].record_sha256,
                inventory.attempt_locators,
            )
        item: dict[str, Any] = {
            "guard_code": blocked.guard_code,
            "subject_kind": "formula",
            "subject": blocked.subject,
        }
        if link is not None:
            item["record"] = link
        result.append(item)
    result.sort(
        key=lambda value: (
            value["subject_kind"], value["subject"], value["guard_code"]
        )
    )
    return result


def _validate_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if frozenset(value) != CONTEXT_FIELDS:
        raise CheckProjectionCollectionError("Check context fields changed")
    context = _plain(value)
    if (
        not isinstance(context["repository"], str)
        or context["repository"].count("/") != 1
        or not isinstance(context["pull_request_number"], int)
        or isinstance(context["pull_request_number"], bool)
        or context["pull_request_number"] < 1
        or not isinstance(context["exact_head"], str)
        or re.fullmatch(r"[0-9a-f]{40}", context["exact_head"]) is None
    ):
        raise CheckProjectionCollectionError("Check context identity is invalid")
    for field in (
        "current_requirements_sha256",
        "current_policy_sha256",
        "current_guard_registry_sha256",
    ):
        if not isinstance(context[field], str) or SHA256.fullmatch(context[field]) is None:
            raise CheckProjectionCollectionError(f"Check context {field} is invalid")
    for field in ("current_policy_version", "current_guard_registry_version"):
        if (
            not isinstance(context[field], int)
            or isinstance(context[field], bool)
            or context[field] < 1
        ):
            raise CheckProjectionCollectionError(f"Check context {field} is invalid")
    return context


def _current_request(
    expected_request: Mapping[str, Any],
    discovered_requests: Sequence[DiscoveredRequestV1],
) -> DiscoveredRequestV1 | None:
    expected_bytes = canonical_bytes(expected_request)
    expected_digest = canonical_sha256(expected_request)
    matches = []
    for discovered in discovered_requests:
        if not isinstance(discovered, DiscoveredRequestV1):
            raise CheckProjectionCollectionError("request discovery item changed type")
        if (
            canonical_bytes(discovered.request) == expected_bytes
            and discovered.request_digest == expected_digest
        ):
            matches.append(discovered)
    if len(matches) > 1:
        raise CheckProjectionCollectionError(
            "public discovery returned duplicate current requests"
        )
    return matches[0] if matches else None


def build_check_projection_input(
    *,
    context: Mapping[str, Any],
    applicable: bool,
    expected_request: Mapping[str, Any] | None,
    discovered_requests: Sequence[DiscoveredRequestV1],
    tap_plan: Mapping[str, Any] | None,
    blockers: Sequence[Mapping[str, Any]],
    public_records: Sequence[Mapping[str, Any]],
    request_claimed: bool,
    now: str,
) -> dict[str, Any]:
    """Build one canonical projector input without treating timestamps as authority."""

    checked_context = _validate_context(context)
    if not isinstance(applicable, bool):
        raise CheckProjectionCollectionError("Check applicability must be boolean")
    if not isinstance(request_claimed, bool):
        raise CheckProjectionCollectionError("request claim observation must be boolean")
    base: dict[str, Any] = {
        "schema": 1,
        "kind": "kandelo-abi-staging-check-projection-input",
        "context": checked_context,
        "applicable": applicable,
        "discovery_delayed": False,
        "expected_request_digest": "0" * 64,
        "public_records": [],
    }
    if not applicable:
        if (
            expected_request is not None
            or tap_plan is not None
            or public_records
            or blockers
            or request_claimed
        ):
            raise CheckProjectionCollectionError(
                "not-applicable collection cannot carry staging facts"
            )
        return _plain(base)
    if expected_request is None:
        raise CheckProjectionCollectionError(
            "applicable collection requires one protected expected request"
        )
    selected = _current_request(expected_request, discovered_requests)
    if selected is None:
        if request_claimed or public_records or blockers:
            raise CheckProjectionCollectionError(
                "missing current request cannot carry downstream public facts"
            )
        return _plain(base)
    if tap_plan is None:
        raise CheckProjectionCollectionError("current request lacks its exact tap plan")
    if tap_plan.get("request_digest") != selected.request_digest:
        raise CheckProjectionCollectionError("tap plan names another request")

    envelopes = [_plain(value) for value in public_records]
    envelopes.sort(key=lambda value: (value.get("digest", ""), value.get("kind", "")))
    digests = [value.get("digest") for value in envelopes]
    references = [value.get("immutable_reference") for value in envelopes]
    if len(set(digests)) != len(digests) or len(set(references)) != len(references):
        raise CheckProjectionCollectionError("public record envelopes repeat an identity")

    created_at = selected.created_at
    discovery_delayed = False
    if created_at is not None and not request_claimed:
        age = _timestamp(now, "projection collection clock") - _timestamp(
            created_at, "request asset creation time"
        )
        if age.total_seconds() < 0:
            raise CheckProjectionCollectionError(
                "request asset creation time is after the collection clock"
            )
        discovery_delayed = age.total_seconds() >= 15 * 60

    normalized_blockers = [_plain(value) for value in blockers]
    normalized_blockers.sort(
        key=lambda value: (
            value.get("subject_kind", ""),
            value.get("subject", ""),
            value.get("guard_code", ""),
        )
    )
    result = {
        **base,
        "discovery_delayed": discovery_delayed,
        "expected_request_digest": selected.request_digest,
        "expected_request": _plain(expected_request),
        "request": {
            "digest": selected.request_digest,
            "immutable_reference": selected.asset_url,
            "request": _plain(selected.request),
        },
        "tap_plan": {
            "request_digest": selected.request_digest,
            "required_subjects": _plain(tap_plan.get("required_subjects")),
            "background_subjects": _plain(tap_plan.get("background_subjects")),
            "blockers": normalized_blockers,
        },
        "public_records": envelopes,
    }
    return _plain(result)


def _checked_exact_head_checkout(
    root_value: Path, *, repository: str, commit: str, tree: str
) -> Path:
    root = root_value.resolve(strict=True)
    if snapshot_tap_source(root, repository) != {
        "repository": repository,
        "commit": commit,
        "tree": tree,
    }:
        raise CheckProjectionCollectionError(
            "exact-head checkout differs from protected request identity"
        )
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckProjectionCollectionError(
            f"cannot inspect exact-head checkout: {error}"
        ) from error
    if status.stdout:
        raise CheckProjectionCollectionError("exact-head checkout contains changes")
    return root


def _maintenance_record_envelopes(
    repository: str,
    *,
    media_type: str,
    role: str,
    title: str,
    record_kind: str,
    envelope_kind: str,
    transport: OciTransportV1,
) -> list[dict[str, Any]]:
    result = []
    for locator in list_public_record_locators(repository, transport=transport):
        fetched = fetch_public_record(
            locator,
            transport=transport,
            expected_artifact_type=media_type,
            required_layer_roles=("immutable-record-bytes",),
        )
        record = load_canonical_mapping(
            fetched.config.body, f"{envelope_kind} public record"
        )
        if (
            record.get("kind") != record_kind
            or fetched.config.role != role
            or fetched.config.media_type != media_type
            or fetched.config.title != title
            or len(fetched.layers) != 1
            or fetched.layers[0].body != fetched.config.body
        ):
            raise CheckProjectionCollectionError(
                f"{envelope_kind} public record identity changed"
            )
        result.append(
            public_record_envelope(
                kind=envelope_kind,
                locator={
                    "repository": fetched.repository,
                    "digest": fetched.digest,
                    "immutable_reference": fetched.immutable_reference,
                },
                record=record,
            )
        )
    return result


def _formula_record_envelopes(
    inventory: PublicSchedulingInventoryV1,
) -> list[dict[str, Any]]:
    result = []
    for kind, locators, records in (
        ("candidate", inventory.candidate_locators, inventory.candidate_records),
        (
            "verification",
            inventory.verification_locators,
            inventory.verification_records,
        ),
        ("candidate-reuse", inventory.reuse_locators, inventory.reuse_records),
    ):
        if set(locators) != set(records):
            raise CheckProjectionCollectionError(
                f"{kind} public records and locators differ"
            )
        for digest in sorted(locators):
            result.append(
                public_record_envelope(
                    kind=kind, locator=locators[digest], record=records[digest]
                )
            )
    return result


def _product_record_envelopes(
    request: Mapping[str, Any],
    request_digest: str,
    catalog: Mapping[str, Any],
    *,
    policy: Any,
    transport: OciTransportV1,
) -> list[dict[str, Any]]:
    target_abi = request["target_abi"]["version"]
    result = []
    requirements = request.get("requirements")
    if not isinstance(requirements, Mapping):
        raise CheckProjectionCollectionError("current request lacks requirements")
    evidence = requirements.get("evidence")
    if not isinstance(evidence, list):
        raise CheckProjectionCollectionError("current request evidence changed shape")
    for binding in evidence:
        if not isinstance(binding, Mapping):
            raise CheckProjectionCollectionError("current evidence binding is invalid")
        if binding.get("applicability") == "not-applicable":
            continue
        product_id = binding.get("product_id")
        if not isinstance(product_id, str):
            raise CheckProjectionCollectionError("current evidence product ID is invalid")
        build_spec = select_product_input_build_spec(request, catalog, product_id)
        repository = candidate_product_repository(
            owner=policy.candidate_owner,
            repository_prefix=policy.candidate_repository_prefix,
            candidate_suffix=policy.candidate_suffix,
            target_abi=target_abi,
            product_id=product_id,
        )
        candidates = inspect_candidate_product_repository(
            repository,
            request=request,
            request_sha256=request_digest,
            expected_source_repository=policy.tap_repository,
            transport=transport,
        )
        product = {
            "id": build_spec["id"],
            "manifest_path": build_spec["manifest_path"],
            "manifest_sha256": build_spec["manifest_sha256"],
            "architecture": build_spec["architecture"],
            "output": build_spec["output"],
        }
        for candidate in candidates:
            entries = inspect_product_evidence_repository(
                repository + "/evidence",
                request=request,
                request_sha256=request_digest,
                product=product,
                candidate_product=candidate.locator,
                runtime_bundle_sha256=candidate.runtime_bundle_sha256,
                expected_source_repository=policy.tap_repository,
                transport=transport,
            )
            for entry in entries:
                result.append(
                    public_record_envelope(
                        kind="product-evidence",
                        locator={
                            "repository": "ghcr.io/" + repository + "/evidence",
                            "digest": entry.manifest_digest,
                            "immutable_reference": entry.immutable_reference,
                        },
                        record=entry.record,
                    )
                )
    return result


def collect_check_projection_input(
    *,
    tap_root: Path,
    exact_head_root: Path | None,
    context: Mapping[str, Any],
    applicable: bool,
    expected_request: Mapping[str, Any] | None,
    formula_requirements: Sequence[Mapping[str, Any]] | None,
    now: str,
    client: GitHubPublicClient | None = None,
    transport: OciTransportV1 | None = None,
) -> dict[str, Any]:
    """Collect anonymous public records without executing exact-head code."""

    root = tap_root.resolve(strict=True)
    policy = load_tap_staging_policy(root / "Kandelo/staging/tap-policy.toml")
    issuer_policy = load_request_issuer_policy(
        root / "Kandelo/staging/request-issuers.toml",
        expected_tap=policy.tap_repository,
    )
    if not applicable:
        return build_check_projection_input(
            context=context,
            applicable=False,
            expected_request=None,
            discovered_requests=(),
            tap_plan=None,
            blockers=(),
            public_records=(),
            request_claimed=False,
            now=now,
        )
    discovered = (client or GitHubPublicClient(issuer_policy)).scan()
    if expected_request is None or formula_requirements is None:
        raise CheckProjectionCollectionError(
            "applicable collection lacks protected request requirements"
        )
    selected = _current_request(expected_request, discovered)
    if selected is None:
        return build_check_projection_input(
            context=context,
            applicable=True,
            expected_request=expected_request,
            discovered_requests=discovered,
            tap_plan=None,
            blockers=(),
            public_records=(),
            request_claimed=False,
            now=now,
        )
    source = expected_request.get("build_source")
    if not isinstance(source, Mapping) or exact_head_root is None:
        raise CheckProjectionCollectionError("current request lacks exact source input")
    kandelo_root = _checked_exact_head_checkout(
        exact_head_root,
        repository=source.get("repository"),
        commit=source.get("commit"),
        tree=source.get("tree"),
    )
    tap_plan = plan_exact_tap_request(
        root,
        expected_request,
        request_digest=selected.request_digest,
        request_asset_url=selected.asset_url,
        formula_requirements=formula_requirements,
        tap_repository=policy.tap_repository,
    )
    verification_tests = load_verification_tests(
        root / "Kandelo/staging/verification-tests.toml"
    )
    public_transport = transport or UrllibOciTransportV1(username="", token="")
    inventory = scan_scheduling_inventory(
        tap_plan,
        policy=policy,
        verification_tests=verification_tests,
        transport=public_transport,
    )
    planned, _, _ = prepare_tap_plan_contracts(
        tap_root=root,
        kandelo_root=kandelo_root,
        tap_plan=tap_plan,
        candidate_facts=inventory.records.candidates,
        candidate_records=inventory.candidate_records,
    )
    reconciliation = reconcile_request(
        selected,
        PullRequestLifecycleV1("open", context["exact_head"], None),
    )
    scheduling = schedule_ready_batch(
        planned,
        inventory.records,
        reconciliation,
        now=now,
        policy=policy,
        verification_tests=verification_tests,
    )
    blockers = scheduling_blockers(
        scheduling,
        inventory=inventory,
        required_subjects=planned["required_subjects"],
    )
    records = _formula_record_envelopes(inventory)
    target_abi = expected_request["target_abi"]["version"]
    names = sorted({item["identity"]["name"] for item in planned["formulae"]})
    for name in names:
        base = candidate_repository(policy, target_abi, formula=name)
        records.extend(
            _maintenance_record_envelopes(
                base + "/authorizations/capture",
                media_type=CAPTURE_AUTHORIZATION_MEDIA_TYPE,
                role="capture-authorization",
                title="capture-override-authorization.json",
                record_kind="kandelo-abi-staging-capture-override-authorization",
                envelope_kind="capture-override-authorization",
                transport=public_transport,
            )
        )
        records.extend(
            _maintenance_record_envelopes(
                base + "/receipts/overrides",
                media_type=OVERRIDE_RECEIPT_MEDIA_TYPE,
                role="override-receipt",
                title="override-receipt.json",
                record_kind="kandelo-abi-staging-override-receipt",
                envelope_kind="override",
                transport=public_transport,
            )
        )
    catalog = load_canonical_mapping(
        (kandelo_root / "images/vfs/products/generated/catalog.json").read_bytes(),
        "exact-head VFS product catalog",
    )
    records.extend(
        _product_record_envelopes(
            expected_request,
            selected.request_digest,
            catalog,
            policy=policy,
            transport=public_transport,
        )
    )
    return build_check_projection_input(
        context=context,
        applicable=True,
        expected_request=expected_request,
        discovered_requests=discovered,
        tap_plan=planned,
        blockers=blockers,
        public_records=records,
        request_claimed=inventory_claims_request(inventory, selected.request_digest),
        now=now,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.abi_staging.check_projection"
    )
    parser.add_argument("--tap-root", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--exact-head-root")
    parser.add_argument("--expected-request")
    parser.add_argument("--formula-requirements")
    parser.add_argument("--not-applicable", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        context = load_canonical_mapping(
            Path(args.context).resolve(strict=True).read_bytes(), "Check context"
        )
        expected_request = None
        requirements = None
        if not args.not_applicable:
            if not args.expected_request or not args.formula_requirements:
                raise CheckProjectionCollectionError(
                    "applicable collection requires request and Formula requirements"
                )
            expected_path = Path(args.expected_request).resolve(strict=True)
            expected_body = expected_path.read_bytes()
            raw_expected = load_canonical_mapping(
                expected_body, "protected expected request"
            )
            digest = hashlib.sha256(expected_body).hexdigest()
            asset_name = (
                f"candidate-request-{raw_expected['build_source']['commit']}-"
                f"sha256-{digest}.json"
            )
            policy = load_tap_staging_policy(
                Path(args.tap_root).resolve(strict=True)
                / "Kandelo/staging/tap-policy.toml"
            )
            issuer = load_request_issuer_policy(
                Path(args.tap_root).resolve(strict=True)
                / "Kandelo/staging/request-issuers.toml",
                expected_tap=policy.tap_repository,
            )
            expected_request = validate_request(expected_body, asset_name, issuer)
            requirements = load_formula_requirements(
                Path(args.formula_requirements).resolve(strict=True).read_bytes()
            )
        value = collect_check_projection_input(
            tap_root=Path(args.tap_root),
            exact_head_root=(
                None if args.exact_head_root is None else Path(args.exact_head_root)
            ),
            context=context,
            applicable=not args.not_applicable,
            expected_request=expected_request,
            formula_requirements=requirements,
            now=args.now,
        )
        destination = Path(args.out).resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=destination.name + ".", delete=False
        ) as temporary:
            temporary.write(canonical_bytes(value))
            temporary_path = Path(temporary.name)
        temporary_path.replace(destination)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"abi-staging check projection: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
