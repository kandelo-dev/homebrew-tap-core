from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
import stat
from types import MappingProxyType

from .bottle_link import (
    BottleLinkError,
    build_link_manifest,
    inspect_bottle_link_inventory,
    link_manifest_bytes,
    load_guest_layout,
    validate_bottle_link_inventory,
)
from .canonical import canonical_bytes
from .contract import ContractError, captured_file_sha256, load_bottle_contract
from .execution import ExecutionError, normalize_candidate_bottle_metadata
from .formula_inventory import FormulaInventoryError, normalize_formula_source
from .oci import (
    FetchedOciRecordV1,
    OciPublicationError,
    OciTransportV1,
    PublishedRecordLocatorV1,
    build_oci_manifest,
    fetch_public_record,
    publish_immutable_oci_plan,
)
from .plan import bottle_metadata_formula_key, exact_formula_subject
from .promotion import (
    CanonicalBottleIdentityV1,
    CanonicalBottlePublicationV1,
    CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE,
    PreparedFormulaMetadataUpdateV1,
    PromotionError,
    _artifact,
    _canonical_publication_identity,
    _candidate_bottle_contract,
    _candidate_bottle_metadata,
    _candidate_layer,
    _digest,
    _exact,
    _nonnegative,
    _plain,
    _source,
    _stable_id,
    _text,
    _validated_fetched_record,
    build_canonical_bottle_plan_from_identity,
    canonical_repository,
)
from .records import (
    CANDIDATE_RECORD_MEDIA_TYPE,
    OciRecordPlanV1,
    TapRecordError,
    validate_candidate_record,
)
from .tap_metadata import (
    FormulaMetadataUpdateV1,
    PromotedBottleMetadataV1,
    PromotionPolicyV1,
    TapMetadataError,
    load_promotion_policy,
    plan_formula_metadata_patch,
)


class PagesCanonicalError(ValueError):
    """Raised when a candidate cannot be used by the direct Pages cutover."""


@dataclass(frozen=True)
class PagesCanonicalSelectionV1:
    formula: str
    architecture: str
    target_abi: int
    candidate_record_sha256: str
    bottle_sha256: str
    bottle_bytes: int


@dataclass(frozen=True)
class PagesCanonicalMetadataFactsV1:
    formula: Mapping[str, object]
    bottle_metadata: Mapping[str, object]
    bottle_contract: Mapping[str, object]
    bottle_inventory: Mapping[str, object]
    candidate_source: Mapping[str, object]
    original_producer: Mapping[str, object]
    canonical: Mapping[str, object]
    promoted_layer: Mapping[str, object]


def pages_canonical_metadata_facts(
    candidate: FetchedOciRecordV1,
    canonical_publication: CanonicalBottlePublicationV1,
) -> PagesCanonicalMetadataFactsV1:
    """Extract direct metadata inputs from one candidate and publication."""

    if not isinstance(canonical_publication, CanonicalBottlePublicationV1):
        raise PagesCanonicalError("Pages canonical publication is missing")
    try:
        record, layer = _candidate_layer(candidate)
        metadata_record, bottle_metadata, _metadata_layer = (
            _candidate_bottle_metadata(candidate)
        )
        contract_record, bottle_contract = _candidate_bottle_contract(candidate)
        if (
            canonical_bytes(metadata_record) != canonical_bytes(record)
            or canonical_bytes(contract_record) != canonical_bytes(record)
        ):
            raise PagesCanonicalError(
                "Pages candidate metadata changed its record identity"
            )
        formula = record["candidate"]["formula"]
        pkg_version = (
            formula["version"]
            if formula["revision"] == 0
            else f"{formula['version']}_{formula['revision']}"
        )
        inventory = inspect_bottle_link_inventory(
            layer.body,
            formula=formula["formula"],
            version=pkg_version,
        )
        return PagesCanonicalMetadataFactsV1(
            formula=MappingProxyType(_plain(formula)),
            bottle_metadata=MappingProxyType(_plain(bottle_metadata)),
            bottle_contract=MappingProxyType(_plain(bottle_contract)),
            bottle_inventory=MappingProxyType(_plain(inventory)),
            candidate_source=MappingProxyType(_plain(record["common"]["source"])),
            original_producer=MappingProxyType(
                _plain(record["candidate"]["producer"])
            ),
            canonical=MappingProxyType(
                _plain(canonical_publication.artifact)
            ),
            promoted_layer=MappingProxyType(
                _plain(record["candidate"]["bottle_layer"])
            ),
        )
    except (BottleLinkError, PromotionError) as error:
        raise PagesCanonicalError(
            f"Pages canonical metadata facts are invalid: {error}"
        ) from error


