from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cleanup import (
    CleanupError,
    assess_retention_inventory,
    authorize_immediate_purge,
    build_cleanup_batch,
    build_deletion_oci_plan,
    build_deletion_plan,
    build_live_retention_inventory,
    classify_retention_reference,
    collect_live_retention_inventory,
    execute_cleanup_batch,
    execute_exact_deletion,
    GitHubPackageDeletionClientV1,
    GitHubRetentionInventoryClientV1,
    main,
    validate_cleanup_batch,
    validate_deletion_record,
    validate_retention_assessment,
)
from scripts.abi_staging.oci import HttpResponseV1


TAP_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
REQUEST_A = "a" * 64
REQUEST_B = "b" * 64
TARGET_A = "1" * 64
TARGET_B = "2" * 64
SOURCE = "3" * 64


def _repository(artifact_class: str, name: str = "bash") -> str:
    if artifact_class == "candidate":
        return (
            "ghcr.io/kandelo-dev/"
            f"homebrew-tap-core-abi-7-candidates/{name}"
        )
    return "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-source-custody"


def _target(
    digest: str = TARGET_A,
    *,
    artifact_class: str = "candidate",
    request_sha256: str = REQUEST_A,
    source_custody_digest: str | None = SOURCE,
    name: str = "bash",
) -> dict[str, object]:
    repository = _repository(artifact_class, name)
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-retention-target",
        "artifact_class": artifact_class,
        "target_digest": digest,
        "repository": repository,
        "immutable_reference": f"{repository}@sha256:{digest}",
        "record_kind": (
            "kandelo-abi-staging-candidate"
            if artifact_class == "candidate"
            else "kandelo-source-custody-manifest"
        ),
        "record_sha256": digest,
        "request_sha256": request_sha256,
        "source_custody_digest": (
            source_custody_digest if artifact_class == "candidate" else None
        ),
    }


def _lifecycle(
    state: str = "closed-unmerged",
    *,
    closed_at: str | None = "2026-07-11T12:00:00Z",
    request_sha256: str = REQUEST_A,
) -> dict[str, object]:
    return {
        "state": state,
        "closed_at": closed_at,
        "request_reference": (
            "https://github.com/Automattic/kandelo/releases/download/"
            "abi-staging-pr-19/"
            f"candidate-request-{'1' * 40}-sha256-{request_sha256}.json"
        ),
    }


def _reference(kind: str, digest: str = TARGET_A) -> dict[str, str]:
    record = kind.replace("-", "")[:1] or "f"
    record_digest = (record if record in "abcdef" else "f") * 64
    return {
        "kind": kind,
        "target_digest": digest,
        "record_sha256": record_digest,
        "immutable_reference": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-records/"
            f"{kind}@sha256:{record_digest}"
        ),
    }


def _assess(
    target: dict[str, object],
    *,
    lifecycle: dict[str, object] | None = None,
    references: tuple[dict[str, str], ...] = (),
    now: datetime = NOW,
) -> dict[str, object]:
    results = assess_retention_inventory(
        targets=(target,),
        lifecycles={target["request_sha256"]: lifecycle or _lifecycle()},
        references=references,
        now=now,
        grace_days=30,
    )
    return results[target["target_digest"]]


def _maintainer(permission: str = "maintain") -> dict[str, str]:
    return {
        "login": "maintainer",
        "permission": permission,
        "authorization_reference": (
            "https://github.com/kandelo-dev/homebrew-tap-core/"
            "actions/runs/17/attempts/1"
        ),
    }


class FakeRegistry:
    def __init__(
        self,
        target: dict[str, object],
        *,
        present: bool = True,
        delete_removes: bool = True,
        version_override: dict[str, object] | None = None,
    ) -> None:
        self.target = target
        self.present = present
        self.delete_removes = delete_removes
        self.version_override = version_override
        self.calls: list[str] = []

    def probe_anonymous(self, target: dict[str, object]) -> dict[str, object]:
        self.calls.append("probe")
        return {
            "status": 200 if self.present else 404,
            "url": target["immutable_reference"],
            "digest": (
                "sha256:" + str(target["target_digest"])
                if self.present
                else None
            ),
        }

    def resolve_exact_version(self, target: dict[str, object]) -> dict[str, object]:
        self.calls.append("resolve")
        return self.version_override or {
            "id": 91,
            "repository": target["repository"],
            "digest": "sha256:" + str(target["target_digest"]),
        }

    def delete_exact_version(self, version: dict[str, object]) -> None:
        self.calls.append("delete")
        if self.delete_removes:
            self.present = False


class FakePackageTransport:
    def __init__(self, target: dict[str, object], manifest: bytes) -> None:
        self.target = target
        self.manifest = manifest
        self.present = True
        self.requests: list[tuple[str, str, bool]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        authenticated: bool,
        maximum_bytes: int,
    ) -> HttpResponseV1:
        del headers, body, maximum_bytes
        self.requests.append((method, url, authenticated))
        if url.startswith("https://ghcr.io/v2/"):
            if self.present:
                return HttpResponseV1(
                    200,
                    {"docker-content-digest": "sha256:" + str(self.target["target_digest"])},
                    self.manifest,
                    url,
                )
            return HttpResponseV1(404, {}, b"", url)
        if "/versions?" in url:
            return HttpResponseV1(
                200,
                {},
                canonical_bytes(
                    [
                        {
                            "id": 91,
                            "name": "sha256:" + str(self.target["target_digest"]),
                        }
                    ]
                ),
                url,
            )
        if "/repos/Automattic/kandelo/pulls/19" in url:
            self.assert_authenticated(authenticated)
            return HttpResponseV1(
                200,
                {},
                canonical_bytes(
                    {
                        "state": "closed",
                        "closed_at": "2026-07-11T12:00:00Z",
                        "merged_at": None,
                    }
                ),
                url,
            )
        if method == "DELETE" and "/versions/91" in url:
            self.present = False
            return HttpResponseV1(204, {}, b"", url)
        return HttpResponseV1(
            200,
            {},
            canonical_bytes(
                {
                    "name": self.target["repository"].split("/", 2)[2],
                    "package_type": "container",
                    "visibility": "public",
                    "repository": {"full_name": "kandelo-dev/homebrew-tap-core"},
                }
            ),
            url,
        )


