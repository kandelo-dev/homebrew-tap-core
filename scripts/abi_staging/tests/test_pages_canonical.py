from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.abi_staging import cli as cli_module
from scripts.abi_staging.tests import test_promotion as promotion_tests
from scripts.abi_staging.tests import test_tap_metadata as tap_metadata_tests

try:
    from scripts.abi_staging import pages_canonical
except ImportError:
    PagesCanonicalSelectionV1 = None  # type: ignore[assignment,misc]
    build_pages_canonical_plan = None  # type: ignore[assignment]
    select_pages_canonical_candidate = None  # type: ignore[assignment]
else:
    PagesCanonicalSelectionV1 = getattr(
        pages_canonical,
        "PagesCanonicalSelectionV1",
        None,
    )
    build_pages_canonical_plan = getattr(
        pages_canonical,
        "build_pages_canonical_plan",
        None,
    )
    publish_pages_canonical_bottle = getattr(
        pages_canonical,
        "publish_pages_canonical_bottle",
        None,
    )
    read_pages_canonical_bottle = getattr(
        pages_canonical,
        "read_pages_canonical_bottle",
        None,
    )
    PagesCanonicalMetadataFactsV1 = getattr(
        pages_canonical,
        "PagesCanonicalMetadataFactsV1",
        None,
    )
    pages_canonical_metadata_facts = getattr(
        pages_canonical,
        "pages_canonical_metadata_facts",
        None,
    )
    prepare_pages_formula_metadata_patch = getattr(
        pages_canonical,
        "prepare_pages_formula_metadata_patch",
        None,
    )
    select_pages_canonical_candidate = getattr(
        pages_canonical,
        "select_pages_canonical_candidate",
        None,
    )


class PagesCanonicalSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = promotion_tests.PromotionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_selects_exact_candidate_without_promotion_evidence(self) -> None:
        self.assertIsNotNone(select_pages_canonical_candidate)
        assert select_pages_canonical_candidate is not None

        selected = select_pages_canonical_candidate(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
        )

        self.assertIsInstance(selected, PagesCanonicalSelectionV1)
        self.assertEqual(selected.formula, "bash")
        self.assertEqual(selected.architecture, "wasm32")
        self.assertEqual(selected.target_abi, promotion_tests.TARGET_ABI)
        self.assertEqual(
            selected.candidate_record_sha256,
            self.fixture.candidate_digest,
        )
        self.assertEqual(
            selected.bottle_sha256,
            self.fixture.bottle_sha256,
        )
        self.assertEqual(
            selected.bottle_bytes,
            len(self.fixture.bottle_body),
        )

    def test_builds_canonical_plan_without_a_receipt_or_admission(self) -> None:
        self.assertIsNotNone(build_pages_canonical_plan)
        assert build_pages_canonical_plan is not None

        plan = build_pages_canonical_plan(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
        )

        self.assertEqual(
            plan.repository,
            "kandelo-dev/homebrew-tap-core-abi-8/bash",
        )
        self.assertEqual(plan.layers[0].body, self.fixture.bottle_body)
        metadata = json.loads(plan.config.body)
        self.assertEqual(metadata["classification"], "canonical-direct")
        self.assertEqual(
            metadata["source"],
            self.fixture.candidate_record["common"]["source"],
        )
        self.assertNotIn("merged_pull_request", metadata)
        self.assertNotIn("receipt", json.dumps(metadata))
        self.assertNotIn("admission", json.dumps(metadata))

    def test_publishes_direct_canonical_object_with_anonymous_readback(self) -> None:
        self.assertIsNotNone(publish_pages_canonical_bottle)
        assert publish_pages_canonical_bottle is not None
        publication = publish_pages_canonical_bottle(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
            transport=self.fixture.transport,
        )

        self.assertEqual(
            publication.locator.repository,
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8/bash",
        )
        self.assertEqual(
            publication.locator.immutable_reference,
            publication.locator.repository + "@" + publication.locator.digest,
        )
        self.assertEqual(
            publication.artifact["sha256"],
            publication.locator.digest.removeprefix("sha256:"),
        )

    def test_reads_back_only_the_exact_direct_canonical_object(self) -> None:
        self.assertIsNotNone(read_pages_canonical_bottle)
        assert read_pages_canonical_bottle is not None
        published = publish_pages_canonical_bottle(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
            transport=self.fixture.transport,
        )

        observed = read_pages_canonical_bottle(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
            transport=self.fixture.transport,
        )

        self.assertEqual(observed, published)

    def test_plans_formula_metadata_without_an_admission_record(self) -> None:
        self.assertIsNotNone(PagesCanonicalMetadataFactsV1)
        self.assertIsNotNone(prepare_pages_formula_metadata_patch)
        assert PagesCanonicalMetadataFactsV1 is not None
        assert prepare_pages_formula_metadata_patch is not None
        fixture = tap_metadata_tests.TapMetadataTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        _history, _snapshot, preactivation, current = fixture._activate_fixture()
        prepared = fixture._prepared_admission(preactivation=preactivation)

        result = prepare_pages_formula_metadata_patch(
            tap_root=fixture.root,
            current_tap_source=current,
            expected_generated_metadata_sha256=fixture._generated_digest(),
            guest_layout_bytes=tap_metadata_tests._guest_layout_bytes(),
            policy=tap_metadata_tests.load_promotion_policy(
                fixture.root / "Kandelo/staging/promotion-policy.toml"
            ),
            facts=PagesCanonicalMetadataFactsV1(
                formula=prepared.candidate_formula,
                bottle_metadata=prepared.candidate_bottle_metadata,
                bottle_contract=prepared.candidate_bottle_contract,
                bottle_inventory=prepared.candidate_bottle_inventory,
                candidate_source=prepared.candidate_source,
                original_producer=prepared.original_producer,
                canonical=prepared.canonical,
                promoted_layer=prepared.promoted_layer,
            ),
        )

        self.assertEqual(result.update.formula, "bash")
        self.assertEqual(result.update.target_abi, tap_metadata_tests.SUCCESSOR_ABI)
        self.assertEqual(
            result.patch.allowed_paths,
            (
                "Formula/bash.rb",
                "Kandelo/formula/bash.json",
                "Kandelo/metadata.json",
                "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
            ),
        )
        self.assertIn("Formula/bash.rb", result.patch.files)

    def test_extracts_metadata_facts_from_candidate_and_canonical_only(self) -> None:
        self.assertIsNotNone(pages_canonical_metadata_facts)
        assert pages_canonical_metadata_facts is not None
        publication = publish_pages_canonical_bottle(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
            transport=self.fixture.transport,
        )

        facts = pages_canonical_metadata_facts(
            self.fixture.candidate,
            publication,
        )

        self.assertEqual(facts.formula["formula"], "bash")
        self.assertEqual(
            facts.promoted_layer["sha256"],
            self.fixture.bottle_sha256,
        )
        self.assertEqual(
            facts.canonical["sha256"],
            publication.artifact["sha256"],
        )
        self.assertTrue(
            str(facts.bottle_inventory["payload_root"]).startswith("bash/")
        )

    def test_cli_publishes_direct_canonical_without_evidence_inputs(self) -> None:
        out_path = self.fixture.root / "canonical.json"
        arguments = [
            "publish-pages-canonical",
            "--tap-root",
            str(promotion_tests.TAP_ROOT),
            "--candidate-reference",
            self.fixture.candidate.immutable_reference,
            "--formula",
            "bash",
            "--target-abi",
            str(promotion_tests.TARGET_ABI),
            "--anonymous-readback",
            "--out",
            str(out_path),
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "HOMEBREW_GITHUB_PACKAGES_TOKEN": "test-token",
                    "HOMEBREW_GITHUB_PACKAGES_USER": "test-user",
                },
                clear=False,
            ),
            patch.object(
                cli_module,
                "isolated_oras_transport",
                return_value=nullcontext(self.fixture.transport),
            ),
        ):
            try:
                status = cli_module.main(arguments)
            except SystemExit:
                status = -1

        self.assertEqual(status, 0)
        output = json.loads(out_path.read_bytes())
        self.assertEqual(output["kind"], "kandelo-pages-canonical-publication")
        self.assertEqual(output["formula"], "bash")
        self.assertNotIn("receipt", json.dumps(output))
        self.assertNotIn("admission", json.dumps(output))

    def test_cli_applies_direct_formula_metadata_without_package_credentials(
        self,
    ) -> None:
        fixture = tap_metadata_tests.TapMetadataTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        _history, _snapshot, preactivation, current = fixture._activate_fixture()
        admission = fixture._prepared_admission(preactivation=preactivation)
        prepared = prepare_pages_formula_metadata_patch(
            tap_root=fixture.root,
            current_tap_source=current,
            expected_generated_metadata_sha256=fixture._generated_digest(),
            guest_layout_bytes=tap_metadata_tests._guest_layout_bytes(),
            policy=tap_metadata_tests.load_promotion_policy(
                fixture.root / "Kandelo/staging/promotion-policy.toml"
            ),
            facts=PagesCanonicalMetadataFactsV1(
                formula=admission.candidate_formula,
                bottle_metadata=admission.candidate_bottle_metadata,
                bottle_contract=admission.candidate_bottle_contract,
                bottle_inventory=admission.candidate_bottle_inventory,
                candidate_source=admission.candidate_source,
                original_producer=admission.original_producer,
                canonical=admission.canonical,
                promoted_layer=admission.promoted_layer,
            ),
        )
        guest_layout_path = fixture.root / "guest-layout.json"
        guest_layout_path.write_bytes(tap_metadata_tests._guest_layout_bytes())
        out_path = fixture.root / "metadata-result.json"
        result = SimpleNamespace(
            status="committed",
            source=current,
            changed_paths=prepared.patch.allowed_paths,
        )
        selected = select_pages_canonical_candidate(
            self.fixture.candidate,
            tap_root=promotion_tests.TAP_ROOT,
            target_abi=promotion_tests.TARGET_ABI,
        )
        arguments = [
            "apply-pages-canonical-metadata",
            "--tap-root",
            str(fixture.root),
            "--candidate-reference",
            self.fixture.candidate.immutable_reference,
            "--formula",
            "bash",
            "--target-abi",
            str(promotion_tests.TARGET_ABI),
            "--guest-layout",
            str(guest_layout_path),
            "--anonymous-readback",
            "--contents-only",
            "--normal-push",
            "--out",
            str(out_path),
        ]
        with (
            patch.object(cli_module, "TAP_ROOT", fixture.root),
            patch.object(
                cli_module,
                "isolated_oras_transport",
                side_effect=AssertionError(
                    "contents-only metadata must not bootstrap registry credentials"
                ),
            ),
            patch.object(
                cli_module,
                "UrllibOciTransportV1",
                return_value=self.fixture.transport,
            ),
            patch.object(
                cli_module,
                "_fetch_candidate_record",
                return_value=self.fixture.candidate,
            ),
            patch.object(
                cli_module,
                "select_pages_canonical_candidate",
                return_value=selected,
            ),
            patch.object(
                cli_module,
                "read_pages_canonical_bottle",
                return_value=SimpleNamespace(artifact=admission.canonical),
                create=True,
            ),
            patch.object(
                cli_module,
                "pages_canonical_metadata_facts",
                return_value=SimpleNamespace(),
                create=True,
            ),
            patch.object(
                cli_module,
                "prepare_pages_formula_metadata_patch",
                return_value=prepared,
                create=True,
            ),
            patch.object(cli_module, "_configure_metadata_committer"),
            patch.object(cli_module, "GitTapMetadataStore"),
            patch.object(
                cli_module,
                "apply_metadata_patch",
                return_value=result,
            ) as apply,
            patch.object(cli_module, "check_tap_metadata"),
        ):
            with patch.dict(os.environ, {}, clear=True):
                try:
                    status = cli_module.main(arguments)
                except SystemExit:
                    status = -1

        self.assertEqual(status, 0)
        self.assertEqual(apply.call_count, 1)
        output = json.loads(out_path.read_bytes())
        self.assertEqual(output["kind"], "kandelo-pages-metadata-write")
        self.assertEqual(output["formula"], "bash")
        self.assertNotIn("receipt", json.dumps(output))
        self.assertNotIn("admission", json.dumps(output))

if __name__ == "__main__":
    unittest.main()