def select_pages_canonical_candidate(
    candidate: FetchedOciRecordV1,
    *,
    tap_root: Path,
    target_abi: int,
) -> PagesCanonicalSelectionV1:
    """Select one current Formula candidate without promotion evidence."""

    try:
        record, layers = _validated_fetched_record(
            candidate,
            artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            required_roles=("bottle-layer", "bottle-contract"),
            field="Pages candidate",
        )
        validate_candidate_record(record)
        contract = load_bottle_contract(layers["bottle-contract"].body)
        policy = load_promotion_policy(
            Path(tap_root).resolve(strict=True)
            / "Kandelo/staging/promotion-policy.toml"
        )
    except (ContractError, OSError, PromotionError, TapRecordError) as error:
        raise PagesCanonicalError(f"Pages candidate is invalid: {error}") from error

    payload = record["candidate"]
    formula = payload["formula"]
    name = formula["formula"]
    architecture = formula["architecture"]
    expected_repository = canonical_repository(
        policy,
        target_abi,
        name,
        candidate=True,
    )
    if (
        formula["target_abi"] != target_abi
        or formula["tap"].lower() != policy.tap_repository.lower()
        or candidate.repository != "ghcr.io/" + expected_repository
    ):
        raise PagesCanonicalError(
            "Pages candidate differs from the exact tap, Formula, or ABI"
        )

    bottle = payload["bottle_layer"]
    bottle_layer = layers["bottle-layer"]
    if (
        bottle["sha256"] != bottle_layer.digest.removeprefix("sha256:")
        or bottle["bytes"] != bottle_layer.size
    ):
        raise PagesCanonicalError("Pages candidate bottle identity changed")

    formula_path = Path(tap_root).resolve(strict=True) / f"Formula/{name}.rb"
    try:
        metadata = formula_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise PagesCanonicalError("Pages Formula source is not a regular file")
        current_formula_sha256 = hashlib.sha256(
            normalize_formula_source(formula_path.read_bytes())
        ).hexdigest()
    except (FormulaInventoryError, OSError) as error:
        raise PagesCanonicalError(f"Pages Formula source is invalid: {error}") from error

    source_components = {
        component["id"]: component["sha256"]
        for component in contract["formula"]["source_components"]
    }
    if (
        contract["target"]["abi"] != target_abi
        or contract["target"]["architecture"] != architecture
        or contract["formula"]["name"] != name
        or contract["formula"]["version"] != formula["version"]
        or contract["formula"]["revision"] != formula["revision"]
        or contract["formula"]["rebuild"] != formula["bottle_rebuild"]
        or source_components.get("formula") != current_formula_sha256
    ):
        raise PagesCanonicalError(
            "Pages candidate differs from the current Formula source identity"
        )

    return PagesCanonicalSelectionV1(
        formula=name,
        architecture=architecture,
        target_abi=target_abi,
        candidate_record_sha256=candidate.digest.removeprefix("sha256:"),
        bottle_sha256=bottle["sha256"],
        bottle_bytes=bottle["bytes"],
    )