class FakeRetentionTransport:
    def __init__(
        self,
        package: str,
        record: dict[str, object],
        *,
        tags: list[str] | None = None,
    ) -> None:
        self.package = package
        self.config = canonical_bytes(record)
        config_digest = hashlib.sha256(self.config).hexdigest()
        self.manifest = canonical_bytes(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "artifactType": "application/vnd.kandelo.test-record.v1+json",
                "config": {
                    "mediaType": "application/vnd.kandelo.test-record.v1+json",
                    "digest": "sha256:" + config_digest,
                    "size": len(self.config),
                    "annotations": {
                        "dev.kandelo.abi-staging.role": "test-record",
                        "org.opencontainers.image.title": "test-record.json",
                    },
                },
                "layers": [],
                "annotations": {
                    "org.opencontainers.image.source": (
                        "https://github.com/kandelo-dev/homebrew-tap-core"
                    )
                },
            }
        )
        self.manifest_digest = hashlib.sha256(self.manifest).hexdigest()
        self.config_digest = config_digest
        self.tags = tags or ["record-sha256-" + self.manifest_digest]

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        authenticated: bool,
        maximum_bytes: int,
    ) -> HttpResponseV1:
        del method, headers, body, maximum_bytes
        if "/orgs/kandelo-dev/packages?" in url:
            self.assert_authenticated(authenticated)
            return HttpResponseV1(
                200,
                {},
                canonical_bytes(
                    [
                        {
                            "name": self.package,
                            "package_type": "container",
                            "visibility": "public",
                            "repository": {
                                "full_name": "kandelo-dev/homebrew-tap-core"
                            },
                        }
                    ]
                ),
                url,
            )
        if "/versions?" in url:
            self.assert_authenticated(authenticated)
            return HttpResponseV1(
                200,
                {},
                canonical_bytes(
                    [
                        {
                            "id": 91,
                            "name": "sha256:" + self.manifest_digest,
                            "metadata": {
                                "container": {
                                    "tags": self.tags
                                }
                            },
                        }
                    ]
                ),
                url,
            )
        if "/repos/Automattic/kandelo/pulls/19" in url:
            self.assert_authenticated(authenticated)
            return HttpResponseV1(
                200,
                {},
                canonical_bytes(
                    {
                        "state": "closed",
                        "closed_at": "2026-07-11T12:00:00Z",
                        "merged_at": None,
                    }
                ),
                url,
            )
        if "/manifests/sha256:" + self.manifest_digest in url:
            self.assert_anonymous(authenticated)
            return HttpResponseV1(
                200,
                {"docker-content-digest": "sha256:" + self.manifest_digest},
                self.manifest,
                url,
            )
        if "/blobs/sha256:" + self.config_digest in url:
            self.assert_anonymous(authenticated)
            return HttpResponseV1(200, {}, self.config, url)
        return HttpResponseV1(404, {}, b"", url)

    def assert_authenticated(self, authenticated: bool) -> None:
        if not authenticated:
            raise AssertionError("GitHub package inventory was not authenticated")

    def assert_anonymous(self, authenticated: bool) -> None:
        if authenticated:
            raise AssertionError("public OCI record was not read anonymously")


class FakeWorkflowArtifactTransport:
    def __init__(self, *, artifact_digest: str) -> None:
        self.artifact_digest = artifact_digest
        self.requests: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        authenticated: bool,
        maximum_bytes: int,
    ) -> HttpResponseV1:
        del headers, body, maximum_bytes
        if method != "GET" or not authenticated:
            raise AssertionError("workflow artifact facts were not read authentically")
        self.requests.append(url)
        if url.endswith("/actions/runs/17/attempts/2"):
            value = {
                "id": 17,
                "run_attempt": 2,
                "event": "schedule",
                "head_branch": "main",
                "head_sha": "6" * 40,
                "path": ".github/workflows/abi-staging-candidate-cleanup.yml",
                "status": "in_progress",
                "conclusion": None,
                "head_repository": {
                    "full_name": "kandelo-dev/homebrew-tap-core"
                },
            }
        elif url.endswith("/actions/artifacts/91"):
            value = {
                "id": 91,
                "name": "abi-staging-cleanup-plan-17-2",
                "expired": False,
                "digest": "sha256:" + self.artifact_digest,
                "size_in_bytes": 4096,
                "workflow_run": {"id": 17, "head_sha": "6" * 40},
            }
        else:
            raise AssertionError(f"unexpected workflow artifact URL: {url}")
        return HttpResponseV1(200, {}, canonical_bytes(value), url)


class RetentionAssessmentTests(unittest.TestCase):
    def test_all_durable_reference_classes_pin_a_candidate(self) -> None:
        pin_kinds = (
            "merged-admission",
            "active-verification",
            "active-product",
            "active-promotion",
            "active-repair",
            "candidate-reuse",
            "shared-custody",
            "canonical-layer",
        )
        for kind in pin_kinds:
            with self.subTest(kind=kind):
                assessment = _assess(
                    _target(), references=(_reference(kind),)
                )
                self.assertFalse(assessment["deletion_eligible"])
                self.assertEqual(
                    [pin["kind"] for pin in assessment["pins"]], [kind]
                )

    def test_open_merged_reopened_and_29_day_targets_are_retained(self) -> None:
        target = _target()
        open_assessment = _assess(
            target, lifecycle=_lifecycle("open", closed_at=None)
        )
        merged = _assess(target, lifecycle=_lifecycle("merged", closed_at=None))
        day_29 = _assess(
            target,
            lifecycle=_lifecycle(closed_at="2026-07-11T12:00:01Z"),
        )
        reopened = _assess(
            target, lifecycle=_lifecycle("open", closed_at=None)
        )
        self.assertEqual(open_assessment["reason"], "pinned")
        self.assertEqual(open_assessment["pins"][0]["kind"], "open-request")
        self.assertEqual(merged["reason"], "request-merged")
        self.assertEqual(reopened, open_assessment)
        self.assertEqual(day_29["reason"], "grace-incomplete")
        self.assertFalse(day_29["grace_complete"])

    def test_exact_30_days_is_eligible_and_historical_identity_does_not_pin(self) -> None:
        assessment = _assess(
            _target(), references=(_reference("historical-identity"),)
        )
        validate_retention_assessment(assessment)
        self.assertTrue(assessment["grace_complete"])
        self.assertTrue(assessment["deletion_eligible"])
        self.assertEqual(assessment["pins"], [])
        self.assertEqual(assessment["reason"], "unreferenced-grace-complete")

    def test_eligible_but_present_candidate_keeps_source_custody_pinned(self) -> None:
        source = _target(
            SOURCE,
            artifact_class="source",
            request_sha256=REQUEST_A,
            source_custody_digest=None,
        )
        candidate_a = _target(TARGET_A, request_sha256=REQUEST_A)
        assessed = assess_retention_inventory(
            targets=(candidate_a, source),
            lifecycles={REQUEST_A: _lifecycle()},
            references=(),
            now=NOW,
            grace_days=30,
        )
        self.assertTrue(assessed[TARGET_A]["deletion_eligible"])
        self.assertFalse(assessed[SOURCE]["deletion_eligible"])
        self.assertEqual(
            [pin["immutable_reference"] for pin in assessed[SOURCE]["pins"]],
            [candidate_a["immutable_reference"]],
        )

    def test_each_present_shared_candidate_keeps_source_custody_pinned(self) -> None:
        source = _target(
            SOURCE,
            artifact_class="source",
            request_sha256=REQUEST_A,
            source_custody_digest=None,
        )
        candidate_a = _target(TARGET_A, request_sha256=REQUEST_A)
        candidate_b = _target(
            TARGET_B,
            request_sha256=REQUEST_B,
            source_custody_digest=SOURCE,
            name="dash",
        )
        assessed = assess_retention_inventory(
            targets=(candidate_a, candidate_b, source),
            lifecycles={
                REQUEST_A: _lifecycle(),
                REQUEST_B: _lifecycle(request_sha256=REQUEST_B),
            },
            references=(),
            now=NOW,
            grace_days=30,
        )
        self.assertTrue(assessed[TARGET_A]["deletion_eligible"])
        self.assertTrue(assessed[TARGET_B]["deletion_eligible"])
        self.assertFalse(assessed[SOURCE]["deletion_eligible"])
        self.assertEqual(len(assessed[SOURCE]["pins"]), 2)

    def test_checked_fixture_is_the_generic_retention_contract(self) -> None:
        fixture = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/retention-assessment.json")
            .read_text(encoding="utf-8")
        )
        target = fixture.pop("target")
        expected = fixture.pop("assessment")
        self.assertEqual(fixture, {"schema": 1, "kind": "kandelo-retention-fixture"})
        self.assertEqual(_assess(target), expected)


