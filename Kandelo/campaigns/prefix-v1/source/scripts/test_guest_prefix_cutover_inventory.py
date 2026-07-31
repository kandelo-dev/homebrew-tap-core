#!/usr/bin/env python3
"""Keep the audited prefix-cutover retention contract truthful."""

from __future__ import annotations

import hashlib
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "Kandelo/guest-prefix-cutover-inventory.json"
RUNBOOK = ROOT / "Kandelo/guest-prefix-cutover-runbook.md"


class GuestPrefixCutoverInventoryTests(unittest.TestCase):
    def test_source_inventory_and_historical_retention_are_exact(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            set(contract),
            {
                "final_catalog",
                "kind",
                "retained_historical_reports",
                "retired_prefix_sha256",
                "schema",
                "source_snapshot",
            },
        )
        self.assertEqual(contract["schema"], 1)
        # WHY: the final live-prefix guard must not gain another literal old
        # path merely because its dated inventory has a test. Construct the
        # historical needle while independently binding its reviewed digest.
        retired = b"/home/" + b"linuxbrew/.linuxbrew"
        self.assertEqual(
            hashlib.sha256(retired).hexdigest(),
            contract["retired_prefix_sha256"],
        )
        source = contract["source_snapshot"]
        self.assertEqual(
            set(source),
            {
                "byte_clean_reuse_variants",
                "formula_bottle_blocks_with_retired_prefix",
                "formula_sidecar_variants",
                "formula_sidecars",
                "formula_sidecars_with_retired_prefix",
                "historical_root_provenance_reports",
                "historical_root_provenance_reports_"
                "with_retired_prefix",
                "live_acceptance_configs_with_retired_prefix",
                "live_link_manifests",
                "live_link_manifests_with_retired_prefix",
                "required_replacement_variants",
                "schema_examples_with_retired_prefix",
                "selected_formulae",
                "selected_variants",
            },
        )

        formula_sidecars = sorted((ROOT / "Kandelo/formula").glob("*.json"))
        link_manifests = sorted((ROOT / "Kandelo/link").glob("*.json"))
        provenance = sorted(
            (ROOT / "Kandelo/reports").glob("*.provenance.json")
        )
        formulae = sorted((ROOT / "Formula").glob("*.rb"))
        selected = json.loads(
            (ROOT / "Kandelo/metadata.json").read_text(encoding="utf-8")
        )["packages"]

        self.assertEqual(len(formula_sidecars), source["formula_sidecars"])
        self.assertEqual(
            sum(
                len(json.loads(path.read_text(encoding="utf-8"))["bottles"])
                for path in formula_sidecars
            ),
            source["formula_sidecar_variants"],
        )
        self.assertEqual(
            sum(retired in path.read_bytes() for path in formula_sidecars),
            source["formula_sidecars_with_retired_prefix"],
        )
        self.assertEqual(len(link_manifests), source["live_link_manifests"])
        self.assertEqual(
            sum(retired in path.read_bytes() for path in link_manifests),
            source["live_link_manifests_with_retired_prefix"],
        )
        self.assertEqual(
            len(provenance),
            source["historical_root_provenance_reports"],
        )
        self.assertEqual(
            sum(retired in path.read_bytes() for path in provenance),
            source[
                "historical_root_provenance_reports_with_retired_prefix"
            ],
        )
        report_records = [
            {
                "bytes": len(path.read_bytes()),
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in provenance
        ]
        report_ledger = contract["retained_historical_reports"]
        self.assertEqual(
            set(report_ledger), {"bytes", "files", "records", "sha256"}
        )
        self.assertEqual(report_ledger["records"], report_records)
        self.assertEqual(report_ledger["files"], len(report_records))
        self.assertEqual(
            report_ledger["bytes"],
            sum(record["bytes"] for record in report_records),
        )
        self.assertEqual(
            report_ledger["sha256"],
            hashlib.sha256(
                json.dumps(
                    report_records,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(
            sum(retired in path.read_bytes() for path in formulae),
            source["formula_bottle_blocks_with_retired_prefix"],
        )
        self.assertEqual(len(selected), source["selected_formulae"])
        self.assertEqual(
            sum(len(package["bottles"]) for package in selected),
            source["selected_variants"],
        )

        acceptance = [
            ROOT / "Kandelo/vfs-acceptance.json",
            ROOT / "Kandelo/vfs-acceptance-shell.json",
        ]
        examples = sorted((ROOT / "Kandelo/examples").rglob("*.json"))
        self.assertEqual(
            sum(retired in path.read_bytes() for path in acceptance),
            source["live_acceptance_configs_with_retired_prefix"],
        )
        self.assertEqual(
            sum(retired in path.read_bytes() for path in examples),
            source["schema_examples_with_retired_prefix"],
        )

        final = contract["final_catalog"]
        self.assertEqual(
            set(final),
            {
                "formula_sidecars",
                "formula_sidecar_variants",
                "historical_root_provenance_reports",
                "live_link_manifests",
                "new_root_provenance_reports",
                "root_provenance_reports",
            },
        )
        self.assertEqual(
            source["byte_clean_reuse_variants"]
            + source["required_replacement_variants"],
            source["formula_sidecar_variants"],
        )
        self.assertEqual(
            final["formula_sidecars"],
            source["formula_sidecars"] + 2,
        )
        self.assertEqual(
            final["formula_sidecar_variants"],
            source["formula_sidecar_variants"] + 2,
        )
        self.assertEqual(
            final["live_link_manifests"],
            final["formula_sidecar_variants"],
        )
        self.assertEqual(
            final["new_root_provenance_reports"],
            source["required_replacement_variants"] + 2,
        )
        self.assertEqual(
            final["root_provenance_reports"],
            final["historical_root_provenance_reports"]
            + final["new_root_provenance_reports"],
        )
        self.assertEqual(
            final["historical_root_provenance_reports"],
            source["historical_root_provenance_reports"],
        )

    def test_runbook_does_not_reclassify_history_as_live_metadata(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(
            "preserve all 139 existing root-level provenance reports "
            "byte-for-byte",
            runbook,
        )
        self.assertNotIn(
            "71 root-level provenance reports",
            runbook,
        )
        earliest = runbook.split(
            "## Earliest Bootstrap-Critical Wave", 1
        )[1].split("## Complete Dependency-Ready Queue", 1)[0]
        self.assertLess(
            earliest.index("Build `homebrew-bootstrap` immediately"),
            earliest.index("In parallel, produce canonical-prefix"),
        )
        self.assertLess(
            earliest.index("build `libyaml`"),
            earliest.index("after Libyaml, build `ruby`"),
        )
        self.assertNotIn("libcxx/wasm32", earliest)
        self.assertNotIn("zlib/wasm32", earliest)
        self.assertNotIn("openssl/wasm32", earliest)
        self.assertNotIn("libcurl/wasm32", earliest)
        complete = runbook.split(
            "## Complete Dependency-Ready Queue", 1
        )[1].split(
            "The exact 22-Formula in-guest runtime-support closure", 1
        )[0]
        self.assertIn(
            "`homebrew-bootstrap` and `libyaml` are already ready at "
            "campaign start",
            complete,
        )
        self.assertNotIn("4. `homebrew-bootstrap`", complete)
        self.assertIn(
            "Keep every Formula's\nselected architectures in one task",
            complete,
        )

    def test_runbook_does_not_assign_formula_tests_to_reuse_tasks(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        reuse_policy = runbook.split(
            "Reuse is permitted only when", 1
        )[1].split("The required replacement identities are:", 1)[0]
        self.assertIn(
            "without rerunning the Formula's declared runtime\n  test",
            reuse_policy,
        )
        self.assertIn(
            "The 34 exact byte-clean ABI-42 reuse tasks\n"
            "do not rerun declared Formula tests",
            reuse_policy,
        )
        self.assertIn(
            "runs its declared\n"
            "test exactly once in the mandatory anonymous public-readback "
            "verifier",
            reuse_policy,
        )
        self.assertNotIn(
            "complete declared runtime test pass",
            reuse_policy,
        )

        handoff_policy = runbook.split(
            "### 3. Produce immutable handoffs without selecting them", 1
        )[1].split(
            "### 4. Publish exactly one final candidate", 1
        )[0]
        self.assertIn(
            "Reuse tasks do not rerun the\n"
            "declared Formula test",
            handoff_policy,
        )
        self.assertIn(
            "the build lane\n"
            "defers that test rather than duplicating it",
            handoff_policy,
        )
        self.assertNotIn(
            "run inspection, pour, test, and readback",
            handoff_policy,
        )


if __name__ == "__main__":
    unittest.main()