def build_pages_canonical_plan(
    candidate: FetchedOciRecordV1,
    *,
    tap_root: Path,
    target_abi: int,
) -> OciRecordPlanV1:
    """Build one canonical OCI plan without trust-receipt prerequisites."""

    selected = select_pages_canonical_candidate(
        candidate,
        tap_root=tap_root,
        target_abi=target_abi,
    )
    try:
        record, _layers = _validated_fetched_record(
            candidate,
            artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            required_roles=("bottle-layer", "bottle-contract"),
            field="Pages candidate",
        )
        policy = load_promotion_policy(
            Path(tap_root).resolve(strict=True)
            / "Kandelo/staging/promotion-policy.toml"
        )
        return build_canonical_bottle_plan_from_identity(
            CanonicalBottleIdentityV1(
                request_digest=record["common"]["request_sha256"],
                formula_subject=exact_formula_subject(
                    selected.formula,
                    selected.architecture,
                ),
                candidate_record_digest=selected.candidate_record_sha256,
                bottle_layer_sha256=selected.bottle_sha256,
                bottle_layer_bytes=selected.bottle_bytes,
                classification="canonical-direct",
                source=record["common"]["source"],
            ),
            candidate=candidate,
            policy=policy,
        )
    except (OSError, PromotionError) as error:
        raise PagesCanonicalError(
            f"Pages canonical plan is invalid: {error}"
        ) from error


def publish_pages_canonical_bottle(
    candidate: FetchedOciRecordV1,
    *,
    tap_root: Path,
    target_abi: int,
    transport: OciTransportV1,
) -> CanonicalBottlePublicationV1:
    """Publish and anonymously read back one direct canonical bottle."""

    plan = build_pages_canonical_plan(
        candidate,
        tap_root=tap_root,
        target_abi=target_abi,
    )
    policy = load_promotion_policy(
        Path(tap_root).resolve(strict=True)
        / "Kandelo/staging/promotion-policy.toml"
    )
    try:
        locator = publish_immutable_oci_plan(
            plan,
            transport=transport,
            expected_source_repository=policy.tap_repository,
            tag_prefix="canonical-sha256-",
        )
    except OciPublicationError as error:
        raise PagesCanonicalError(
            f"Pages canonical publication failed: {error}"
        ) from error
    expected = _canonical_publication_identity(plan)
    if (
        locator.repository != expected["repository"]
        or locator.digest != expected["digest"]
        or locator.immutable_reference != expected["immutable_reference"]
        or locator.anonymous_readback_sha256
        != expected["anonymous_readback_sha256"]
    ):
        raise PagesCanonicalError(
            "Pages canonical publication lacks exact anonymous readback"
        )
    return CanonicalBottlePublicationV1(
        locator=locator,
        artifact=expected["artifact"],
    )


def read_pages_canonical_bottle(
    candidate: FetchedOciRecordV1,
    *,
    tap_root: Path,
    target_abi: int,
    transport: OciTransportV1,
) -> CanonicalBottlePublicationV1:
    """Read the one direct canonical object derived from a candidate."""

    try:
        plan = build_pages_canonical_plan(
            candidate,
            tap_root=tap_root,
            target_abi=target_abi,
        )
        expected = _canonical_publication_identity(plan)
        fetched = fetch_public_record(
            {
                "repository": expected["repository"],
                "digest": expected["digest"],
                "immutable_reference": expected["immutable_reference"],
            },
            transport=transport,
            expected_artifact_type=CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE,
            required_layer_roles=tuple(layer.role for layer in plan.layers),
        )
        if (
            fetched.manifest != build_oci_manifest(plan)
            or fetched.config.body != plan.config.body
            or len(fetched.layers) != len(plan.layers)
            or any(
                fetched_layer.body != planned_layer.body
                for fetched_layer, planned_layer in zip(
                    fetched.layers,
                    plan.layers,
                    strict=True,
                )
            )
        ):
            raise PagesCanonicalError(
                "Pages canonical readback differs from the direct plan"
            )
        return CanonicalBottlePublicationV1(
            locator=PublishedRecordLocatorV1(
                repository=expected["repository"],
                digest=expected["digest"],
                immutable_reference=expected["immutable_reference"],
                anonymous_readback_sha256=expected[
                    "anonymous_readback_sha256"
                ],
            ),
            artifact=MappingProxyType(_plain(expected["artifact"])),
        )
    except (OciPublicationError, PromotionError) as error:
        raise PagesCanonicalError(
            f"Pages canonical readback is invalid: {error}"
        ) from error