class DeletionTests(unittest.TestCase):
    def _ordinary_plan(self) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        target = _target()
        assessment = _assess(target)
        plan = build_deletion_plan(
            target=target,
            assessment=assessment,
            mode="ordinary",
            reason_category="retention-expired",
            authorization=None,
            decision_time="2026-08-10T12:00:00Z",
        )
        return target, assessment, plan

    def test_deletes_one_exact_version_after_recheck_and_publishes_tombstone(self) -> None:
        target, assessment, plan = self._ordinary_plan()
        registry = FakeRegistry(target)
        order: list[str] = []
        published: list[dict[str, object]] = []

        def recheck() -> dict[str, object]:
            order.append("recheck")
            return assessment

        def publish(record: dict[str, object]) -> None:
            order.append("publish")
            published.append(record)

        record = execute_exact_deletion(
            plan,
            recheck=recheck,
            registry=registry,
            existing_tombstones=(),
            publish_tombstone=publish,
        )
        validate_deletion_record(record)
        self.assertEqual(order, ["recheck", "publish"])
        self.assertEqual(registry.calls, ["probe", "resolve", "delete", "probe"])
        self.assertEqual(published, [record])
        self.assertEqual(record["target"], target)

        oci = build_deletion_oci_plan(record)
        self.assertEqual(oci.repository, target["repository"][len("ghcr.io/") :] + "/deletions")
        self.assertEqual(
            oci.annotations["dev.kandelo.abi-staging.classification"],
            "immutable-deletion-tombstone",
        )

    def test_rejects_stale_recheck_wrong_version_and_unconfirmed_deletion(self) -> None:
        target, assessment, plan = self._ordinary_plan()
        stale = copy.deepcopy(assessment)
        stale["pins"] = [_reference("active-repair")]
        stale["deletion_eligible"] = False
        stale["reason"] = "pinned"
        stale["grace_complete"] = True
        registry = FakeRegistry(target)
        with self.assertRaises(CleanupError):
            execute_exact_deletion(
                plan,
                recheck=lambda: stale,
                registry=registry,
                existing_tombstones=(),
                publish_tombstone=lambda record: None,
            )
        self.assertEqual(registry.calls, [])

        changed_but_eligible = copy.deepcopy(assessment)
        changed_but_eligible["unreferenced_since"] = "2026-07-10T12:00:00Z"
        with self.assertRaisesRegex(CleanupError, "assessment changed"):
            execute_exact_deletion(
                plan,
                recheck=lambda: changed_but_eligible,
                registry=FakeRegistry(target),
                existing_tombstones=(),
                publish_tombstone=lambda record: None,
            )

        wrong = FakeRegistry(
            target,
            version_override={
                "id": 91,
                "repository": target["repository"] + "/other",
                "digest": "sha256:" + str(target["target_digest"]),
            },
        )
        with self.assertRaises(CleanupError):
            execute_exact_deletion(
                plan,
                recheck=lambda: assessment,
                registry=wrong,
                existing_tombstones=(),
                publish_tombstone=lambda record: None,
            )

        remains = FakeRegistry(target, delete_removes=False)
        with self.assertRaises(CleanupError):
            execute_exact_deletion(
                plan,
                recheck=lambda: assessment,
                registry=remains,
                existing_tombstones=(),
                publish_tombstone=lambda record: None,
            )

    def test_rejects_mutable_canonical_unknown_and_pinned_targets(self) -> None:
        target = _target()
        assessment = _assess(target)
        mutations = []
        mutable = copy.deepcopy(target)
        mutable["immutable_reference"] = mutable["repository"] + ":latest"
        mutations.append(mutable)
        canonical = copy.deepcopy(target)
        canonical["repository"] = canonical["repository"].replace(
            "-candidates", ""
        )
        canonical["immutable_reference"] = (
            canonical["repository"] + "@sha256:" + canonical["target_digest"]
        )
        mutations.append(canonical)
        unknown = copy.deepcopy(target)
        unknown["record_kind"] = "unknown-record"
        mutations.append(unknown)
        for changed in mutations:
            with self.subTest(target=changed):
                with self.assertRaises(CleanupError):
                    build_deletion_plan(
                        target=changed,
                        assessment=assessment,
                        mode="ordinary",
                        reason_category="retention-expired",
                        authorization=None,
                        decision_time="2026-08-10T12:00:00Z",
                    )

        pinned = _assess(target, references=(_reference("canonical-layer"),))
        with self.assertRaises(CleanupError):
            build_deletion_plan(
                target=target,
                assessment=pinned,
                mode="ordinary",
                reason_category="retention-expired",
                authorization=None,
                decision_time="2026-08-10T12:00:00Z",
            )

    def test_retry_after_absence_is_idempotent_and_conflicting_reason_fails(self) -> None:
        target, assessment, plan = self._ordinary_plan()
        first_registry = FakeRegistry(target)
        record = execute_exact_deletion(
            plan,
            recheck=lambda: assessment,
            registry=first_registry,
            existing_tombstones=(),
            publish_tombstone=lambda candidate: None,
        )
        absent = FakeRegistry(target, present=False)
        retried = execute_exact_deletion(
            plan,
            recheck=lambda: assessment,
            registry=absent,
            existing_tombstones=(record,),
            publish_tombstone=lambda candidate: None,
        )
        self.assertEqual(retried, record)
        self.assertEqual(absent.calls, ["probe"])

        conflict = copy.deepcopy(record)
        conflict["reason_category"] = "legal-removal"
        with self.assertRaises(CleanupError):
            execute_exact_deletion(
                plan,
                recheck=lambda: assessment,
                registry=FakeRegistry(target, present=False),
                existing_tombstones=(conflict,),
                publish_tombstone=lambda candidate: None,
            )

    def test_tombstone_must_match_the_exact_factual_target(self) -> None:
        target, assessment, plan = self._ordinary_plan()
        record = execute_exact_deletion(
            plan,
            recheck=lambda: assessment,
            registry=FakeRegistry(target),
            existing_tombstones=(),
            publish_tombstone=lambda candidate: None,
        )
        changed = copy.deepcopy(record)
        changed["target"]["target_digest"] = TARGET_B
        with self.assertRaises(CleanupError):
            validate_deletion_record(changed)

    def test_live_client_resolves_and_deletes_only_the_exact_package_version(self) -> None:
        manifest = canonical_bytes({"schemaVersion": 2, "test": "candidate"})
        target = _target(hashlib.sha256(manifest).hexdigest())
        transport = FakePackageTransport(target, manifest)
        client = GitHubPackageDeletionClientV1(
            expected_source_repository="kandelo-dev/homebrew-tap-core",
            transport=transport,
        )

        self.assertEqual(client.probe_anonymous(target)["status"], 200)
        version = client.resolve_exact_version(target)
        self.assertEqual(version["id"], 91)
        client.delete_exact_version(version)
        self.assertEqual(client.probe_anonymous(target)["status"], 404)
        self.assertTrue(any("%2Fbash/versions" in url for _, url, _ in transport.requests))
        self.assertEqual(
            [authenticated for _, url, authenticated in transport.requests if "ghcr.io/v2" in url],
            [False, False],
        )

    def test_live_client_rejects_redirected_anonymous_probe(self) -> None:
        manifest = canonical_bytes({"schemaVersion": 2, "test": "candidate"})
        target = _target(hashlib.sha256(manifest).hexdigest())
        transport = FakePackageTransport(target, manifest)
        original = transport.request

        def redirected(*args, **kwargs):
            response = original(*args, **kwargs)
            if str(args[1]).startswith("https://ghcr.io/v2/"):
                return HttpResponseV1(
                    response.status,
                    response.headers,
                    response.body,
                    "https://objects.example.invalid/redirected",
                )
            return response

        transport.request = redirected  # type: ignore[method-assign]
        client = GitHubPackageDeletionClientV1(
            expected_source_repository="kandelo-dev/homebrew-tap-core",
            transport=transport,
        )
        with self.assertRaises(CleanupError):
            client.probe_anonymous(target)

    def test_live_client_rejects_non_string_digest_after_exact_resolution(self) -> None:
        manifest = canonical_bytes({"schemaVersion": 2, "test": "candidate"})
        target = _target("1" * 64)
        transport = FakePackageTransport(target, manifest)
        client = GitHubPackageDeletionClientV1(
            expected_source_repository="kandelo-dev/homebrew-tap-core",
            transport=transport,
        )
        version = client.resolve_exact_version(target)
        version["digest"] = int("1" * 64)

        with self.assertRaises(CleanupError):
            client.delete_exact_version(version)


