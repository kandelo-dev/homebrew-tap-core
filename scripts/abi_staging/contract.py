"""Content-complete bottle contracts, capture assessments, and exact reuse."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit

from .canonical import (
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .plan import exact_formula_subject


MAX_CONTRACT_BYTES = 16 * 1024 * 1024
MAX_COMPONENTS = 65_536
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9@+._-]{0,255}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ARCHITECTURES = frozenset({"wasm32", "wasm64"})

CONTRACT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "target",
        "formula",
        "kandelo_inputs",
        "tap_inputs",
        "sdk",
        "libc",
        "sysroot",
        "toolchain",
        "instrumentation",
        "environment",
        "sources",
        "native_inputs",
        "direct_dependencies",
        "build_policy_sha256",
    }
)
TARGET_KEYS = frozenset({"abi", "snapshot_sha256", "architecture"})
FORMULA_KEYS = frozenset(
    {"name", "version", "revision", "rebuild", "normalized_source_sha256", "source_components"}
)
NAMED_DIGEST_KEYS = frozenset({"id", "sha256"})
REPOSITORY_INPUT_KEYS = frozenset({"id", "kind", "path", "sha256"})
COMPONENT_KEYS = frozenset({"policy_sha256", "component_sha256"})
ENVIRONMENT_KEYS = frozenset({"policy_sha256", "variables_sha256"})
SOURCE_KEYS = frozenset({"role", "url", "sha256", "receipt_sha256"})
NATIVE_KEYS = frozenset({"role", "identity", "sha256", "receipt_sha256"})
DEPENDENCY_KEYS = frozenset(
    {
        "formula",
        "architecture",
        "bottle_layer_sha256",
        "bottle_layer_bytes",
        "materialization_policy_sha256",
    }
)
ASSESSMENT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "subject",
        "complete",
        "captured",
        "missing",
        "ambiguous",
        "affected_products",
        "override_subject",
        "guard_code",
    }
)
CAPTURED_INPUT_KEYS = frozenset({"id", "repository", "kind", "path", "sha256"})
CAPTURE_ISSUE_KEYS = frozenset({"repository", "path", "reason"})


class ContractError(ValueError):
    """Raised when capture or reuse identity is incomplete or contradictory."""


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ContractError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ContractError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise ContractError(f"{field} is outside its string bound")
    return value


def _stable_id(value: Any, field: str) -> str:
    result = _text(value, field, 256)
    if STABLE_ID.fullmatch(result) is None:
        raise ContractError(f"{field} is not a stable identity")
    return result


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ContractError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise ContractError(f"{field} is not a full lowercase Git SHA")
    return value


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > 2**64 - 1
    ):
        qualifier = "positive " if positive else "nonnegative "
        raise ContractError(f"{field} must be a bounded {qualifier}integer")
    return value


def _architecture(value: Any, field: str) -> str:
    if value not in ARCHITECTURES:
        raise ContractError(f"{field} is not a supported architecture")
    return value


def _relative_path(value: Any, field: str) -> str:
    result = _text(value, field)
    if (
        result.startswith("/")
        or "\\" in result
        or any(part in {"", ".", "..", ".git"} for part in result.split("/"))
    ):
        raise ContractError(f"{field} is not an exact repository-relative path")
    return result


def _repository(value: Any, field: str) -> str:
    result = _text(value, field, 256)
    if REPOSITORY.fullmatch(result) is None:
        raise ContractError(f"{field} is not owner/repository")
    return result


def _url(value: Any, field: str) -> str:
    result = _text(value, field, 8192)
    parsed = urlsplit(result)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ContractError(f"{field} is not a bounded source URL")
    return result


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _validate_named_digests(value: Any, field: str) -> None:
    previous = ""
    entries = _sequence(value, field)
    if len(entries) > MAX_COMPONENTS:
        raise ContractError(f"{field} exceeds its bound")
    for index, candidate in enumerate(entries):
        entry = _mapping(candidate, f"{field} {index}")
        _exact_keys(entry, NAMED_DIGEST_KEYS, f"{field} {index}")
        identity = _stable_id(entry["id"], f"{field} identity {index}")
        if identity <= previous:
            raise ContractError(f"{field} must be sorted and duplicate-free")
        previous = identity
        _digest(entry["sha256"], f"{field} digest {index}")


def _validate_repository_inputs(value: Any, field: str) -> None:
    previous = ""
    entries = _sequence(value, field)
    if len(entries) > MAX_COMPONENTS:
        raise ContractError(f"{field} exceeds its bound")
    for index, candidate in enumerate(entries):
        entry = _mapping(candidate, f"{field} {index}")
        _exact_keys(entry, REPOSITORY_INPUT_KEYS, f"{field} {index}")
        identity = _stable_id(entry["id"], f"{field} identity {index}")
        if identity <= previous:
            raise ContractError(f"{field} must be sorted and duplicate-free")
        previous = identity
        if entry["kind"] not in {"file", "tree"}:
            raise ContractError(f"{field} {identity} has an unsupported kind")
        _relative_path(entry["path"], f"{field} path {identity}")
        _digest(entry["sha256"], f"{field} digest {identity}")


def _validate_component(value: Any, field: str, keys: frozenset[str] = COMPONENT_KEYS) -> None:
    component = _mapping(value, field)
    _exact_keys(component, keys, field)
    for key in keys:
        _digest(component[key], f"{field} {key}")


def validate_bottle_contract(contract: Mapping[str, Any]) -> None:
    value = _mapping(contract, "bottle contract")
    _exact_keys(value, CONTRACT_KEYS, "bottle contract")
    if value["schema"] != 1 or value["kind"] != "kandelo-homebrew-bottle-contract":
        raise ContractError("bottle contract protocol is unsupported")
    target = _mapping(value["target"], "contract target")
    _exact_keys(target, TARGET_KEYS, "contract target")
    _integer(target["abi"], "target ABI")
    _digest(target["snapshot_sha256"], "target ABI snapshot")
    architecture = _architecture(target["architecture"], "target architecture")
    formula = _mapping(value["formula"], "contract Formula")
    _exact_keys(formula, FORMULA_KEYS, "contract Formula")
    _stable_id(formula["name"], "Formula name")
    _text(formula["version"], "Formula version", 256)
    _integer(formula["revision"], "Formula revision")
    _integer(formula["rebuild"], "Formula bottle rebuild")
    _digest(formula["normalized_source_sha256"], "normalized Formula source")
    _validate_named_digests(formula["source_components"], "Formula source components")
    _validate_repository_inputs(value["kandelo_inputs"], "Kandelo inputs")
    _validate_repository_inputs(value["tap_inputs"], "tap inputs")
    for field in ("sdk", "libc", "sysroot", "toolchain", "instrumentation"):
        _validate_component(value[field], field)
    _validate_component(value["environment"], "environment", ENVIRONMENT_KEYS)

    previous_role = ""
    sources = _sequence(value["sources"], "contract sources")
    if not sources or len(sources) > MAX_COMPONENTS:
        raise ContractError("contract sources must be bounded and nonempty")
    for index, candidate in enumerate(sources):
        source = _mapping(candidate, f"contract source {index}")
        _exact_keys(source, SOURCE_KEYS, f"contract source {index}")
        role = _text(source["role"], f"source role {index}", 256)
        if role <= previous_role:
            raise ContractError("contract sources must be sorted and duplicate-free")
        previous_role = role
        _url(source["url"], f"source URL {role}")
        _digest(source["sha256"], f"source digest {role}")
        _digest(source["receipt_sha256"], f"source receipt {role}")

    previous_native: tuple[str, str] | None = None
    native_inputs = _sequence(value["native_inputs"], "native inputs")
    if len(native_inputs) > MAX_COMPONENTS:
        raise ContractError("native inputs exceed their bound")
    for index, candidate in enumerate(native_inputs):
        native = _mapping(candidate, f"native input {index}")
        _exact_keys(native, NATIVE_KEYS, f"native input {index}")
        role = _stable_id(native["role"], f"native role {index}")
        identity = _text(native["identity"], f"native identity {index}", 512)
        key = (role, identity)
        if previous_native is not None and previous_native >= key:
            raise ContractError("native inputs must be sorted and duplicate-free")
        previous_native = key
        _digest(native["sha256"], f"native input digest {role}")
        _digest(native["receipt_sha256"], f"native input receipt {role}")

    previous_dependency = ""
    dependencies = _sequence(value["direct_dependencies"], "direct dependencies")
    if len(dependencies) > MAX_COMPONENTS:
        raise ContractError("direct dependencies exceed their bound")
    for index, candidate in enumerate(dependencies):
        dependency = _mapping(candidate, f"direct dependency {index}")
        _exact_keys(dependency, DEPENDENCY_KEYS, f"direct dependency {index}")
        name = _stable_id(dependency["formula"], f"dependency Formula {index}")
        dependency_architecture = _architecture(
            dependency["architecture"], f"dependency architecture {index}"
        )
        if dependency_architecture != architecture:
            raise ContractError("direct dependency architecture differs from target")
        subject = exact_formula_subject(name, dependency_architecture)
        if subject <= previous_dependency:
            raise ContractError("direct dependencies must be sorted and duplicate-free")
        previous_dependency = subject
        _digest(dependency["bottle_layer_sha256"], f"dependency layer {name}")
        _integer(dependency["bottle_layer_bytes"], f"dependency layer bytes {name}", positive=True)
        _digest(
            dependency["materialization_policy_sha256"],
            f"dependency materialization policy {name}",
        )
    _digest(value["build_policy_sha256"], "build policy")
    if len(canonical_bytes(value)) > MAX_CONTRACT_BYTES:
        raise ContractError("bottle contract exceeds its byte bound")


def build_bottle_contract(inputs: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical_bytes(inputs))
    validate_bottle_contract(value)
    return value


def bottle_contract_digest(contract: Mapping[str, Any]) -> str:
    validate_bottle_contract(contract)
    return canonical_sha256(contract)


def load_bottle_contract(body: bytes) -> dict[str, Any]:
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_CONTRACT_BYTES))
    except CanonicalJsonError as error:
        raise ContractError(f"bottle contract is not canonical: {error}") from error
    validate_bottle_contract(value)
    return value


def load_canonical_mapping(body: bytes, field: str) -> dict[str, Any]:
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_CONTRACT_BYTES))
    except CanonicalJsonError as error:
        raise ContractError(f"{field} is not canonical: {error}") from error
    return dict(_mapping(value, field))


def contract_from_build_context(context: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(context, "contract build context")
    _exact_keys(value, frozenset({"contract_inputs", "provenance"}), "contract build context")
    provenance = _mapping(value["provenance"], "contract provenance")
    _exact_keys(
        provenance,
        frozenset(
            {
                "pull_request",
                "branch_hint",
                "exact_commit",
                "exact_tree",
                "request_digest",
                "run_id",
                "job",
                "producer_workflow",
                "timestamp",
            }
        ),
        "contract provenance",
    )
    _integer(provenance["pull_request"], "provenance pull request", positive=True)
    _text(provenance["branch_hint"], "provenance branch hint", 1024)
    _git_sha(provenance["exact_commit"], "provenance exact commit")
    _git_sha(provenance["exact_tree"], "provenance exact tree")
    _digest(provenance["request_digest"], "provenance request digest")
    _integer(provenance["run_id"], "provenance run ID", positive=True)
    _stable_id(provenance["job"], "provenance job")
    _text(provenance["producer_workflow"], "provenance workflow", 2048)
    timestamp = _text(provenance["timestamp"], "provenance timestamp", 64)
    if "T" not in timestamp or not timestamp.endswith("Z"):
        raise ContractError("provenance timestamp is not UTC RFC 3339")
    return build_bottle_contract(_mapping(value["contract_inputs"], "contract inputs"))


def _validated_subject(subject: Any, field: str) -> str:
    value = _text(subject, field, 512)
    try:
        parsed = _mapping(json.loads(value), field)
    except (json.JSONDecodeError, ContractError) as error:
        raise ContractError(f"{field} is not exact subject JSON: {error}") from error
    _exact_keys(parsed, frozenset({"architecture", "identity", "kind"}), field)
    if parsed["kind"] != "formula":
        raise ContractError(f"{field} is not a Formula subject")
    expected = exact_formula_subject(
        _stable_id(parsed["identity"], f"{field} identity"),
        _architecture(parsed["architecture"], f"{field} architecture"),
    )
    if value != expected:
        raise ContractError(f"{field} is not canonical")
    return value


def _path_descriptor(root: Path, relative: str) -> tuple[str, str]:
    path = root / relative
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise error
    if stat.S_ISLNK(metadata.st_mode):
        raise ContractError("symlink-not-authorized")
    if stat.S_ISREG(metadata.st_mode):
        body = path.read_bytes()
        descriptor = {
            "bytes": len(body),
            "kind": "file",
            "mode": "executable" if metadata.st_mode & 0o111 else "regular",
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        return "file", canonical_sha256(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ContractError("unsupported-file-kind")
    entries = []
    for candidate in sorted(path.rglob("*")):
        candidate_metadata = candidate.lstat()
        candidate_relative = candidate.relative_to(path).as_posix()
        if stat.S_ISLNK(candidate_metadata.st_mode):
            raise ContractError(f"symlink-not-authorized:{candidate_relative}")
        if stat.S_ISDIR(candidate_metadata.st_mode):
            continue
        if not stat.S_ISREG(candidate_metadata.st_mode):
            raise ContractError(f"unsupported-file-kind:{candidate_relative}")
        body = candidate.read_bytes()
        entries.append(
            {
                "bytes": len(body),
                "kind": "file",
                "mode": "executable" if candidate_metadata.st_mode & 0o111 else "regular",
                "path": candidate_relative,
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    return "tree", canonical_sha256({"entries": entries})


def _captured_path(
    repository: str, root: Path, relative: str, index: int
) -> tuple[dict[str, str] | None, dict[str, str] | None, dict[str, str] | None]:
    try:
        kind, digest = _path_descriptor(root, relative)
    except FileNotFoundError:
        return None, {"repository": repository, "path": relative, "reason": "missing"}, None
    except (ContractError, OSError) as error:
        reason = str(error)
        if not reason.startswith(("symlink-not-authorized", "unsupported-file-kind")):
            reason = "unreadable-input"
        return None, None, {"repository": repository, "path": relative, "reason": reason}
    return (
        {
            "id": f"{repository}-{index:04d}",
            "repository": repository,
            "kind": kind,
            "path": relative,
            "sha256": digest,
        },
        None,
        None,
    )


def _sorted_paths(values: Sequence[str], field: str) -> list[str]:
    checked = [_relative_path(value, field) for value in values]
    if checked != sorted(set(checked)):
        raise ContractError(f"{field} must be sorted and duplicate-free")
    return checked


def _covered(observed: str, declared: Sequence[str]) -> bool:
    return any(observed == path or observed.startswith(f"{path}/") for path in declared)


def assess_capture(
    *,
    subject: str,
    affected_products: Sequence[str],
    kandelo_root: Path,
    tap_root: Path,
    kandelo_paths: Sequence[str],
    tap_paths: Sequence[str],
    observed_kandelo_paths: Sequence[str],
    observed_tap_paths: Sequence[str],
) -> dict[str, Any]:
    exact_subject = _validated_subject(subject, "capture subject")
    products = [_stable_id(product, "affected product") for product in affected_products]
    if products != sorted(set(products)):
        raise ContractError("affected products must be sorted and duplicate-free")
    roots = {
        "kandelo": kandelo_root.resolve(strict=True),
        "tap": tap_root.resolve(strict=True),
    }
    declarations = {
        "kandelo": _sorted_paths(kandelo_paths, "Kandelo capture paths"),
        "tap": _sorted_paths(tap_paths, "tap capture paths"),
    }
    observations = {
        "kandelo": _sorted_paths(observed_kandelo_paths, "observed Kandelo paths"),
        "tap": _sorted_paths(observed_tap_paths, "observed tap paths"),
    }
    captured = []
    missing = []
    ambiguous = []
    for repository in ("kandelo", "tap"):
        for index, relative in enumerate(declarations[repository]):
            captured_entry, missing_entry, ambiguous_entry = _captured_path(
                repository, roots[repository], relative, index
            )
            if captured_entry is not None:
                captured.append(captured_entry)
            if missing_entry is not None:
                missing.append(missing_entry)
            if ambiguous_entry is not None:
                ambiguous.append(ambiguous_entry)
        for observed in observations[repository]:
            if not _covered(observed, declarations[repository]):
                ambiguous.append(
                    {
                        "repository": repository,
                        "path": observed,
                        "reason": "undeclared-observed-input",
                    }
                )
    captured.sort(key=lambda item: (item["repository"], item["path"]))
    missing.sort(key=lambda item: (item["repository"], item["path"], item["reason"]))
    ambiguous.sort(key=lambda item: (item["repository"], item["path"], item["reason"]))
    assessment = {
        "schema": 1,
        "kind": "kandelo-build-input-capture-assessment",
        "subject": exact_subject,
        "complete": not missing and not ambiguous,
        "captured": captured,
        "missing": missing,
        "ambiguous": ambiguous,
        "affected_products": products,
        "override_subject": exact_subject,
        "guard_code": "build_input_capture_incomplete",
    }
    validate_capture_assessment(assessment)
    return assessment


def validate_capture_assessment(assessment: Mapping[str, Any]) -> None:
    value = _mapping(assessment, "capture assessment")
    _exact_keys(value, ASSESSMENT_KEYS, "capture assessment")
    if (
        value["schema"] != 1
        or value["kind"] != "kandelo-build-input-capture-assessment"
    ):
        raise ContractError("capture assessment protocol is unsupported")
    subject = _validated_subject(value["subject"], "capture subject")
    if not isinstance(value["complete"], bool):
        raise ContractError("capture assessment complete must be boolean")

    previous_capture: tuple[str, str] | None = None
    for index, candidate in enumerate(_sequence(value["captured"], "captured inputs")):
        entry = _mapping(candidate, f"captured input {index}")
        _exact_keys(entry, CAPTURED_INPUT_KEYS, f"captured input {index}")
        _stable_id(entry["id"], f"captured input identity {index}")
        repository = entry["repository"]
        if repository not in {"kandelo", "tap"}:
            raise ContractError("captured input repository is unsupported")
        path = _relative_path(entry["path"], f"captured input path {index}")
        key = (repository, path)
        if previous_capture is not None and previous_capture >= key:
            raise ContractError("captured inputs must be sorted and duplicate-free")
        previous_capture = key
        if entry["kind"] not in {"file", "tree"}:
            raise ContractError("captured input kind is unsupported")
        _digest(entry["sha256"], f"captured input digest {index}")

    for field in ("missing", "ambiguous"):
        previous_issue: tuple[str, str, str] | None = None
        for index, candidate in enumerate(_sequence(value[field], f"capture {field}")):
            issue = _mapping(candidate, f"capture {field} {index}")
            _exact_keys(issue, CAPTURE_ISSUE_KEYS, f"capture {field} {index}")
            repository = issue["repository"]
            if repository not in {"kandelo", "tap"}:
                raise ContractError(f"capture {field} repository is unsupported")
            path = _relative_path(issue["path"], f"capture {field} path {index}")
            reason = _text(issue["reason"], f"capture {field} reason {index}", 1024)
            key = (repository, path, reason)
            if previous_issue is not None and previous_issue >= key:
                raise ContractError(f"capture {field} must be sorted and duplicate-free")
            previous_issue = key

    products = [
        _stable_id(product, f"affected product {index}")
        for index, product in enumerate(
            _sequence(value["affected_products"], "affected products")
        )
    ]
    if products != sorted(set(products)):
        raise ContractError("affected products must be sorted and duplicate-free")
    if value["override_subject"] != subject:
        raise ContractError("capture assessment override subject differs")
    if value["guard_code"] != "build_input_capture_incomplete":
        raise ContractError("capture assessment guard code changed")
    expected_complete = not value["missing"] and not value["ambiguous"]
    if value["complete"] != expected_complete:
        raise ContractError("capture assessment completeness is contradictory")


def require_complete_capture(assessment: Mapping[str, Any], subject: str) -> None:
    value = _mapping(assessment, "capture assessment")
    validate_capture_assessment(value)
    expected_subject = _validated_subject(subject, "required capture subject")
    if value["subject"] != expected_subject or value["override_subject"] != expected_subject:
        raise ContractError("capture assessment authorizes a different exact subject")
    if not value["complete"] or value["missing"] or value["ambiguous"]:
        raise ContractError(
            "build input capture is incomplete: "
            f"missing={value['missing']!r} ambiguous={value['ambiguous']!r}; "
            f"override_subject={expected_subject}"
        )


def _record_link(value: Any, field: str) -> dict[str, str]:
    link = _mapping(value, field)
    _exact_keys(link, frozenset({"record_sha256", "immutable_reference"}), field)
    digest = _digest(link["record_sha256"], f"{field} digest")
    reference = _text(link["immutable_reference"], f"{field} reference", 8192)
    if f"sha256:{digest}" not in reference:
        raise ContractError(f"{field} reference does not bind its digest")
    return {"record_sha256": digest, "immutable_reference": reference}


def _artifact(value: Any, field: str) -> dict[str, Any]:
    artifact = _mapping(value, field)
    _exact_keys(artifact, frozenset({"sha256", "bytes", "immutable_reference"}), field)
    digest = _digest(artifact["sha256"], f"{field} digest")
    byte_count = _integer(artifact["bytes"], f"{field} bytes", positive=True)
    reference = _text(artifact["immutable_reference"], f"{field} reference", 8192)
    if f"sha256:{digest}" not in reference:
        raise ContractError(f"{field} reference does not bind its digest")
    return {"sha256": digest, "bytes": byte_count, "immutable_reference": reference}


def _producer(value: Any, field: str) -> dict[str, Any]:
    producer = _mapping(value, field)
    _exact_keys(producer, frozenset({"request_sha256", "head", "run_id"}), field)
    return {
        "request_sha256": _digest(producer["request_sha256"], f"{field} request"),
        "head": _git_sha(producer["head"], f"{field} head"),
        "run_id": _integer(producer["run_id"], f"{field} run ID", positive=True),
    }


def _validate_candidate(
    contract: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    expected_source_custody_sha256: str,
) -> dict[str, Any]:
    validate_bottle_contract(contract)
    value = _mapping(candidate, "existing candidate")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "contract_sha256",
                "formula",
                "candidate_record",
                "source_custody",
                "bottle_layer",
                "qualifying_receipts",
                "original_producer",
                "nonendorsed",
            }
        ),
        "existing candidate",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-existing-candidate":
        raise ContractError("existing candidate protocol is unsupported")
    _digest(value["contract_sha256"], "candidate contract")
    formula = _mapping(value["formula"], "candidate Formula")
    _exact_keys(
        formula,
        frozenset({"tap", "formula", "architecture", "target_abi"}),
        "candidate Formula",
    )
    _repository(formula["tap"], "candidate tap")
    if formula["formula"] != contract["formula"]["name"]:
        raise ContractError("candidate Formula differs from contract")
    if formula["architecture"] != contract["target"]["architecture"]:
        raise ContractError("candidate architecture differs from contract")
    if formula["target_abi"] != contract["target"]["abi"]:
        raise ContractError("candidate ABI differs from contract")
    candidate_record = _record_link(value["candidate_record"], "candidate record")
    custody = _record_link(value["source_custody"], "source custody")
    expected_custody = _digest(
        expected_source_custody_sha256, "expected source-custody record"
    )
    if custody["record_sha256"] != expected_custody:
        raise ContractError("candidate source custody differs from exact capture")
    bottle_layer = _artifact(value["bottle_layer"], "candidate bottle layer")
    receipts = [
        _record_link(candidate_receipt, f"qualifying receipt {index}")
        for index, candidate_receipt in enumerate(
            _sequence(value["qualifying_receipts"], "qualifying receipts")
        )
    ]
    if not receipts or [item["record_sha256"] for item in receipts] != sorted(
        {item["record_sha256"] for item in receipts}
    ):
        raise ContractError("qualifying receipts must be sorted, unique, and nonempty")
    producer = _producer(value["original_producer"], "original producer")
    if value["nonendorsed"] is not True:
        raise ContractError("candidate must remain visibly nonendorsed")
    return {
        **value,
        "candidate_record": candidate_record,
        "source_custody": custody,
        "bottle_layer": bottle_layer,
        "qualifying_receipts": receipts,
        "original_producer": producer,
    }


def candidate_reuse_decision(
    contract: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    *,
    expected_source_custody_sha256: str,
    assessment: Mapping[str, Any] | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    validate_bottle_contract(contract)
    if assessment is not None:
        if subject is None:
            raise ContractError("capture assessment requires an exact subject")
        require_complete_capture(assessment, subject)
    if candidate is None:
        return {"action": "rebuild", "reason": "no-candidate"}
    checked = _validate_candidate(
        contract,
        candidate,
        expected_source_custody_sha256=expected_source_custody_sha256,
    )
    contract_digest = bottle_contract_digest(contract)
    if checked["contract_sha256"] != contract_digest:
        return {"action": "rebuild", "reason": "contract-changed"}
    return {
        "action": "reuse",
        "reason": "exact-contract-and-custody",
        "candidate_record_sha256": checked["candidate_record"]["record_sha256"],
        "bottle_layer_sha256": checked["bottle_layer"]["sha256"],
    }


def _new_request_context(value: Any) -> dict[str, Any]:
    context = _mapping(value, "new request context")
    _exact_keys(context, frozenset({"request_sha256", "source", "run"}), "new request context")
    source = _mapping(context["source"], "new request source")
    _exact_keys(source, frozenset({"repository", "commit", "tree"}), "new request source")
    checked_source = {
        "repository": _repository(source["repository"], "new request source repository"),
        "commit": _git_sha(source["commit"], "new request source commit"),
        "tree": _git_sha(source["tree"], "new request source tree"),
    }
    run = _mapping(context["run"], "reuse run")
    _exact_keys(
        run,
        frozenset({"repository", "workflow_ref", "run_id", "run_attempt", "job"}),
        "reuse run",
    )
    checked_run = {
        "repository": _repository(run["repository"], "reuse run repository"),
        "workflow_ref": _text(run["workflow_ref"], "reuse workflow ref", 2048),
        "run_id": _integer(run["run_id"], "reuse run ID", positive=True),
        "run_attempt": _integer(run["run_attempt"], "reuse run attempt", positive=True),
        "job": _stable_id(run["job"], "reuse job"),
    }
    return {
        "request_sha256": _digest(context["request_sha256"], "new request digest"),
        "source": checked_source,
        "run": checked_run,
    }


def make_candidate_reuse_record(
    contract: Mapping[str, Any],
    subject: str,
    candidate: Mapping[str, Any],
    new_request: Mapping[str, Any],
) -> dict[str, Any]:
    validate_bottle_contract(contract)
    exact_subject = _validated_subject(subject, "candidate reuse subject")
    subject_value = json.loads(exact_subject)
    if (
        subject_value["identity"] != contract["formula"]["name"]
        or subject_value["architecture"] != contract["target"]["architecture"]
    ):
        raise ContractError("candidate reuse subject differs from contract")
    checked_candidate = _validate_candidate(
        contract,
        candidate,
        expected_source_custody_sha256=candidate["source_custody"]["record_sha256"],
    )
    if checked_candidate["contract_sha256"] != bottle_contract_digest(contract):
        raise ContractError("cannot emit reuse record for a changed contract")
    context = _new_request_context(new_request)
    if context["request_sha256"] == checked_candidate["original_producer"]["request_sha256"]:
        raise ContractError("candidate reuse must bind a new request")
    formula = checked_candidate["formula"]
    record = {
        "kind": "kandelo-abi-staging-candidate-reuse",
        "schema": 1,
        "common": {
            "request_sha256": context["request_sha256"],
            "subject": {
                "kind": "formula",
                "identity": f"{formula['tap']}/{formula['formula']}",
                "architecture": formula["architecture"],
            },
            "source": context["source"],
            "run": context["run"],
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "candidate",
            "artifact": checked_candidate["bottle_layer"],
            "promotion_state": "eligible",
            "retry_state": {
                "attempts": 0,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "blockers": [],
        },
        "candidate_reuse": {
            "formula": {
                "tap": formula["tap"],
                "formula": formula["formula"],
                "architecture": formula["architecture"],
                "target_abi": formula["target_abi"],
                "bottle_contract_sha256": checked_candidate["contract_sha256"],
            },
            "existing_candidate": checked_candidate["candidate_record"],
            "bottle_layer": checked_candidate["bottle_layer"],
            "source_custody": checked_candidate["source_custody"],
            "qualifying_receipts": checked_candidate["qualifying_receipts"],
            "original_producer": checked_candidate["original_producer"],
            "nonendorsed": True,
        },
    }
    validate_candidate_reuse_record(record)
    return record


def validate_candidate_reuse_record(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "candidate reuse record")
    _exact_keys(
        value,
        frozenset({"kind", "schema", "common", "candidate_reuse"}),
        "candidate reuse record",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-candidate-reuse":
        raise ContractError("candidate reuse record protocol is unsupported")
    common = _mapping(value["common"], "candidate reuse common")
    _exact_keys(
        common,
        frozenset(
            {
                "request_sha256",
                "subject",
                "source",
                "run",
                "guard_codes",
                "work_state",
                "outcome",
                "artifact_class",
                "artifact",
                "promotion_state",
                "retry_state",
                "blockers",
            }
        ),
        "candidate reuse common",
    )
    _digest(common["request_sha256"], "candidate reuse request")
    subject = _mapping(common["subject"], "candidate reuse common subject")
    _exact_keys(subject, frozenset({"kind", "identity", "architecture"}), "candidate reuse common subject")
    if subject["kind"] != "formula":
        raise ContractError("candidate reuse record subject is not Formula")
    _architecture(subject["architecture"], "candidate reuse common architecture")
    _text(subject["identity"], "candidate reuse common identity", 512)
    context = _new_request_context(
        {"request_sha256": common["request_sha256"], "source": common["source"], "run": common["run"]}
    )
    if (
        common["guard_codes"] != []
        or common["work_state"] != "complete"
        or common["outcome"] != "success"
        or common["artifact_class"] != "candidate"
        or common["promotion_state"] != "eligible"
        or common["blockers"] != []
        or common["retry_state"]
        != {"attempts": 0, "eligible": False, "exhausted": False, "next_action": "none"}
    ):
        raise ContractError("candidate reuse common state is contradictory")
    common_artifact = _artifact(common["artifact"], "candidate reuse common artifact")
    payload = _mapping(value["candidate_reuse"], "candidate reuse payload")
    _exact_keys(
        payload,
        frozenset(
            {
                "formula",
                "existing_candidate",
                "bottle_layer",
                "source_custody",
                "qualifying_receipts",
                "original_producer",
                "nonendorsed",
            }
        ),
        "candidate reuse payload",
    )
    formula = _mapping(payload["formula"], "candidate reuse Formula")
    _exact_keys(
        formula,
        frozenset({"tap", "formula", "architecture", "target_abi", "bottle_contract_sha256"}),
        "candidate reuse Formula",
    )
    tap = _repository(formula["tap"], "candidate reuse tap")
    name = _stable_id(formula["formula"], "candidate reuse Formula name")
    architecture = _architecture(formula["architecture"], "candidate reuse architecture")
    _integer(formula["target_abi"], "candidate reuse target ABI")
    _digest(formula["bottle_contract_sha256"], "candidate reuse contract")
    if subject != {"kind": "formula", "identity": f"{tap}/{name}", "architecture": architecture}:
        raise ContractError("candidate reuse common subject differs from payload")
    _record_link(payload["existing_candidate"], "candidate reuse existing candidate")
    bottle_layer = _artifact(payload["bottle_layer"], "candidate reuse bottle layer")
    if common_artifact != bottle_layer:
        raise ContractError("candidate reuse common artifact differs from exact existing layer")
    _record_link(payload["source_custody"], "candidate reuse source custody")
    receipts = [
        _record_link(item, f"candidate reuse receipt {index}")
        for index, item in enumerate(_sequence(payload["qualifying_receipts"], "candidate reuse receipts"))
    ]
    if not receipts or [item["record_sha256"] for item in receipts] != sorted(
        {item["record_sha256"] for item in receipts}
    ):
        raise ContractError("candidate reuse qualifying receipts are invalid")
    producer = _producer(payload["original_producer"], "candidate reuse original producer")
    if producer["request_sha256"] == context["request_sha256"]:
        raise ContractError("candidate reuse record invents no new build for the same request")
    if payload["nonendorsed"] is not True:
        raise ContractError("candidate reuse record must preserve nonendorsement")


def changed_dependency_subjects(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[str]:
    validate_bottle_contract(before)
    validate_bottle_contract(after)
    before_dependencies = {
        (entry["formula"], entry["architecture"]): entry
        for entry in before["direct_dependencies"]
    }
    after_dependencies = {
        (entry["formula"], entry["architecture"]): entry
        for entry in after["direct_dependencies"]
    }
    changed = []
    for subject in sorted(set(before_dependencies) | set(after_dependencies)):
        if before_dependencies.get(subject) != after_dependencies.get(subject):
            changed.append(exact_formula_subject(*subject))
    return changed


def build_miniature_bottle_contract_fixture() -> dict[str, Any]:
    return build_bottle_contract(
        {
            "schema": 1,
            "kind": "kandelo-homebrew-bottle-contract",
            "target": {"abi": 8, "snapshot_sha256": "a" * 64, "architecture": "wasm32"},
            "formula": {
                "name": "mini-tool",
                "version": "1.0.0",
                "revision": 1,
                "rebuild": 2,
                "normalized_source_sha256": "b" * 64,
                "source_components": [
                    {"id": "formula", "sha256": "c" * 64},
                    {"id": "support", "sha256": "d" * 64},
                ],
            },
            "kandelo_inputs": [
                {"id": "sdk", "kind": "tree", "path": "sdk", "sha256": "e" * 64}
            ],
            "tap_inputs": [
                {
                    "id": "formula-support",
                    "kind": "file",
                    "path": "Kandelo/formula_support/kandelo_formula_support.rb",
                    "sha256": "f" * 64,
                }
            ],
            "sdk": {"policy_sha256": "1" * 64, "component_sha256": "2" * 64},
            "libc": {"policy_sha256": "3" * 64, "component_sha256": "4" * 64},
            "sysroot": {"policy_sha256": "5" * 64, "component_sha256": "6" * 64},
            "toolchain": {"policy_sha256": "7" * 64, "component_sha256": "8" * 64},
            "instrumentation": {"policy_sha256": "9" * 64, "component_sha256": "a" * 64},
            "environment": {"policy_sha256": "b" * 64, "variables_sha256": "c" * 64},
            "sources": [
                {
                    "role": "primary",
                    "url": "https://example.test/mini-tool-1.0.0.tar.gz",
                    "sha256": "d" * 64,
                    "receipt_sha256": "e" * 64,
                }
            ],
            "native_inputs": [],
            "direct_dependencies": [
                {
                    "formula": "mini-base",
                    "architecture": "wasm32",
                    "bottle_layer_sha256": "f" * 64,
                    "bottle_layer_bytes": 128,
                    "materialization_policy_sha256": "1" * 64,
                }
            ],
            "build_policy_sha256": "2" * 64,
        }
    )