def prepare_pages_formula_metadata_patch(
    *,
    tap_root: Path,
    current_tap_source: Mapping[str, object],
    expected_generated_metadata_sha256: str,
    guest_layout_bytes: bytes,
    policy: PromotionPolicyV1,
    facts: PagesCanonicalMetadataFactsV1,
) -> PreparedFormulaMetadataUpdateV1:
    """Plan Formula metadata without an admission or trust receipt."""

    if not isinstance(facts, PagesCanonicalMetadataFactsV1):
        raise PagesCanonicalError("Pages canonical metadata facts are missing")
    try:
        current = _source(current_tap_source, "current Pages tap source")
        formula = _exact(
            facts.formula,
            frozenset(
                {
                    "tap",
                    "formula",
                    "version",
                    "revision",
                    "bottle_rebuild",
                    "architecture",
                    "target_abi",
                    "bottle_contract_sha256",
                }
            ),
            "Pages candidate Formula",
        )
        name = _stable_id(formula["formula"], "Pages metadata Formula")
        architecture = formula["architecture"]
        target_abi = _nonnegative(
            formula["target_abi"],
            "Pages metadata target ABI",
        )
        version = _text(formula["version"], "Pages Formula version", 256)
        revision = _nonnegative(formula["revision"], "Pages Formula revision")
        rebuild = _nonnegative(
            formula["bottle_rebuild"],
            "Pages Formula rebuild",
        )
        contract = load_bottle_contract(
            canonical_bytes(_plain(facts.bottle_contract))
        )
        contract_digest = hashlib.sha256(canonical_bytes(contract)).hexdigest()
        source_components = {
            component["id"]: component["sha256"]
            for component in contract["formula"]["source_components"]
        }
        root = Path(tap_root).resolve(strict=True)
        normalized = hashlib.sha256(
            normalize_formula_source((root / f"Formula/{name}.rb").read_bytes())
        ).hexdigest()
        if (
            architecture not in {"wasm32", "wasm64"}
            or formula["tap"].lower() != policy.tap_repository.lower()
            or contract_digest != formula["bottle_contract_sha256"]
            or contract["target"]["abi"] != target_abi
            or contract["target"]["architecture"] != architecture
            or contract["formula"]["name"] != name
            or contract["formula"]["version"] != version
            or contract["formula"]["revision"] != revision
            or contract["formula"]["rebuild"] != rebuild
            or source_components.get("formula") != normalized
        ):
            raise PagesCanonicalError(
                "Pages metadata differs from the current Formula contract"
            )

        guest_inputs = [
            component
            for component in contract["kandelo_inputs"]
            if component["path"] == "homebrew/kandelo-guest-layout.json"
        ]
        if (
            len(guest_inputs) != 1
            or guest_inputs[0]["kind"] != "file"
            or captured_file_sha256(guest_layout_bytes, executable=False)
            != guest_inputs[0]["sha256"]
        ):
            raise PagesCanonicalError(
                "Pages guest layout differs from the Formula contract"
            )
        guest_layout = load_guest_layout(guest_layout_bytes)
        canonical = _artifact(facts.canonical, "Pages canonical bottle")
        layer = _artifact(facts.promoted_layer, "Pages promoted bottle layer")
        normalized_metadata, _, _ = normalize_candidate_bottle_metadata(
            {
                "formula": name,
                "tap_repository": policy.tap_repository,
                "target_abi": target_abi,
                "architecture": architecture,
                "bottle_layer": layer,
            },
            facts.bottle_metadata,
        )
        metadata_key = bottle_metadata_formula_key(policy.tap_repository, name)
        metadata = _exact(
            normalized_metadata,
            frozenset({metadata_key}),
            "Pages Homebrew bottle metadata",
        )
        entry = _exact(
            metadata[metadata_key],
            frozenset({"formula", "bottle"}),
            "Pages Homebrew bottle entry",
        )
        bottle_metadata = _exact(
            entry["bottle"],
            frozenset({"root_url", "cellar", "rebuild", "tags"}),
            "Pages Homebrew bottle projection",
        )
        tags = _exact(
            bottle_metadata["tags"],
            frozenset({f"{architecture}_kandelo"}),
            "Pages Homebrew bottle tags",
        )
        tag = _exact(
            tags[f"{architecture}_kandelo"],
            frozenset({"sha256"}),
            "Pages Homebrew bottle tag",
        )
        candidate_repository = canonical_repository(
            policy,
            target_abi,
            name,
            candidate=True,
        )
        canonical_repository_name = canonical_repository(
            policy,
            target_abi,
            name,
        )
        if (
            bottle_metadata["root_url"]
            != "https://ghcr.io/v2/" + candidate_repository
            or _nonnegative(bottle_metadata["rebuild"], "Pages bottle rebuild")
            != rebuild
            or _digest(tag["sha256"], "Pages bottle layer")
            != layer["sha256"]
            or canonical["immutable_reference"]
            != "ghcr.io/"
            + canonical_repository_name
            + "@sha256:"
            + canonical["sha256"]
        ):
            raise PagesCanonicalError(
                "Pages metadata differs from the canonical bottle"
            )
        pkg_version = version if revision == 0 else f"{version}_{revision}"
        inventory = validate_bottle_link_inventory(
            facts.bottle_inventory,
            formula=name,
            version=pkg_version,
        )
        link_manifest = build_link_manifest(
            inventory=inventory,
            guest_layout=guest_layout,
            formula=name,
            version=pkg_version,
            architecture=architecture,
            target_abi=target_abi,
            bottle_url=(
                "https://ghcr.io/v2/"
                + canonical_repository_name
                + "/blobs/sha256:"
                + layer["sha256"]
            ),
            bottle_sha256=layer["sha256"],
            bottle_bytes=layer["bytes"],
        )
        link_path = (
            f"Kandelo/link/{name}-{pkg_version}-"
            f"rebuild{rebuild}-{architecture}.json"
        )
        source = _source(facts.candidate_source, "Pages candidate source")
        producer = _exact(
            facts.original_producer,
            frozenset({"request_sha256", "head", "run_id"}),
            "Pages candidate producer",
        )
        update = FormulaMetadataUpdateV1(
            formula=name,
            architecture=architecture,
            expected_main_commit=current["commit"],
            expected_normalized_formula_sha256=normalized,
            expected_generated_metadata_sha256=_digest(
                expected_generated_metadata_sha256,
                "Pages generated Formula metadata",
            ),
            allowed_paths=(
                f"Formula/{name}.rb",
                f"Kandelo/formula/{name}.json",
                "Kandelo/metadata.json",
                link_path,
            ),
            link_manifest_path=link_path,
            link_manifest_sha256=hashlib.sha256(
                link_manifest_bytes(link_manifest)
            ).hexdigest(),
            canonical_manifest_digest=canonical["sha256"],
            bottle_layer_sha256=layer["sha256"],
            bottle_layer_bytes=layer["bytes"],
            target_abi=target_abi,
        )
        promoted = PromotedBottleMetadataV1(
            formula=name,
            architecture=architecture,
            version=version,
            revision=revision,
            rebuild=rebuild,
            canonical_root_url="https://ghcr.io/v2/" + canonical_repository_name,
            cellar=str(guest_layout["cellar"]),
            built_by=(
                "https://github.com/"
                + policy.tap_repository
                + "/actions/runs/"
                + str(producer["run_id"])
            ),
            built_from=MappingProxyType(
                {
                    "formula_sha256": normalized,
                    "kandelo_commit": source["commit"],
                    "kandelo_repository": source["repository"],
                    "tap_commit": current["commit"],
                    "tap_repository": current["repository"],
                }
            ),
            link_manifest=MappingProxyType(_plain(link_manifest)),
        )
        patch = plan_formula_metadata_patch(
            root,
            current_tap_source=current,
            update=update,
            promoted=promoted,
        )
        return PreparedFormulaMetadataUpdateV1(update=update, patch=patch)
    except PagesCanonicalError:
        raise
    except (
        BottleLinkError,
        ContractError,
        ExecutionError,
        FormulaInventoryError,
        OSError,
        PromotionError,
        TapMetadataError,
    ) as error:
        raise PagesCanonicalError(
            f"Pages Formula metadata is invalid: {error}"
        ) from error