class ImmediatePurgeTests(unittest.TestCase):
    def test_authorizes_exact_unpinned_immediate_purge_with_bounded_reason(self) -> None:
        target = _target()
        assessment = _assess(
            target,
            lifecycle=_lifecycle(closed_at="2026-08-09T12:00:00Z"),
        )
        authorization = authorize_immediate_purge(
            target=target,
            assessment=assessment,
            reason_category="malicious-object",
            justification="candidate contains a confirmed malicious payload",
            maintainer=_maintainer(),
            authorized_at="2026-08-10T12:00:00Z",
        )
        plan = build_deletion_plan(
            target=target,
            assessment=assessment,
            mode="immediate-purge",
            reason_category="malicious-object",
            authorization=authorization,
            decision_time="2026-08-10T12:00:00Z",
        )
        self.assertEqual(plan["target"], target)
        self.assertEqual(plan["authorization_sha256"], canonical_sha256(authorization))

    def test_immediate_purge_rejects_reason_actor_pin_and_broad_target(self) -> None:
        target = _target()
        assessment = _assess(
            target,
            lifecycle=_lifecycle(closed_at="2026-08-09T12:00:00Z"),
        )
        cases = (
            {"reason_category": "because-I-said-so"},
            {"maintainer": _maintainer("read")},
            {"justification": " "},
            {"justification": "x" * 4097},
        )
        for overrides in cases:
            arguments = {
                "target": target,
                "assessment": assessment,
                "reason_category": "legal-removal",
                "justification": "bounded legal removal request",
                "maintainer": _maintainer(),
                "authorized_at": "2026-08-10T12:00:00Z",
                **overrides,
            }
            with self.subTest(overrides=overrides):
                with self.assertRaises(CleanupError):
                    authorize_immediate_purge(**arguments)

        pinned = _assess(target, references=(_reference("shared-custody"),))
        with self.assertRaises(CleanupError):
            authorize_immediate_purge(
                target=target,
                assessment=pinned,
                reason_category="pathological-size",
                justification="object exceeds the protected emergency size bound",
                maintainer=_maintainer(),
                authorized_at="2026-08-10T12:00:00Z",
            )

        broad = copy.deepcopy(target)
        broad["immutable_reference"] = broad["repository"]
        with self.assertRaises(CleanupError):
            authorize_immediate_purge(
                target=broad,
                assessment=assessment,
                reason_category="legal-removal",
                justification="bounded legal removal request",
                maintainer=_maintainer(),
                authorized_at="2026-08-10T12:00:00Z",
            )


