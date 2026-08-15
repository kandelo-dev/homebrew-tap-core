from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
KANDELO_ROOT = Path(os.environ["KANDELO_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.policy import (
    VerificationTestDefinitionV1,
    load_tap_staging_policy,
)
from scripts.abi_staging.canonical import canonical_sha256
from scripts.abi_staging.reconcile import (
    PullRequestLifecycleV1,
    ReconciliationDecisionV1,
)
from scripts.abi_staging.scheduler import (
    AttemptFactV1,
    CandidateFactV1,
    ProtectedFailureFactsV1,
    SchedulingError,
    SchedulingRecordsV1,
    VerificationFactV1,
    classify_protected_failure,
    deterministic_retry_delay_ms,
    retry_decision,
    schedule_ready_batch,
)


FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json"
RETRY_VECTORS = (
    KANDELO_ROOT
    / "tools/xtask/tests/fixtures/abi-staging/retry-vectors.json"
)
POLICY = load_tap_staging_policy(TAP_ROOT / "Kandelo/staging/tap-policy.toml")
DEFINITION = VerificationTestDefinitionV1(
    id="bottle-structure",
    hosts=("build",),
    kandelo_paths=(
        "scripts/homebrew-inspect-bottle.py",
        "scripts/test-homebrew-inspect-bottle.sh",
    ),
    policy="kandelo-bottle-structure-v1",
    sha256=canonical_sha256(
        {
            "hosts": ["build"],
            "id": "bottle-structure",
            "kandelo_paths": [
                "scripts/homebrew-inspect-bottle.py",
                "scripts/test-homebrew-inspect-bottle.sh",
            ],
            "policy": "kandelo-bottle-structure-v1",
        }
    ),
)
NOW = "2026-08-09T10:00:00.000Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _plan() -> dict[str, object]:
    plan = json.loads(FIXTURE.read_bytes())
    for formula in plan["formulae"]:
        subject = json.dumps(
            {
                "architecture": formula["identity"]["architecture"],
                "identity": formula["identity"]["name"],
                "kind": "formula",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        formula["contract_sha256"] = _digest("contract:" + subject)
    return plan


def _decision(plan: dict[str, object], action: str = "observe-open") -> ReconciliationDecisionV1:
    head = "1" * 40
    state = "merged" if action == "observe-merged" else (
        "closed" if action == "stop-new-work" else "open"
    )
    lifecycle = PullRequestLifecycleV1(
        state,
        head,
        "2" * 40 if state == "merged" else None,
    )
    return ReconciliationDecisionV1(
        request_digest=plan["request_digest"],
        claim_key="sha256:" + plan["request_digest"],
        lifecycle=lifecycle,
        current_for_pull_request=action not in {"await-new-request"},
        action=action,
        permitted_work=(),
        blockers=(),
    )


def _formula(plan: dict[str, object], subject: str) -> dict[str, object]:
    identity = json.loads(subject)
    return next(
        item
        for item in plan["formulae"]
        if item["identity"]["name"] == identity["identity"]
        and item["identity"]["architecture"] == identity["architecture"]
    )


def _complete(
    plan: dict[str, object], subject: str, *, layer: str | None = None
) -> tuple[CandidateFactV1, VerificationFactV1]:
    contract = _formula(plan, subject)["contract_sha256"]
    candidate_digest = _digest("candidate:" + subject + ":" + contract)
    candidate = CandidateFactV1(
        request_sha256=plan["request_digest"],
        subject=subject,
        contract_sha256=contract,
        record_sha256=candidate_digest,
        bottle_layer_sha256=layer or _digest("layer:" + subject),
        descriptor_capable=True,
    )
    receipt = VerificationFactV1(
        request_sha256=plan["request_digest"],
        subject=subject,
        candidate_record_sha256=candidate_digest,
        test_definition_sha256=DEFINITION.sha256,
        host="build",
        outcome="success",
        guard_code=None,
        attempt_ordinal=0,
        completed_at="2026-08-09T09:00:00.000Z",
        record_sha256=_digest("receipt:" + candidate_digest),
    )
    return candidate, receipt


def _records(*facts: object) -> SchedulingRecordsV1:
    return SchedulingRecordsV1(
        attempts=tuple(item for item in facts if isinstance(item, AttemptFactV1)),
        candidates=tuple(item for item in facts if isinstance(item, CandidateFactV1)),
        verifications=tuple(
            item for item in facts if isinstance(item, VerificationFactV1)
        ),
    )


def _descriptor_capable(
    candidate: CandidateFactV1, capable: bool
) -> CandidateFactV1:
    return replace(candidate, descriptor_capable=capable)


class RetryVectorTests(unittest.TestCase):
    def test_python_matches_every_checked_rust_vector(self) -> None:
        fixture = json.loads(RETRY_VECTORS.read_bytes())
        self.assertEqual(fixture["kind"], "kandelo-abi-staging-retry-vectors")
        for vector in fixture["vectors"]:
            arguments = (
                vector["request_sha256"],
                vector["subject"],
                vector["retry_number"],
                vector["base_ms"],
                vector["cap_ms"],
            )
            with self.subTest(vector=vector["name"]):
                if vector["error"] is None:
                    self.assertEqual(
                        deterministic_retry_delay_ms(*arguments), vector["delay_ms"]
                    )
                else:
                    with self.assertRaisesRegex(
                        SchedulingError, "^" + vector["error"] + "$"
                    ):
                        deterministic_retry_delay_ms(*arguments)

    def test_retry_wait_ready_exhaustion_and_timeout_are_deterministic(self) -> None:
        request = "a" * 64
        subject = "formula:base:wasm32"
        wait = retry_decision(
            request,
            subject,
            current_ordinal=0,
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T10:00:00.000Z",
            now="2026-08-09T10:00:00.100Z",
            policy=POLICY,
        )
        ready = retry_decision(
            request,
            subject,
            current_ordinal=0,
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T10:00:00.000Z",
            now="2026-08-09T10:15:00.000Z",
            policy=POLICY,
        )
        self.assertEqual(wait.action, "wait")
        self.assertEqual(ready.action, "retry")
        self.assertEqual(wait.next_ordinal, 1)
        self.assertEqual(wait.next_eligible_at, ready.next_eligible_at)
        timeout = retry_decision(
            request,
            subject,
            current_ordinal=1,
            guard_code="verification_timeout",
            completed_at="2026-08-09T10:00:00.000Z",
            now="2026-08-09T10:15:00.000Z",
            policy=POLICY,
        )
        self.assertEqual(timeout.action, "retry")
        exhausted = retry_decision(
            request,
            subject,
            current_ordinal=3,
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T10:00:00.000Z",
            now="2026-08-09T10:15:00.000Z",
            policy=POLICY,
        )
        self.assertEqual(exhausted.action, "maintainer-action")
        self.assertTrue(exhausted.exhausted)
        application = retry_decision(
            request,
            subject,
            current_ordinal=0,
            guard_code="build_failed",
            completed_at="2026-08-09T10:00:00.000Z",
            now="2026-08-09T10:15:00.000Z",
            policy=POLICY,
        )
        self.assertEqual(application.action, "retry")
        self.assertEqual(application.next_ordinal, 1)


class FailureClassifierTests(unittest.TestCase):
    def test_only_protected_infrastructure_facts_classify_transient(self) -> None:
        transient = [
            ProtectedFailureFactsV1(
                "protected-workflow", "runner-lost", "cancelled", None, False, None
            ),
            ProtectedFailureFactsV1(
                "protected-workflow",
                "artifact-service-unavailable",
                "failure",
                None,
                False,
                None,
            ),
            ProtectedFailureFactsV1(
                "protected-workflow", "registry-http", "failure", 429, True, "success"
            ),
            ProtectedFailureFactsV1(
                "protected-workflow", "github-http", "failure", 502, False, None
            ),
            ProtectedFailureFactsV1(
                "protected-workflow", "transport-reset", "failure", None, False, None
            ),
        ]
        for facts in transient:
            with self.subTest(kind=facts.kind):
                result = classify_protected_failure(facts)
                self.assertEqual(result.guard_code, "transient_infrastructure_failure")
                self.assertTrue(result.automatic_retry)

        deterministic = {
            "formula-nonzero": "build_failed",
            "contract-failure": "build_input_capture_incomplete",
            "capture-failure": "build_input_capture_incomplete",
            "source-mismatch": "source_identity_mismatch",
            "archive-hazard": "candidate_integrity_mismatch",
            "digest-mismatch": "candidate_integrity_mismatch",
            "public-readback-failure": "candidate_public_readback_failed",
            "unknown": "build_failed",
        }
        for kind, guard in deterministic.items():
            with self.subTest(kind=kind):
                result = classify_protected_failure(
                    ProtectedFailureFactsV1(
                        "protected-workflow",
                        kind,
                        "failure",
                        None,
                        True,
                        "failure",
                    )
                )
                self.assertEqual(result.guard_code, guard)
                self.assertFalse(result.automatic_retry)

        verification = classify_protected_failure(
            ProtectedFailureFactsV1(
                "protected-workflow",
                "test-assertion",
                "failure",
                None,
                True,
                "failure",
            )
        )
        self.assertEqual(verification.guard_code, "verification_failed")
        self.assertTrue(verification.automatic_retry)

        reset = classify_protected_failure(
            ProtectedFailureFactsV1(
                "protected-workflow",
                "transport-reset",
                "failure",
                None,
                True,
                "failure",
            )
        )
        self.assertFalse(reset.automatic_retry)
        with self.assertRaises(SchedulingError):
            classify_protected_failure(
                ProtectedFailureFactsV1(
                    "candidate-output",
                    "runner-lost",
                    "failure",
                    None,
                    False,
                    None,
                )
            )


class SchedulingTests(unittest.TestCase):
    def test_duplicate_attempts_converge_with_deterministic_application_failure_dominance(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        contract = _formula(plan, subject)["contract_sha256"]
        transient = AttemptFactV1(
            request_sha256=plan["request_digest"],
            subject=subject,
            contract_sha256=contract,
            retry_ordinal=0,
            outcome="canceled",
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T09:00:00.000Z",
            record_sha256=_digest("duplicate-transient-build"),
        )
        application = replace(
            transient,
            outcome="failure",
            guard_code="build_failed",
            completed_at="2026-08-09T09:00:01.000Z",
            record_sha256=_digest("duplicate-application-build"),
        )
        decision = schedule_ready_batch(
            plan,
            _records(transient, application),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        retry = next(item for item in decision.ready if item.subject == subject)
        self.assertEqual(retry.action, "build-candidate")
        self.assertEqual(retry.attempt_ordinal, 1)

    def test_duplicate_verifiers_preserve_success_and_retry_failures(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        candidate, success = _complete(plan, subject)
        transient = replace(
            success,
            outcome="canceled",
            guard_code="transient_infrastructure_failure",
            record_sha256=_digest("duplicate-transient-verification"),
        )
        application = replace(
            transient,
            outcome="failure",
            guard_code="verification_failed",
            completed_at="2026-08-09T09:00:01.000Z",
            record_sha256=_digest("duplicate-application-verification"),
        )
        passed = schedule_ready_batch(
            plan,
            _records(candidate, transient, success),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertIn(subject, passed.complete)
        failed = schedule_ready_batch(
            plan,
            _records(candidate, transient, application),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        retry = next(item for item in failed.ready if item.subject == subject)
        self.assertEqual(retry.action, "verify-candidate")
        self.assertEqual(retry.attempt_ordinal, 1)

    def test_transient_verification_receipt_advances_the_bounded_retry_ordinal(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        candidate, _receipt = _complete(plan, subject)
        transient = VerificationFactV1(
            request_sha256=plan["request_digest"],
            subject=subject,
            candidate_record_sha256=candidate.record_sha256,
            test_definition_sha256=DEFINITION.sha256,
            host="build",
            outcome="canceled",
            guard_code="transient_infrastructure_failure",
            attempt_ordinal=0,
            completed_at="2026-08-09T09:00:00.000Z",
            record_sha256=_digest("transient-verification"),
        )
        decision = schedule_ready_batch(
            plan,
            _records(candidate, transient),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        intent = next(item for item in decision.ready if item.subject == subject)
        self.assertEqual(intent.action, "verify-candidate")
        self.assertEqual(intent.attempt_ordinal, 1)

    def test_verification_failure_advances_the_bounded_retry_ordinal(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        candidate, receipt = _complete(plan, subject)
        failed = replace(
            receipt,
            outcome="failure",
            guard_code="verification_failed",
            record_sha256=_digest("failed-verification"),
        )
        decision = schedule_ready_batch(
            plan,
            _records(candidate, failed),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        intent = next(item for item in decision.ready if item.subject == subject)
        self.assertEqual(intent.action, "verify-candidate")
        self.assertEqual(intent.attempt_ordinal, 1)

    def test_required_work_precedes_independent_background_and_batch_is_bounded(self) -> None:
        plan = _plan()
        decision = schedule_ready_batch(
            plan,
            _records(),
            _decision(plan),
            now=NOW,
            policy=replace(POLICY, max_ready_subjects_per_cycle=3),
            verification_tests=(DEFINITION,),
        )
        self.assertEqual(len(decision.ready), 3)
        self.assertTrue(all(item.work_class == "required" for item in decision.ready))
        self.assertEqual(
            [json.loads(item.subject)["identity"] for item in decision.ready],
            ["libcxx", "openssl", "zlib"],
        )
        repeated = schedule_ready_batch(
            plan,
            _records(),
            _decision(plan),
            now=NOW,
            policy=replace(POLICY, max_ready_subjects_per_cycle=3),
            verification_tests=(DEFINITION,),
        )
        self.assertEqual(decision, repeated)

    def test_dependency_failure_blocks_only_dependants_and_leaves_sibling_ready(self) -> None:
        plan = _plan()
        by_name = {
            json.loads(subject)["identity"] + ":" + json.loads(subject)["architecture"]: subject
            for subject in plan["required_subjects"] + plan["background_subjects"]
        }
        openssl = _complete(plan, by_name["openssl:wasm32"])
        zlib = _complete(plan, by_name["zlib:wasm32"])
        libcxx_subject = by_name["libcxx:wasm32"]
        failure = AttemptFactV1(
            request_sha256=plan["request_digest"],
            subject=libcxx_subject,
            contract_sha256=_formula(plan, libcxx_subject)["contract_sha256"],
            retry_ordinal=0,
            outcome="failure",
            guard_code="build_failed",
            completed_at="2026-08-09T09:00:00.000Z",
            record_sha256=_digest("failed-libcxx"),
        )
        decision = schedule_ready_batch(
            plan,
            _records(*openssl, *zlib, failure),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        ready_names = [json.loads(item.subject)["identity"] for item in decision.ready]
        self.assertEqual(ready_names[:2], ["libcxx", "libcurl"])
        blockers = {
            json.loads(item.subject)["identity"]: item.guard_code
            for item in decision.blocked
        }
        self.assertNotIn("libcxx", blockers)
        self.assertEqual(blockers["ncurses"], "dependency_unavailable")
        self.assertEqual(blockers["bash"], "dependency_unavailable")

    def test_candidate_verification_and_exact_contract_invalidation_are_staged(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        candidate, receipt = _complete(plan, subject)
        verify = schedule_ready_batch(
            plan,
            _records(candidate),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        intent = next(item for item in verify.ready if item.subject == subject)
        self.assertEqual(intent.action, "verify-candidate")
        self.assertEqual(intent.candidate_record_sha256, candidate.record_sha256)

        complete = schedule_ready_batch(
            plan,
            _records(candidate, receipt, candidate, receipt),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertIn(subject, complete.complete)

        changed = copy.deepcopy(plan)
        _formula(changed, subject)["contract_sha256"] = _digest("changed-contract")
        invalidated = schedule_ready_batch(
            changed,
            _records(candidate, receipt),
            _decision(changed),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        rebuild = next(item for item in invalidated.ready if item.subject == subject)
        self.assertEqual(rebuild.action, "build-candidate")

    def test_identical_historical_candidate_is_verified_then_explicitly_reused(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        candidate, receipt = _complete(plan, subject)
        historical_request = _digest("historical-request")
        candidate = replace(
            candidate,
            request_sha256=historical_request,
            binding_record_sha256=candidate.record_sha256,
        )
        receipt = replace(receipt, request_sha256=historical_request)

        needs_verification = schedule_ready_batch(
            plan,
            _records(candidate),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        verify = next(item for item in needs_verification.ready if item.subject == subject)
        self.assertEqual(verify.action, "verify-candidate")
        self.assertEqual(verify.candidate_record_sha256, candidate.record_sha256)

        reusable = schedule_ready_batch(
            plan,
            _records(candidate, receipt),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        reuse = next(item for item in reusable.ready if item.subject == subject)
        self.assertEqual(reuse.action, "reuse-candidate")
        self.assertEqual(reuse.candidate_record_sha256, candidate.record_sha256)

        reuse_binding = replace(
            candidate,
            request_sha256=plan["request_digest"],
            binding_record_sha256=_digest("reuse-binding"),
        )
        complete = schedule_ready_batch(
            plan,
            _records(candidate, reuse_binding, receipt),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertIn(subject, complete.complete)

    def test_historical_candidates_with_conflicting_layers_fail_closed(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        first, _ = _complete(plan, subject)
        first = replace(first, request_sha256=_digest("old-one"))
        second = replace(
            first,
            request_sha256=_digest("old-two"),
            record_sha256=_digest("second-candidate"),
            bottle_layer_sha256=_digest("different-layer"),
        )
        with self.assertRaisesRegex(
            SchedulingError, "conflicting candidate bottle layers"
        ):
            schedule_ready_batch(
                plan,
                _records(first, second),
                _decision(plan),
                now=NOW,
                policy=POLICY,
                verification_tests=(DEFINITION,),
            )

    def test_descriptor_candidate_wins_before_conflicts_and_legacy_only_rebuilds(self) -> None:
        plan = _plan()
        subjects = {
            json.loads(subject)["identity"]: subject
            for subject in plan["required_subjects"]
        }
        libcxx = subjects["libcxx"]
        openssl = subjects["openssl"]

        legacy_only, legacy_receipt = _complete(plan, libcxx)
        legacy_only = _descriptor_capable(legacy_only, False)

        current, _receipt = _complete(plan, openssl)
        current = _descriptor_capable(current, True)
        legacy_conflict = replace(
            current,
            record_sha256=_digest("legacy-openssl-candidate"),
            bottle_layer_sha256=_digest("legacy-openssl-layer"),
        )
        legacy_conflict = _descriptor_capable(legacy_conflict, False)

        decision = schedule_ready_batch(
            plan,
            _records(
                legacy_only,
                legacy_receipt,
                legacy_conflict,
                current,
            ),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        ready = {item.subject: item for item in decision.ready}
        self.assertEqual(ready[libcxx].action, "build-candidate")
        self.assertEqual(ready[openssl].action, "verify-candidate")
        self.assertEqual(
            ready[openssl].candidate_record_sha256,
            current.record_sha256,
        )
        self.assertNotIn(libcxx, decision.complete)

    def test_legacy_only_candidate_preserves_current_rebuild_retry_delay(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        legacy, _receipt = _complete(plan, subject)
        legacy = _descriptor_capable(legacy, False)
        attempt = AttemptFactV1(
            request_sha256=plan["request_digest"],
            subject=subject,
            contract_sha256=legacy.contract_sha256,
            retry_ordinal=0,
            outcome="failure",
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T10:00:00.000Z",
            record_sha256=_digest("legacy-rebuild-transient"),
        )

        waiting = schedule_ready_batch(
            plan,
            _records(legacy, attempt),
            _decision(plan),
            now="2026-08-09T10:00:00.001Z",
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        blocker = next(item for item in waiting.blocked if item.subject == subject)
        self.assertEqual(blocker.next_action, "wait")
        self.assertNotIn(subject, [item.subject for item in waiting.ready])

        eligible = schedule_ready_batch(
            plan,
            _records(legacy, attempt),
            _decision(plan),
            now="2026-08-09T10:15:00.000Z",
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        intent = next(item for item in eligible.ready if item.subject == subject)
        self.assertEqual(intent.action, "build-candidate")
        self.assertEqual(intent.attempt_ordinal, 1)

    def test_legacy_only_candidate_preserves_exhausted_rebuild_attempt(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        legacy, _receipt = _complete(plan, subject)
        legacy = _descriptor_capable(legacy, False)
        exhausted = AttemptFactV1(
            request_sha256=plan["request_digest"],
            subject=subject,
            contract_sha256=legacy.contract_sha256,
            retry_ordinal=3,
            outcome="failure",
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T09:00:00.000Z",
            record_sha256=_digest("legacy-rebuild-exhausted"),
        )

        decision = schedule_ready_batch(
            plan,
            _records(legacy, exhausted),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertNotIn(subject, [item.subject for item in decision.ready])
        blocker = next(item for item in decision.blocked if item.subject == subject)
        self.assertEqual(blocker.next_action, "maintainer-action")
        self.assertTrue(blocker.exhausted)

    def test_at_least_once_candidate_publication_converges_by_exact_layer(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        first, _ = _complete(plan, subject)
        duplicate = replace(first, record_sha256=_digest("duplicate-run"))
        decision = schedule_ready_batch(
            plan,
            _records(first, duplicate),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        intent = next(item for item in decision.ready if item.subject == subject)
        self.assertEqual(intent.action, "verify-candidate")
        self.assertEqual(
            intent.candidate_record_sha256,
            min(first.record_sha256, duplicate.record_sha256),
        )

        conflicting = replace(
            duplicate, bottle_layer_sha256=_digest("different-layer")
        )
        with self.assertRaises(SchedulingError):
            schedule_ready_batch(
                plan,
                _records(first, conflicting),
                _decision(plan),
                now=NOW,
                policy=POLICY,
                verification_tests=(DEFINITION,),
            )

    def test_reverse_dependants_invalidate_only_after_dependency_layer_change(self) -> None:
        plan = _plan()
        facts = [
            fact
            for subject in plan["required_subjects"]
            for fact in _complete(plan, subject)
        ]
        names = {
            json.loads(subject)["identity"]: subject
            for subject in plan["required_subjects"]
        }
        libcxx = names["libcxx"]
        old_candidate = next(
            item
            for item in facts
            if isinstance(item, CandidateFactV1) and item.subject == libcxx
        )
        old_receipt = next(
            item
            for item in facts
            if isinstance(item, VerificationFactV1) and item.subject == libcxx
        )
        facts.remove(old_candidate)
        facts.remove(old_receipt)
        same_layer_candidate = replace(
            old_candidate, record_sha256=_digest("replacement-producer")
        )
        same_layer_receipt = replace(
            old_receipt,
            candidate_record_sha256=same_layer_candidate.record_sha256,
            record_sha256=_digest("replacement-receipt"),
        )
        same_layer = schedule_ready_batch(
            plan,
            _records(*facts, same_layer_candidate, same_layer_receipt),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertFalse(
            any(item.work_class == "required" for item in same_layer.ready)
        )
        self.assertEqual(set(same_layer.complete), set(plan["required_subjects"]))

        changed = copy.deepcopy(plan)
        _formula(changed, names["ncurses"])["contract_sha256"] = _digest(
            "ncurses:new-libcxx-layer"
        )
        _formula(changed, names["bash"])["contract_sha256"] = _digest(
            "bash:new-ncurses-layer"
        )
        changed_layer_candidate = replace(
            same_layer_candidate,
            record_sha256=_digest("changed-layer-candidate"),
            bottle_layer_sha256=_digest("changed-libcxx-layer"),
        )
        changed_layer_receipt = replace(
            same_layer_receipt,
            candidate_record_sha256=changed_layer_candidate.record_sha256,
            record_sha256=_digest("changed-layer-receipt"),
        )
        invalidated = schedule_ready_batch(
            changed,
            _records(*facts, changed_layer_candidate, changed_layer_receipt),
            _decision(changed),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertEqual(
            [
                item.subject
                for item in invalidated.ready
                if item.work_class == "required"
            ],
            [names["ncurses"]],
        )
        self.assertIn(names["bash"], [item.subject for item in invalidated.blocked])
        self.assertIn(names["libcurl"], invalidated.complete)
        self.assertIn(names["curl"], invalidated.complete)

    def test_lifecycle_controls_work_without_commit_ordering(self) -> None:
        plan = _plan()
        closed = schedule_ready_batch(
            plan,
            _records(),
            _decision(plan, "stop-new-work"),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertEqual(closed.ready, ())

        merged = schedule_ready_batch(
            plan,
            _records(),
            _decision(plan, "observe-merged"),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        open_request = schedule_ready_batch(
            plan,
            _records(),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertEqual(merged.ready, open_request.ready)
        self.assertEqual(
            {item.work_class for item in merged.ready}, {"required", "background"}
        )

        subject = plan["required_subjects"][0]
        exhausted = AttemptFactV1(
            request_sha256=plan["request_digest"],
            subject=subject,
            contract_sha256=_formula(plan, subject)["contract_sha256"],
            retry_ordinal=3,
            outcome="failure",
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T09:00:00.000Z",
            record_sha256=_digest("exhausted"),
        )
        ordinary = schedule_ready_batch(
            plan,
            _records(exhausted),
            _decision(plan),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertNotIn(subject, [item.subject for item in ordinary.ready])
        reopened = schedule_ready_batch(
            plan,
            _records(exhausted),
            _decision(plan, "resume-same-head"),
            now=NOW,
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        self.assertNotIn(subject, [item.subject for item in reopened.ready])
        blocker = next(item for item in reopened.blocked if item.subject == subject)
        self.assertEqual(blocker.next_action, "maintainer-action")
        self.assertTrue(blocker.exhausted)

    def test_retry_wait_and_exhaustion_are_projected_without_sleeping(self) -> None:
        plan = _plan()
        subject = plan["required_subjects"][0]
        attempt = AttemptFactV1(
            request_sha256=plan["request_digest"],
            subject=subject,
            contract_sha256=_formula(plan, subject)["contract_sha256"],
            retry_ordinal=0,
            outcome="failure",
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T10:00:00.000Z",
            record_sha256=_digest("transient"),
        )
        waiting = schedule_ready_batch(
            plan,
            _records(attempt),
            _decision(plan),
            now="2026-08-09T10:00:00.001Z",
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        blocker = next(item for item in waiting.blocked if item.subject == subject)
        self.assertEqual(blocker.next_action, "wait")
        self.assertIsNotNone(blocker.next_eligible_at)
        ready = schedule_ready_batch(
            plan,
            _records(attempt),
            _decision(plan),
            now="2026-08-09T10:15:00.000Z",
            policy=POLICY,
            verification_tests=(DEFINITION,),
        )
        intent = next(item for item in ready.ready if item.subject == subject)
        self.assertEqual(intent.attempt_ordinal, 1)


if __name__ == "__main__":
    unittest.main()