class CleanupBatchTests(unittest.TestCase):
    def _inventory(self) -> dict[str, object]:
        return {
            "schema": 1,
            "kind": "kandelo-abi-staging-retention-inventory",
            "targets": [_target()],
            "lifecycles": {REQUEST_A: _lifecycle()},
            "references": [],
            "tombstones": [],
        }

    def test_builds_a_bounded_canonical_batch_bound_to_tap_and_inventory(self) -> None:
        inventory = self._inventory()
        tap_source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        batch = build_cleanup_batch(
            inventory=inventory,
            tap_source=tap_source,
            now=NOW,
            grace_days=30,
            batch_size=16,
            mode="ordinary",
            target_reference="",
            reason_category="retention-expired",
            justification="",
            maintainer=None,
        )
        self.assertEqual(validate_cleanup_batch(batch), batch)
        self.assertEqual(batch["tap_source"], tap_source)
        self.assertEqual(batch["inventory_sha256"], canonical_sha256(inventory))
        self.assertEqual(len(batch["plans"]), 1)
        self.assertEqual(batch["plans"][0]["target"]["target_digest"], TARGET_A)

        changed = copy.deepcopy(batch)
        changed["inventory_sha256"] = "9" * 64
        with self.assertRaises(CleanupError):
            validate_cleanup_batch(changed)

    def test_empty_public_inventory_is_a_successful_no_work_batch(self) -> None:
        inventory = {
            "schema": 1,
            "kind": "kandelo-abi-staging-retention-inventory",
            "targets": [],
            "lifecycles": {},
            "references": [],
            "tombstones": [],
        }
        batch = build_cleanup_batch(
            inventory=inventory,
            tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "4" * 40,
                "tree": "5" * 40,
            },
            now=NOW,
            grace_days=30,
            batch_size=16,
            mode="ordinary",
            target_reference="",
            reason_category="retention-expired",
            justification="",
            maintainer=None,
        )
        self.assertEqual(batch["plans"], [])
        self.assertEqual(validate_cleanup_batch(batch), batch)

    def test_immediate_batch_requires_one_exact_target_and_maintainer(self) -> None:
        inventory = self._inventory()
        target_reference = str(inventory["targets"][0]["immutable_reference"])
        batch = build_cleanup_batch(
            inventory=inventory,
            tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "4" * 40,
                "tree": "5" * 40,
            },
            now=NOW,
            grace_days=30,
            batch_size=16,
            mode="immediate-purge",
            target_reference=target_reference,
            reason_category="malicious-object",
            justification="confirmed malicious payload",
            maintainer=_maintainer(),
        )
        self.assertEqual(len(batch["plans"]), 1)
        self.assertEqual(batch["plans"][0]["mode"], "immediate-purge")
        for reference in ("", "*", target_reference + "-other"):
            with self.subTest(reference=reference):
                with self.assertRaises(CleanupError):
                    build_cleanup_batch(
                        inventory=inventory,
                        tap_source=batch["tap_source"],
                        now=NOW,
                        grace_days=30,
                        batch_size=16,
                        mode="immediate-purge",
                        target_reference=reference,
                        reason_category="malicious-object",
                        justification="confirmed malicious payload",
                        maintainer=_maintainer(),
                    )

    def test_direct_source_purge_fails_while_candidate_referent_is_present(self) -> None:
        candidate = _target()
        source = _target(
            SOURCE,
            artifact_class="source",
            request_sha256=REQUEST_A,
            source_custody_digest=None,
        )
        inventory = {
            "schema": 1,
            "kind": "kandelo-abi-staging-retention-inventory",
            "targets": [candidate, source],
            "lifecycles": {REQUEST_A: _lifecycle()},
            "references": [],
            "tombstones": [],
        }
        with self.assertRaisesRegex(CleanupError, "still pinned"):
            build_cleanup_batch(
                inventory=inventory,
                tap_source={
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "commit": "4" * 40,
                    "tree": "5" * 40,
                },
                now=NOW,
                grace_days=30,
                batch_size=16,
                mode="immediate-purge",
                target_reference=str(source["immutable_reference"]),
                reason_category="malicious-object",
                justification="remove one exact malicious source capsule",
                maintainer=_maintainer(),
            )

    def test_classifies_only_explicit_active_and_durable_reference_kinds(self) -> None:
        locator = {
            "repository": "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7/bash/admissions",
            "digest": "sha256:" + "6" * 64,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7/bash/admissions"
                "@sha256:" + "6" * 64
            ),
        }
        admission = {
            "schema": 1,
            "kind": "kandelo-abi-staging-admission",
            "common": {"request_sha256": REQUEST_A},
            "admission": {"candidate_record_sha256": TARGET_A},
        }
        reference = classify_retention_reference(
            record=admission,
            locator=locator,
            target_digest=TARGET_A,
            lifecycle_state="merged",
        )
        self.assertEqual(reference["kind"], "merged-admission")
        historical = copy.deepcopy(admission)
        historical["kind"] = "kandelo-abi-staging-attempt-outcome"
        self.assertEqual(
            classify_retention_reference(
                record=historical,
                locator=locator,
                target_digest=TARGET_A,
                lifecycle_state="closed-unmerged",
            )["kind"],
            "historical-identity",
        )
        unknown = copy.deepcopy(admission)
        unknown["kind"] = "unknown-record"
        with self.assertRaises(CleanupError):
            classify_retention_reference(
                record=unknown,
                locator=locator,
                target_digest=TARGET_A,
                lifecycle_state="open",
            )

    def test_builds_live_targets_and_pins_from_public_record_bytes(self) -> None:
        candidate_locator = {
            "repository": _repository("candidate"),
            "digest": "sha256:" + TARGET_A,
            "immutable_reference": _repository("candidate") + "@sha256:" + TARGET_A,
        }
        source_locator = {
            "repository": _repository("source"),
            "digest": "sha256:" + SOURCE,
            "immutable_reference": _repository("source") + "@sha256:" + SOURCE,
        }
        admission_locator = {
            "repository": "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7/bash/admissions",
            "digest": "sha256:" + "6" * 64,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7/bash/admissions"
                "@sha256:" + "6" * 64
            ),
        }
        records = [
            {
                "locator": candidate_locator,
                "record": {
                    "schema": 1,
                    "kind": "kandelo-abi-staging-candidate",
                    "common": {"request_sha256": REQUEST_A},
                    "candidate": {"source_custody_sha256": SOURCE},
                },
            },
            {
                "locator": source_locator,
                "record": {"schema": 1, "kind": "kandelo-source-custody-manifest"},
            },
            {
                "locator": admission_locator,
                "record": {
                    "schema": 1,
                    "kind": "kandelo-abi-staging-admission",
                    "common": {"request_sha256": REQUEST_A},
                    "admission": {"candidate_record_sha256": TARGET_A},
                },
            },
        ]
        inventory = build_live_retention_inventory(
            records=records,
            lifecycles={REQUEST_A: _lifecycle("merged", closed_at=None)},
            required_targets=(),
        )
        self.assertEqual(
            [candidate["target_digest"] for candidate in inventory["targets"]],
            [TARGET_A, SOURCE],
        )
        self.assertEqual(inventory["references"][0]["kind"], "merged-admission")
        assessments = assess_retention_inventory(
            targets=inventory["targets"],
            lifecycles=inventory["lifecycles"],
            references=inventory["references"],
            now=NOW,
            grace_days=30,
        )
        self.assertEqual(assessments[TARGET_A]["reason"], "pinned")
        self.assertEqual(assessments[SOURCE]["pins"][0]["kind"], "shared-custody")

    def test_candidate_tombstone_preserves_source_cleanup_identity(self) -> None:
        candidate = _target()
        assessment = _assess(candidate)
        plan = build_deletion_plan(
            target=candidate,
            assessment=assessment,
            mode="ordinary",
            reason_category="retention-expired",
            authorization=None,
            decision_time="2026-08-10T12:00:00Z",
        )
        tombstone = execute_exact_deletion(
            plan,
            recheck=lambda: assessment,
            registry=FakeRegistry(candidate),
            existing_tombstones=(),
            publish_tombstone=lambda record: None,
        )
        source_locator = {
            "repository": _repository("source"),
            "digest": "sha256:" + SOURCE,
            "immutable_reference": _repository("source") + "@sha256:" + SOURCE,
        }
        tombstone_digest = canonical_sha256(tombstone)
        tombstone_locator = {
            "repository": _repository("candidate") + "/deletions",
            "digest": "sha256:" + tombstone_digest,
            "immutable_reference": (
                _repository("candidate")
                + "/deletions@sha256:"
                + tombstone_digest
            ),
        }

        inventory = build_live_retention_inventory(
            records=(
                {
                    "locator": source_locator,
                    "record": {
                        "schema": 1,
                        "kind": "kandelo-source-custody-manifest",
                    },
                },
                {"locator": tombstone_locator, "record": tombstone},
            ),
            lifecycles={REQUEST_A: _lifecycle()},
            required_targets=(),
        )

        self.assertEqual(len(inventory["targets"]), 1)
        self.assertEqual(inventory["targets"][0]["target_digest"], SOURCE)
        self.assertEqual(inventory["targets"][0]["request_sha256"], REQUEST_A)
        self.assertTrue(
            assess_retention_inventory(
                targets=inventory["targets"],
                lifecycles=inventory["lifecycles"],
                references=inventory["references"],
                now=NOW,
                grace_days=30,
            )[SOURCE]["deletion_eligible"]
        )

    def test_missing_candidate_without_tombstone_cannot_release_source(self) -> None:
        source = _target(
            SOURCE,
            artifact_class="source",
            request_sha256=REQUEST_A,
            source_custody_digest=None,
        )
        source_locator = {
            "repository": _repository("source"),
            "digest": "sha256:" + SOURCE,
            "immutable_reference": _repository("source") + "@sha256:" + SOURCE,
        }
        inventory = build_live_retention_inventory(
            records=(
                {
                    "locator": source_locator,
                    "record": {
                        "schema": 1,
                        "kind": "kandelo-source-custody-manifest",
                    },
                },
            ),
            lifecycles={REQUEST_A: _lifecycle()},
            required_targets=(source,),
        )
        self.assertEqual(inventory["targets"], [])

    def test_live_collection_rechecks_request_preserved_only_by_a_tombstone(self) -> None:
        candidate = _target()
        assessment = _assess(candidate)
        plan = build_deletion_plan(
            target=candidate,
            assessment=assessment,
            mode="ordinary",
            reason_category="retention-expired",
            authorization=None,
            decision_time="2026-08-10T12:00:00Z",
        )
        tombstone = execute_exact_deletion(
            plan,
            recheck=lambda: assessment,
            registry=FakeRegistry(candidate),
            existing_tombstones=(),
            publish_tombstone=lambda record: None,
        )
        tombstone_digest = canonical_sha256(tombstone)
        records = (
            {
                "locator": {
                    "repository": _repository("source"),
                    "digest": "sha256:" + SOURCE,
                    "immutable_reference": (
                        _repository("source") + "@sha256:" + SOURCE
                    ),
                },
                "record": {
                    "schema": 1,
                    "kind": "kandelo-source-custody-manifest",
                },
            },
            {
                "locator": {
                    "repository": _repository("candidate") + "/deletions",
                    "digest": "sha256:" + tombstone_digest,
                    "immutable_reference": (
                        _repository("candidate")
                        + "/deletions@sha256:"
                        + tombstone_digest
                    ),
                },
                "record": tombstone,
            },
        )
        lifecycle_calls: list[tuple[str, int, str]] = []

        class InventoryClient:
            def __init__(self, **kwargs) -> None:
                del kwargs

            def scan_records(self):
                return records

            def pull_request_lifecycle(
                self, *, repository: str, number: int, request_reference: str
            ):
                lifecycle_calls.append((repository, number, request_reference))
                return _lifecycle()

        request = SimpleNamespace(
            request_digest=REQUEST_A,
            request={
                "pull_request": {
                    "repository": "Automattic/kandelo",
                    "number": 19,
                }
            },
            asset_url=_lifecycle()["request_reference"],
        )
        public = mock.MagicMock()
        public.scan.return_value = (request,)
        policy = SimpleNamespace(
            tap_repository="kandelo-dev/homebrew-tap-core",
            candidate_repository_prefix="homebrew-tap-core-abi-",
        )
        with (
            mock.patch(
                "scripts.abi_staging.cleanup.GitHubRetentionInventoryClientV1",
                InventoryClient,
            ),
            mock.patch(
                "scripts.abi_staging.cleanup.GitHubPublicClient",
                return_value=public,
            ),
            mock.patch(
                "scripts.abi_staging.cleanup.load_request_issuer_policy",
                return_value=object(),
            ),
        ):
            try:
                inventory = collect_live_retention_inventory(
                    tap_root=TAP_ROOT,
                    policy=policy,
                    repository="kandelo-dev/homebrew-tap-core",
                    username="maintainer",
                    token="test-token",
                )
            except CleanupError as error:
                self.fail(f"tombstoned request lifecycle was not rechecked: {error}")

        self.assertEqual(len(lifecycle_calls), 1)
        self.assertEqual(inventory["targets"][0]["target_digest"], SOURCE)

    def test_reappearing_tombstoned_source_remains_a_visible_conflict(self) -> None:
        source = _target(SOURCE, artifact_class="source")
        assessment = _assess(source)
        plan = build_deletion_plan(
            target=source,
            assessment=assessment,
            mode="ordinary",
            reason_category="retention-expired",
            authorization=None,
            decision_time="2026-08-10T12:00:00Z",
        )
        tombstone = execute_exact_deletion(
            plan,
            recheck=lambda: assessment,
            registry=FakeRegistry(source),
            existing_tombstones=(),
            publish_tombstone=lambda record: None,
        )
        tombstone_digest = canonical_sha256(tombstone)
        inventory = build_live_retention_inventory(
            records=(
                {
                    "locator": {
                        "repository": _repository("source"),
                        "digest": "sha256:" + SOURCE,
                        "immutable_reference": (
                            _repository("source") + "@sha256:" + SOURCE
                        ),
                    },
                    "record": {
                        "schema": 1,
                        "kind": "kandelo-source-custody-manifest",
                    },
                },
                {
                    "locator": {
                        "repository": _repository("source") + "/deletions",
                        "digest": "sha256:" + tombstone_digest,
                        "immutable_reference": (
                            _repository("source")
                            + "/deletions@sha256:"
                            + tombstone_digest
                        ),
                    },
                    "record": tombstone,
                },
            ),
            lifecycles={REQUEST_A: _lifecycle()},
            required_targets=(),
        )

        self.assertEqual(len(inventory["targets"]), 1)
        with self.assertRaisesRegex(CleanupError, "tombstoned cleanup target"):
            build_cleanup_batch(
                inventory=inventory,
                tap_source={
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "commit": "4" * 40,
                    "tree": "5" * 40,
                },
                now=NOW,
                grace_days=30,
                batch_size=16,
                mode="ordinary",
                target_reference="",
                reason_category="retention-expired",
                justification="",
                maintainer=None,
            )

    def test_shared_source_uses_the_least_advanced_tombstoned_request(self) -> None:
        def tombstone_entry(
            target: dict[str, object], assessment: dict[str, object], *, immediate: bool
        ) -> dict[str, object]:
            authorization = None
            mode = "ordinary"
            reason = "retention-expired"
            if immediate:
                mode = "immediate-purge"
                reason = "malicious-object"
                authorization = authorize_immediate_purge(
                    target=target,
                    assessment=assessment,
                    reason_category=reason,
                    justification="remove one exact malicious candidate",
                    maintainer=_maintainer(),
                    authorized_at="2026-08-10T12:00:00Z",
                )
            plan = build_deletion_plan(
                target=target,
                assessment=assessment,
                mode=mode,
                reason_category=reason,
                authorization=authorization,
                decision_time="2026-08-10T12:00:00Z",
            )
            record = execute_exact_deletion(
                plan,
                recheck=lambda: assessment,
                registry=FakeRegistry(target),
                existing_tombstones=(),
                publish_tombstone=lambda candidate: None,
            )
            digest = canonical_sha256(record)
            repository = str(target["repository"]) + "/deletions"
            return {
                "locator": {
                    "repository": repository,
                    "digest": "sha256:" + digest,
                    "immutable_reference": repository + "@sha256:" + digest,
                },
                "record": record,
            }

        candidate_a = _target()
        candidate_b = _target(
            TARGET_B,
            request_sha256=REQUEST_B,
            source_custody_digest=SOURCE,
            name="zsh",
        )
        old = _assess(candidate_a)
        recent = _assess(
            candidate_b,
            lifecycle=_lifecycle(
                closed_at="2026-07-12T12:00:00Z",
                request_sha256=REQUEST_B,
            ),
        )
        source_locator = {
            "repository": _repository("source"),
            "digest": "sha256:" + SOURCE,
            "immutable_reference": _repository("source") + "@sha256:" + SOURCE,
        }
        inventory = build_live_retention_inventory(
            records=(
                {
                    "locator": source_locator,
                    "record": {
                        "schema": 1,
                        "kind": "kandelo-source-custody-manifest",
                    },
                },
                tombstone_entry(candidate_a, old, immediate=False),
                tombstone_entry(candidate_b, recent, immediate=True),
            ),
            lifecycles={
                REQUEST_A: _lifecycle(),
                REQUEST_B: _lifecycle(
                    closed_at="2026-07-12T12:00:00Z",
                    request_sha256=REQUEST_B,
                ),
            },
            required_targets=(),
        )

        self.assertEqual(inventory["targets"][0]["request_sha256"], REQUEST_B)
        assessment = assess_retention_inventory(
            targets=inventory["targets"],
            lifecycles=inventory["lifecycles"],
            references=inventory["references"],
            now=NOW,
            grace_days=30,
        )[SOURCE]
        self.assertFalse(assessment["deletion_eligible"])
        self.assertEqual(assessment["reason"], "grace-incomplete")
    def test_executes_each_plan_against_one_fresh_inventory_snapshot(self) -> None:
        inventory = self._inventory()
        source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        batch = build_cleanup_batch(
            inventory=inventory,
            tap_source=source,
            now=NOW,
            grace_days=30,
            batch_size=16,
            mode="ordinary",
            target_reference="",
            reason_category="retention-expired",
            justification="",
            maintainer=None,
        )
        target = inventory["targets"][0]
        registry = FakeRegistry(target)
        collected: list[str] = []
        published: list[dict[str, object]] = []

        def collect(required_target):
            collected.append(required_target["target_digest"])
            return inventory

        result = execute_cleanup_batch(
            batch,
            current_tap_source=source,
            collect_inventory=collect,
            registry=registry,
            publish_tombstone=lambda record: published.append(record),
        )
        self.assertEqual(collected, [TARGET_A])
        self.assertEqual(result["records"], published)
        self.assertEqual(registry.calls, ["probe", "resolve", "delete", "probe"])

    def test_plan_live_writes_only_the_canonical_batch_and_outputs(self) -> None:
        inventory = self._inventory()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "out"
            github_output = Path(temporary) / "github-output"
            with (
                mock.patch(
                    "scripts.abi_staging.cleanup.collect_live_retention_inventory",
                    return_value=inventory,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.snapshot_tap_source",
                    return_value={
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "commit": "4" * 40,
                        "tree": "5" * 40,
                    },
                ),
                mock.patch.dict(
                    "os.environ",
                    {
                        "GITHUB_TOKEN": "test-token",
                        "HOMEBREW_GITHUB_PACKAGES_USER": "maintainer",
                        "HOMEBREW_GITHUB_PACKAGES_TOKEN": "test-token",
                    },
                    clear=False,
                ),
            ):
                status = main(
                    [
                        "plan-live",
                        "--tap-root",
                        str(TAP_ROOT),
                        "--repository",
                        "kandelo-dev/homebrew-tap-core",
                        "--mode",
                        "ordinary",
                        "--target-reference",
                        "",
                        "--reason-category",
                        "retention-expired",
                        "--justification",
                        "",
                        "--actor",
                        "maintainer",
                        "--authorization-reference",
                        "https://github.com/kandelo-dev/homebrew-tap-core/actions/runs/1/attempts/1",
                        "--verify-actor-permission",
                        "--enumerate-public-records",
                        "--recheck-lifecycle",
                        "--grace-days",
                        "30",
                        "--batch-size",
                        "16",
                        "--out",
                        str(output),
                        "--github-output",
                        str(github_output),
                    ]
                )
            self.assertEqual(status, 0)
            batch = json.loads((output / "plan.json").read_text(encoding="utf-8"))
            validate_cleanup_batch(batch)
            self.assertTrue((output / "plan.json").read_bytes().endswith(b"\n"))
            self.assertIn("has_work=true\n", github_output.read_text(encoding="utf-8"))
            self.assertIn("tap_commit=" + "4" * 40, github_output.read_text(encoding="utf-8"))

    def test_execute_live_rechecks_and_writes_a_canonical_result(self) -> None:
        inventory = self._inventory()
        source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        batch = build_cleanup_batch(
            inventory=inventory,
            tap_source=source,
            now=NOW,
            grace_days=30,
            batch_size=16,
            mode="ordinary",
            target_reference="",
            reason_category="retention-expired",
            justification="",
            maintainer=None,
        )
        target = inventory["targets"][0]
        registry = FakeRegistry(target)
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary) / "plan.json"
            plan.write_bytes(canonical_bytes(batch))
            output = Path(temporary) / "result"
            isolated = mock.MagicMock()
            isolated.return_value.__enter__.return_value = object()
            with (
                mock.patch(
                    "scripts.abi_staging.cleanup.snapshot_tap_source",
                    return_value=source,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.collect_live_retention_inventory",
                    return_value=inventory,
                ) as collect,
                mock.patch(
                    "scripts.abi_staging.cleanup.GitHubPackageDeletionClientV1",
                    return_value=registry,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.isolated_oras_transport",
                    isolated,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.UrllibOciTransportV1",
                    return_value=FakeWorkflowArtifactTransport(
                        artifact_digest="7" * 64
                    ),
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.publish_record", return_value=None
                ),
                mock.patch.dict(
                    "os.environ",
                    {
                        "GITHUB_ACTOR": "maintainer",
                        "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_RUN_ID": "17",
                        "GITHUB_SHA": "6" * 40,
                        "GITHUB_TOKEN": "test-token",
                        "GITHUB_WORKFLOW_REF": (
                            "kandelo-dev/homebrew-tap-core/.github/workflows/"
                            "abi-staging-candidate-cleanup.yml@refs/heads/main"
                        ),
                        "HOMEBREW_GITHUB_PACKAGES_USER": "maintainer",
                        "HOMEBREW_GITHUB_PACKAGES_TOKEN": "test-token",
                    },
                    clear=False,
                ),
            ):
                status = main(
                    [
                        "execute-live",
                        "--tap-root",
                        str(TAP_ROOT),
                        "--repository",
                        "kandelo-dev/homebrew-tap-core",
                        "--plan",
                        str(plan),
                        "--plan-artifact-id",
                        "91",
                        "--plan-artifact-digest",
                        "7" * 64,
                        "--recheck-live",
                        "--one-exact-version",
                        "--anonymous-absence",
                        "--immutable-tombstone",
                        "--batch-size",
                        "16",
                        "--out",
                        str(output),
                    ]
                )
            self.assertEqual(status, 0)
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["kind"], "kandelo-abi-staging-cleanup-result")
            self.assertEqual(result["handoff"]["artifact_id"], 91)
            self.assertEqual(result["handoff"]["artifact_digest"], "7" * 64)
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(collect.call_count, 1)

    def test_execute_live_rejects_a_plan_artifact_with_another_digest(self) -> None:
        inventory = self._inventory()
        source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        batch = build_cleanup_batch(
            inventory=inventory,
            tap_source=source,
            now=NOW,
            grace_days=30,
            batch_size=16,
            mode="ordinary",
            target_reference="",
            reason_category="retention-expired",
            justification="",
            maintainer=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary) / "plan.json"
            plan.write_bytes(canonical_bytes(batch))
            output = Path(temporary) / "result"
            isolated = mock.MagicMock()
            isolated.return_value.__enter__.return_value = object()
            with (
                mock.patch(
                    "scripts.abi_staging.cleanup.snapshot_tap_source",
                    return_value=source,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.collect_live_retention_inventory",
                    return_value=inventory,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.GitHubPackageDeletionClientV1",
                    return_value=FakeRegistry(inventory["targets"][0]),
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.isolated_oras_transport",
                    isolated,
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.UrllibOciTransportV1",
                    return_value=FakeWorkflowArtifactTransport(
                        artifact_digest="8" * 64
                    ),
                ),
                mock.patch(
                    "scripts.abi_staging.cleanup.publish_record", return_value=None
                ),
                mock.patch.dict(
                    "os.environ",
                    {
                        "GITHUB_ACTOR": "maintainer",
                        "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_RUN_ID": "17",
                        "GITHUB_SHA": "6" * 40,
                        "GITHUB_TOKEN": "test-token",
                        "GITHUB_WORKFLOW_REF": (
                            "kandelo-dev/homebrew-tap-core/.github/workflows/"
                            "abi-staging-candidate-cleanup.yml@refs/heads/main"
                        ),
                        "HOMEBREW_GITHUB_PACKAGES_USER": "maintainer",
                        "HOMEBREW_GITHUB_PACKAGES_TOKEN": "test-token",
                    },
                    clear=False,
                ),
            ):
                status = main(
                    [
                        "execute-live",
                        "--tap-root",
                        str(TAP_ROOT),
                        "--repository",
                        "kandelo-dev/homebrew-tap-core",
                        "--plan",
                        str(plan),
                        "--plan-artifact-id",
                        "91",
                        "--plan-artifact-digest",
                        "7" * 64,
                        "--recheck-live",
                        "--one-exact-version",
                        "--anonymous-absence",
                        "--immutable-tombstone",
                        "--batch-size",
                        "16",
                        "--out",
                        str(output),
                    ]
                )

            self.assertEqual(status, 1)
            self.assertFalse(output.exists())

    def test_public_inventory_client_reads_exact_record_versions_anonymously(self) -> None:
        package = "homebrew-tap-core-abi-7-candidates/bash"
        record = {
            "schema": 1,
            "kind": "kandelo-abi-staging-candidate",
            "common": {"request_sha256": REQUEST_A},
            "candidate": {"source_custody_sha256": SOURCE},
        }
        transport = FakeRetentionTransport(package, record)
        client = GitHubRetentionInventoryClientV1(
            expected_source_repository="kandelo-dev/homebrew-tap-core",
            package_prefix="homebrew-tap-core-abi-",
            transport=transport,
        )
        records = client.scan_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record"], record)
        self.assertEqual(
            records[0]["locator"]["digest"],
            "sha256:" + transport.manifest_digest,
        )
        lifecycle = client.pull_request_lifecycle(
            repository="Automattic/kandelo",
            number=19,
            request_reference=_lifecycle()["request_reference"],
        )
        self.assertEqual(lifecycle, _lifecycle())

    def test_public_inventory_normalizes_unordered_unique_api_tags(self) -> None:
        package = "homebrew-tap-core-abi-7-candidates/bash"
        record = {
            "schema": 1,
            "kind": "kandelo-abi-staging-candidate",
            "common": {"request_sha256": REQUEST_A},
            "candidate": {"source_custody_sha256": SOURCE},
        }
        transport = FakeRetentionTransport(package, record)
        transport.tags = [
            "record-sha256-" + transport.manifest_digest,
            "canonical-sha256-" + transport.manifest_digest,
        ]
        client = GitHubRetentionInventoryClientV1(
            expected_source_repository="kandelo-dev/homebrew-tap-core",
            package_prefix="homebrew-tap-core-abi-",
            transport=transport,
        )

        records = client.scan_records()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record"], record)

if __name__ == "__main__":
    unittest.main()
