"""Protected normalization of the tap's exact Formula inventory.

The checked-in probe is a local fixture aid. Hosted staging must supply an
inert probe produced by the reviewed Homebrew environment; this module then
compares every field with independently parsed protected tap bytes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

from .canonical import (
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .policy import FormulaBuildInputPolicyV1


PROBE_KEYS = frozenset({"schema", "kind", "formulae"})
FORMULA_KEYS = frozenset(
    {
        "name",
        "formula_path",
        "version",
        "revision",
        "rebuild",
        "architectures",
        "target_dependencies",
        "native_requirements",
        "sources",
    }
)
DEPENDENCY_KEYS = frozenset({"name", "scopes"})
NATIVE_REQUIREMENT_KEYS = frozenset({"identity", "scopes"})
SOURCE_KEYS = frozenset({"role", "kind", "url", "mirrors", "sha256"})
INVENTORY_KEYS = frozenset(
    {
        "schema",
        "kind",
        "formula_tree",
        "sidecar_tree",
        "probe_sha256",
        "capture_catalog_sha256",
        "graph_sha256",
        "formulae",
    }
)
INVENTORY_FORMULA_KEYS = FORMULA_KEYS | frozenset(
    {
        "normalized_formula_sha256",
        "tap_input_components",
        "normalized_source_sha256",
        "capture_policy_sha256",
    }
)
SIDECAR_KEYS = frozenset(
    {
        "bottle_rebuild",
        "bottles",
        "dependencies",
        "formula_path",
        "formula_revision",
        "full_name",
        "kandelo_abi",
        "name",
        "schema",
        "source_metadata",
        "tap_commit",
        "tap_name",
        "tap_repository",
        "version",
    }
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT = re.compile(r"^[0-9a-f]{40,64}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
NATIVE_ID = re.compile(r"^[a-z0-9][a-z0-9@+._-]{0,127}$")
URL = re.compile(r"^https?://[^\s\x00-\x1f]{1,4096}$")
CONSTANT_REQUIREMENTS = {
    "BinaryenRequirement": "binaryen",
    "PkgconfRequirement": "pkgconf",
    "WabtRequirement": "wabt",
}
MAX_PROBE_BYTES = 16 * 1024 * 1024
MAX_INVENTORY_BYTES = 32 * 1024 * 1024


class FormulaInventoryError(ValueError):
    """Raised when Formula inventory evidence is incomplete or ambiguous."""


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise FormulaInventoryError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FormulaInventoryError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise FormulaInventoryError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise FormulaInventoryError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise FormulaInventoryError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise FormulaInventoryError(f"{field} is outside its string bound")
    return value


def _stable_id(value: Any, field: str) -> str:
    result = _text(value, field, 128)
    if STABLE_ID.fullmatch(result) is None:
        raise FormulaInventoryError(f"{field} is not a stable identifier")
    return result


def _native_id(value: Any, field: str) -> str:
    result = _text(value, field, 128)
    if NATIVE_ID.fullmatch(result) is None:
        raise FormulaInventoryError(f"{field} is not a native package identifier")
    return result


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**32 - 1:
        raise FormulaInventoryError(f"{field} must be a bounded nonnegative integer")
    return value


def _digest(value: Any, field: str) -> str:
    result = _text(value, field, 64)
    if SHA256.fullmatch(result) is None:
        raise FormulaInventoryError(f"{field} is not a lowercase SHA-256 digest")
    return result


def _decode_formula(source: bytes) -> str:
    if not isinstance(source, bytes) or not source:
        raise FormulaInventoryError("Formula source must be nonempty bytes")
    if b"\r" in source or not source.endswith(b"\n"):
        raise FormulaInventoryError("Formula source must use LF lines and one trailing line feed")
    try:
        return source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise FormulaInventoryError("Formula source is not UTF-8") from error


def _bottle_span(lines: list[str]) -> tuple[int, int] | None:
    starts: list[int] = []
    for index, line in enumerate(lines):
        if line.strip() == "bottle do":
            if line != "  bottle do\n":
                raise FormulaInventoryError("bottle block has noncanonical indentation")
            starts.append(index)
    if len(starts) > 1:
        raise FormulaInventoryError("Formula contains multiple bottle blocks")
    if not starts:
        return None
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index] == "  end\n"),
        None,
    )
    if end is None:
        raise FormulaInventoryError("bottle block is unterminated")
    body = lines[start + 1 : end]
    if not 2 <= len(body) <= 4:
        raise FormulaInventoryError("bottle block has an unsupported shape")
    position = 0
    if re.fullmatch(r'    root_url "https?://[^"\n]+"\n', body[position]) is None:
        raise FormulaInventoryError("bottle block has no canonical root_url")
    position += 1
    if position < len(body) and body[position].startswith("    rebuild "):
        match = re.fullmatch(r"    rebuild ([1-9][0-9]*)\n", body[position])
        if match is None or int(match.group(1)) > 2**32 - 1:
            raise FormulaInventoryError("bottle rebuild is not a bounded positive integer")
        position += 1
    sha_lines = body[position:]
    if not 1 <= len(sha_lines) <= 2:
        raise FormulaInventoryError("bottle block must contain one or two SHA lines")
    architectures: list[str] = []
    for line in sha_lines:
        match = re.fullmatch(
            r'    sha256 cellar: (?:"[^"\n]+"|:any_skip_relocation), '
            r'(wasm(?:32|64))_kandelo: "([0-9a-f]{64})"\n',
            line,
        )
        if match is None:
            raise FormulaInventoryError("bottle block contains an unsupported line")
        architectures.append(match.group(1))
    if architectures != sorted(set(architectures)):
        raise FormulaInventoryError("bottle SHA lines must be sorted and duplicate-free")
    return start, end


def normalize_formula_source(source: bytes) -> bytes:
    """Exclude only the canonical generated bottle block from Formula identity."""

    text = _decode_formula(source)
    lines = text.splitlines(keepends=True)
    span = _bottle_span(lines)
    if span is None:
        return source
    start, end = span
    removal_start = start
    if start > 0 and lines[start - 1] == "\n":
        removal_start -= 1
    del lines[removal_start : end + 1]
    return "".join(lines).encode("utf-8")


def _bottle_rebuild(source: bytes) -> int:
    lines = _decode_formula(source).splitlines(keepends=True)
    span = _bottle_span(lines)
    if span is None:
        return 0
    start, end = span
    for line in lines[start + 1 : end]:
        match = re.fullmatch(r"    rebuild ([1-9][0-9]*)\n", line)
        if match is not None:
            return int(match.group(1))
    return 0


def _literal(argument: str, constants: Mapping[str, str], field: str) -> str:
    quoted = re.fullmatch(r'"([^"\n]*)"', argument)
    if quoted is not None:
        return quoted.group(1)
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", argument) is not None and argument in constants:
        return constants[argument]
    raise FormulaInventoryError(f"{field} is not one protected literal")


def _constants(lines: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(r'^  ([A-Z][A-Z0-9_]*) = "([^"\n]*)"(?:\.freeze)?$')
    interpolation = re.compile(r"#\{([A-Z][A-Z0-9_]*)\}")
    for line in lines:
        match = pattern.fullmatch(line)
        if match is None:
            continue
        name, value = match.groups()
        unresolved = False
        for referenced in interpolation.findall(value):
            if referenced not in result:
                unresolved = True
                break
            value = value.replace(f"#{{{referenced}}}", result[referenced])
        if unresolved or "#{" in value:
            continue
        result[name] = value
    return result


def _infer_version(url: str) -> str:
    basename = unquote(Path(urlsplit(url).path).name)
    for suffix in (".tar.gz", ".tar.xz", ".tar.lz", ".tar.bz2", ".tgz", ".zip", ".gz", ".xz"):
        if basename.endswith(suffix):
            basename = basename[: -len(suffix)]
            break
    candidates = re.findall(r"(?<![A-Za-z])v?(\d+(?:[._-]\d+)*)", basename)
    if not candidates:
        raise FormulaInventoryError(f"cannot infer a version from primary URL {url!r}")
    version = candidates[-1].replace("_", ".").replace("-", ".")
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,127}", version):
        raise FormulaInventoryError("inferred Formula version is unsupported")
    return version


def _scopes(suffix: str, field: str) -> list[str]:
    if not suffix:
        return ["runtime"]
    if suffix == " => :build":
        return ["build"]
    if suffix == " => :test":
        return ["test"]
    if suffix == " => [:build, :test]":
        return ["build", "test"]
    raise FormulaInventoryError(f"{field} has an unsupported dependency scope")


def _source_record(
    *, role: str, kind: str, url: str, mirrors: Sequence[str], sha256: str
) -> dict[str, Any]:
    if kind not in {"remote", "inline-patch"}:
        raise FormulaInventoryError(f"source {role} has an unsupported kind")
    _text(role, f"source role {role}", 256)
    if kind == "remote":
        if URL.fullmatch(url) is None or any(URL.fullmatch(mirror) is None for mirror in mirrors):
            raise FormulaInventoryError(f"source {role} has an unsupported URL")
    elif url != "inline:__END__" or mirrors:
        raise FormulaInventoryError("inline patch source has invalid location fields")
    _digest(sha256, f"source digest for {role}")
    return {
        "role": role,
        "kind": kind,
        "url": url,
        "mirrors": list(mirrors),
        "sha256": sha256,
    }


def _remote_block(
    body: Sequence[str], constants: Mapping[str, str], role: str
) -> dict[str, Any]:
    url: str | None = None
    digest: str | None = None
    mirrors: list[str] = []
    for line in body:
        if match := re.fullmatch(r"    url (.+)", line):
            if url is not None:
                raise FormulaInventoryError(f"source {role} contains multiple URLs")
            url = _literal(match.group(1), constants, f"URL for {role}")
        elif match := re.fullmatch(r"    mirror (.+)", line):
            mirrors.append(_literal(match.group(1), constants, f"mirror for {role}"))
        elif match := re.fullmatch(r"    sha256 (.+)", line):
            if digest is not None:
                raise FormulaInventoryError(f"source {role} contains multiple digests")
            digest = _literal(match.group(1), constants, f"digest for {role}")
    if url is None or digest is None:
        raise FormulaInventoryError(f"source {role} is missing a URL or SHA-256")
    return _source_record(role=role, kind="remote", url=url, mirrors=mirrors, sha256=digest)


def _class_blocks(lines: Sequence[str], opener: re.Pattern[str]) -> list[tuple[re.Match[str], list[str]]]:
    result: list[tuple[re.Match[str], list[str]]] = []
    index = 0
    while index < len(lines):
        match = opener.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        end = next(
            (candidate for candidate in range(index + 1, len(lines)) if lines[candidate] == "  end"),
            None,
        )
        if end is None:
            raise FormulaInventoryError("Formula source block is unterminated")
        result.append((match, list(lines[index + 1 : end])))
        index = end + 1
    return result


def parse_formula_source(
    name: str,
    formula_path: str,
    source: bytes,
    architectures: Sequence[str],
) -> dict[str, Any]:
    """Parse the bounded Formula declarations that define staged inputs."""

    if STABLE_ID.fullmatch(name) is None or formula_path != f"Formula/{name}.rb":
        raise FormulaInventoryError("Formula identity and path do not agree")
    if list(architectures) != sorted(set(architectures)) or any(
        item not in {"wasm32", "wasm64"} for item in architectures
    ):
        raise FormulaInventoryError(f"Formula {name} has invalid architecture policy")
    text = _decode_formula(source)
    class_matches = re.findall(r"^class [A-Za-z][A-Za-z0-9]* < Formula$", text, re.MULTILINE)
    if len(class_matches) != 1:
        raise FormulaInventoryError(f"Formula {name} must contain one Formula class")
    lines = text.splitlines()
    formula_lines = lines[: lines.index("__END__")] if "__END__" in lines else lines
    constants = _constants(formula_lines)

    top_urls = [match.group(1) for line in formula_lines if (match := re.fullmatch(r"  url (.+)", line))]
    top_shas = [match.group(1) for line in formula_lines if (match := re.fullmatch(r"  sha256 (.+)", line))]
    top_mirrors = [match.group(1) for line in formula_lines if (match := re.fullmatch(r"  mirror (.+)", line))]
    if len(top_urls) != 1 or len(top_shas) != 1:
        raise FormulaInventoryError(f"Formula {name} must have one primary URL and SHA-256")
    primary_url = _literal(top_urls[0], constants, f"primary URL for {name}")
    primary_sha = _literal(top_shas[0], constants, f"primary digest for {name}")
    primary_mirrors = [
        _literal(value, constants, f"primary mirror for {name}") for value in top_mirrors
    ]

    versions = [match.group(1) for line in formula_lines if (match := re.fullmatch(r'  version "([^"\n]+)"', line))]
    if len(versions) > 1:
        raise FormulaInventoryError(f"Formula {name} contains multiple versions")
    version = versions[0] if versions else _infer_version(primary_url)
    _text(version, f"version for {name}", 128)
    revisions = [int(match.group(1)) for line in formula_lines if (match := re.fullmatch(r"  revision ([0-9]+)", line))]
    if len(revisions) > 1:
        raise FormulaInventoryError(f"Formula {name} contains multiple revisions")
    revision = revisions[0] if revisions else 0
    _integer(revision, f"revision for {name}")

    target: list[dict[str, Any]] = []
    native: list[dict[str, Any]] = []
    dependency_lines = [line for line in formula_lines if line.lstrip().startswith("depends_on ")]
    if any(not line.startswith("  depends_on ") for line in dependency_lines):
        raise FormulaInventoryError(f"Formula {name} has a noncanonical dependency declaration")
    for line in dependency_lines:
        expression = line[len("  depends_on ") :]
        quoted = re.fullmatch(r'"([^"\n]+)"(.*)', expression)
        requirement = re.fullmatch(
            r"KandeloFormulaSupport::([A-Za-z][A-Za-z0-9]*)(.*)", expression
        )
        if quoted is not None:
            identity, suffix = quoted.groups()
            scopes = _scopes(suffix, f"dependency {identity} for {name}")
            prefix = "kandelo-dev/tap-core/"
            if identity.startswith(prefix):
                dependency_name = _stable_id(identity[len(prefix) :], "target dependency")
                target.append({"name": dependency_name, "scopes": scopes})
            elif "/" in identity:
                raise FormulaInventoryError(f"Formula {name} names an unsupported tap dependency")
            else:
                native.append({"identity": _native_id(identity, "native requirement"), "scopes": scopes})
        elif requirement is not None and requirement.group(1) in CONSTANT_REQUIREMENTS:
            requirement_name, suffix = requirement.groups()
            native.append(
                {
                    "identity": CONSTANT_REQUIREMENTS[requirement_name],
                    "scopes": _scopes(suffix, f"native requirement for {name}"),
                }
            )
        else:
            raise FormulaInventoryError(f"Formula {name} has a dynamic dependency declaration")
    for collection, field in ((target, "target dependencies"), (native, "native requirements")):
        collection.sort(key=lambda item: item.get("name", item.get("identity")))
        identities = [item.get("name", item.get("identity")) for item in collection]
        if len(identities) != len(set(identities)):
            raise FormulaInventoryError(f"Formula {name} has duplicate {field}")

    sources = [
        _source_record(
            role="primary",
            kind="remote",
            url=primary_url,
            mirrors=primary_mirrors,
            sha256=primary_sha,
        )
    ]
    for match, body in _class_blocks(
        formula_lines, re.compile(r'^  resource "([a-z0-9][a-z0-9._-]{0,127})" do$')
    ):
        sources.append(_remote_block(body, constants, f"resource:{match.group(1)}"))
    patch_index = 0
    for _, body in _class_blocks(formula_lines, re.compile(r"^  patch do$")):
        sources.append(_remote_block(body, constants, f"patch:{patch_index:03d}"))
        patch_index += 1
    inline_count = sum(line == "  patch :DATA" for line in formula_lines)
    if inline_count:
        if inline_count != 1 or "__END__\n" not in text:
            raise FormulaInventoryError(f"Formula {name} has invalid inline patch declarations")
        inline = text.split("__END__\n", 1)[1].encode("utf-8")
        if not inline:
            raise FormulaInventoryError(f"Formula {name} has an empty inline patch")
        sources.append(
            _source_record(
                role="inline-patch:000",
                kind="inline-patch",
                url="inline:__END__",
                mirrors=[],
                sha256=hashlib.sha256(inline).hexdigest(),
            )
        )
    sources.sort(key=lambda item: item["role"])
    roles = [item["role"] for item in sources]
    if len(roles) != len(set(roles)):
        raise FormulaInventoryError(f"Formula {name} has duplicate source roles")
    return {
        "name": name,
        "formula_path": formula_path,
        "version": version,
        "revision": revision,
        "rebuild": _bottle_rebuild(source),
        "architectures": list(architectures),
        "target_dependencies": target,
        "native_requirements": native,
        "sources": sources,
    }


def build_static_formula_probe(
    tap_root: Path, capture_policy: FormulaBuildInputPolicyV1
) -> dict[str, Any]:
    """Build a fixture-only probe without executing Formula Ruby code."""

    root = tap_root.resolve(strict=True)
    formulae = []
    for policy_entry in capture_policy.formulae:
        relative = f"Formula/{policy_entry.name}.rb"
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise FormulaInventoryError(f"Formula input is not one direct file: {relative}")
        formulae.append(
            parse_formula_source(
                policy_entry.name,
                relative,
                path.read_bytes(),
                policy_entry.architectures,
            )
        )
    return {"schema": 1, "kind": "kandelo-homebrew-formula-probe", "formulae": formulae}


def combined_source_sha256(
    normalized_formula_sha256: str, components: Sequence[Mapping[str, Any]]
) -> str:
    _digest(normalized_formula_sha256, "normalized Formula digest")
    normalized_components = []
    for index, candidate in enumerate(components):
        component = _mapping(candidate, f"tap input component {index}")
        _exact_keys(component, frozenset({"path", "sha256"}), f"tap input component {index}")
        path = _text(component["path"], f"tap input component path {index}")
        if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise FormulaInventoryError("tap input component path is unsafe")
        normalized_components.append({"path": path, "sha256": _digest(component["sha256"], f"tap input component digest {index}")})
    if normalized_components != sorted(normalized_components, key=lambda item: item["path"]):
        raise FormulaInventoryError("tap input components must be sorted")
    return canonical_sha256(
        {
            "normalized_formula_sha256": normalized_formula_sha256,
            "tap_input_components": normalized_components,
        }
    )


def _tap_component(root: Path, relative: str) -> dict[str, str]:
    candidate = root / relative
    if candidate.is_file() and not candidate.is_symlink():
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    elif candidate.is_dir() and not candidate.is_symlink():
        files: list[dict[str, str]] = []
        for path in sorted(candidate.rglob("*")):
            if path.is_symlink():
                raise FormulaInventoryError(f"tap input traverses a symlink: {path}")
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(candidate).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        if not files:
            raise FormulaInventoryError(f"tap input directory is empty: {relative}")
        digest = canonical_sha256({"files": files})
    else:
        raise FormulaInventoryError(f"tap input is not a direct file or directory: {relative}")
    return {"path": relative, "sha256": digest}


def _git(tap_root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=tap_root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise FormulaInventoryError(f"cannot inspect protected tap Git state: {error}") from error
    return completed.stdout.strip()


def _tree_identity(tap_root: Path, path: str) -> str:
    identity = _git(tap_root, "rev-parse", f"HEAD:{path}")
    if GIT_OBJECT.fullmatch(identity) is None:
        raise FormulaInventoryError(f"Git returned an invalid tree identity for {path}")
    return identity


def _validate_probe_shape(probe: Mapping[str, Any]) -> list[dict[str, Any]]:
    _exact_keys(probe, PROBE_KEYS, "Formula probe")
    if probe["schema"] != 1 or probe["kind"] != "kandelo-homebrew-formula-probe":
        raise FormulaInventoryError("Formula probe protocol is unsupported")
    if len(canonical_bytes(probe)) > MAX_PROBE_BYTES:
        raise FormulaInventoryError("Formula probe exceeds its byte bound")
    result: list[dict[str, Any]] = []
    for index, candidate in enumerate(_sequence(probe["formulae"], "Formula probe entries")):
        entry = _mapping(candidate, f"Formula probe entry {index}")
        _exact_keys(entry, FORMULA_KEYS, f"Formula probe entry {index}")
        name = _stable_id(entry["name"], f"Formula name {index}")
        if entry["formula_path"] != f"Formula/{name}.rb":
            raise FormulaInventoryError(f"Formula {name} path is not exact")
        architectures = list(_sequence(entry["architectures"], f"architectures for {name}"))
        if architectures != sorted(set(architectures)) or any(item not in {"wasm32", "wasm64"} for item in architectures):
            raise FormulaInventoryError(f"Formula {name} architecture list is invalid")
        _text(entry["version"], f"version for {name}", 128)
        _integer(entry["revision"], f"revision for {name}")
        _integer(entry["rebuild"], f"rebuild for {name}")
        for field, keys, identity_key in (
            ("target_dependencies", DEPENDENCY_KEYS, "name"),
            ("native_requirements", NATIVE_REQUIREMENT_KEYS, "identity"),
        ):
            previous = ""
            for item_index, item in enumerate(_sequence(entry[field], f"{field} for {name}")):
                value = _mapping(item, f"{field} {item_index} for {name}")
                _exact_keys(value, keys, f"{field} {item_index} for {name}")
                identity = (
                    _native_id(value[identity_key], f"{field} identity for {name}")
                    if identity_key == "identity"
                    else _stable_id(value[identity_key], f"{field} identity for {name}")
                )
                if identity <= previous:
                    raise FormulaInventoryError(f"{field} for {name} is not sorted and unique")
                previous = identity
                scopes = list(_sequence(value["scopes"], f"scopes for {identity}"))
                if scopes not in (["build"], ["build", "test"], ["runtime"], ["test"]):
                    raise FormulaInventoryError(f"dependency {identity} has unsupported scopes")
        previous_role = ""
        for source_index, item in enumerate(_sequence(entry["sources"], f"sources for {name}")):
            value = _mapping(item, f"source {source_index} for {name}")
            _exact_keys(value, SOURCE_KEYS, f"source {source_index} for {name}")
            role = _text(value["role"], f"source role for {name}", 256)
            if role <= previous_role:
                raise FormulaInventoryError(f"sources for {name} are not sorted and unique")
            previous_role = role
            mirrors = list(_sequence(value["mirrors"], f"mirrors for {role}"))
            _source_record(
                role=role,
                kind=value["kind"],
                url=value["url"],
                mirrors=mirrors,
                sha256=value["sha256"],
            )
        result.append(dict(entry))
    names = [entry["name"] for entry in result]
    if not names or names != sorted(set(names)):
        raise FormulaInventoryError("Formula probe entries must be sorted and duplicate-free")
    return result


def _validate_graph(formulae: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    names = {entry["name"] for entry in formulae}
    graph: dict[str, list[str]] = {}
    edge_count = 0
    for entry in formulae:
        dependencies = [item["name"] for item in entry["target_dependencies"]]
        unknown = sorted(set(dependencies) - names)
        if unknown:
            raise FormulaInventoryError(f"Formula {entry['name']} has unknown target dependencies {unknown!r}")
        graph[entry["name"]] = dependencies
        edge_count += len(dependencies)
    if edge_count > 65_536:
        raise FormulaInventoryError("Formula graph exceeds its edge bound")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise FormulaInventoryError(f"Formula dependency graph contains a cycle through {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in sorted(graph):
        visit(name)
    return graph


def validate_legacy_sidecar(parsed: Mapping[str, Any], sidecar: Mapping[str, Any]) -> None:
    """Check a legacy bottle record's self-consistency without using it as authority."""

    value = _mapping(sidecar, "legacy Formula sidecar")
    _exact_keys(value, SIDECAR_KEYS, "legacy Formula sidecar")
    name = parsed["name"]
    if (
        value["schema"] != 1
        or value["name"] != name
        or value["formula_path"] != parsed["formula_path"]
        or value["full_name"] != f"kandelo-dev/tap-core/{name}"
        or value["tap_name"] != "kandelo-dev/tap-core"
        or value["tap_repository"] != "kandelo-dev/homebrew-tap-core"
    ):
        raise FormulaInventoryError(f"legacy Formula sidecar identity drifted for {name}")
    revision = _integer(value["formula_revision"], f"legacy revision for {name}")
    version = _text(value["version"], f"legacy version for {name}", 128)
    expected_suffix = f"_{revision}" if revision else ""
    if expected_suffix:
        if not version.endswith(expected_suffix):
            raise FormulaInventoryError(f"legacy Formula revision/version disagree for {name}")
        base_version = version[: -len(expected_suffix)]
    else:
        base_version = version
    if base_version != parsed["version"]:
        raise FormulaInventoryError(f"legacy Formula version differs from current source for {name}")
    rebuild = _integer(value["bottle_rebuild"], f"legacy bottle rebuild for {name}")
    bottles = list(_sequence(value["bottles"], f"legacy bottles for {name}"))
    if not bottles:
        raise FormulaInventoryError(f"legacy sidecar has no bottle records for {name}")
    bottle_architectures: list[str] = []
    for index, candidate in enumerate(bottles):
        bottle = _mapping(candidate, f"legacy bottle {index} for {name}")
        architecture = bottle.get("arch")
        if architecture not in parsed["architectures"]:
            raise FormulaInventoryError(f"legacy bottle architecture drifted for {name}")
        link_manifest = bottle.get("link_manifest")
        if not isinstance(link_manifest, str) or f"-rebuild{rebuild}-{architecture}.json" not in link_manifest:
            raise FormulaInventoryError(f"legacy bottle rebuild drifted for {name}")
        bottle_architectures.append(architecture)
    if bottle_architectures != sorted(set(bottle_architectures)):
        raise FormulaInventoryError(f"legacy bottle architectures are not sorted and unique for {name}")
    runtime_dependencies = sorted(
        dependency["name"]
        for dependency in parsed["target_dependencies"]
        if "runtime" in dependency["scopes"]
    )
    sidecar_dependencies = []
    for candidate in _sequence(value["dependencies"], f"legacy dependencies for {name}"):
        dependency = _mapping(candidate, f"legacy dependency for {name}")
        if frozenset(dependency) not in (
            frozenset({"name", "version"}),
            frozenset({"full_name", "name", "version"}),
        ):
            raise FormulaInventoryError(f"legacy dependency shape drifted for {name}")
        dependency_name = _stable_id(dependency["name"], f"legacy dependency for {name}")
        if dependency.get("full_name", f"kandelo-dev/tap-core/{dependency_name}") != f"kandelo-dev/tap-core/{dependency_name}":
            raise FormulaInventoryError(f"legacy dependency identity drifted for {name}")
        _text(dependency["version"], f"legacy dependency version for {name}", 128)
        sidecar_dependencies.append(dependency_name)
    if (
        sidecar_dependencies != sorted(set(sidecar_dependencies))
        or not set(sidecar_dependencies).issubset(runtime_dependencies)
    ):
        raise FormulaInventoryError(f"legacy runtime dependency drifted for {name}")


def validate_formula_probe(
    tap_root: Path,
    probe: Mapping[str, Any],
    capture_policy: FormulaBuildInputPolicyV1,
    capture_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    root = tap_root.resolve(strict=True)
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise FormulaInventoryError("tap root is not the protected checkout root")
    entries = _validate_probe_shape(_mapping(probe, "Formula probe"))
    graph = _validate_graph(entries)
    expected_probe = build_static_formula_probe(root, capture_policy)
    if canonical_bytes(probe) != canonical_bytes(expected_probe):
        raise FormulaInventoryError("Formula probe differs from independently parsed protected source")
    catalog_entries = {
        entry["name"]: entry
        for entry in _sequence(capture_catalog.get("formulae"), "capture catalog Formulae")
    }
    if set(catalog_entries) != {entry["name"] for entry in entries}:
        raise FormulaInventoryError("capture catalog and Formula probe inventories differ")
    if _git(root, "diff", "--quiet", "HEAD", "--", "Formula", "Kandelo/formula"):
        raise FormulaInventoryError("unreachable Git diff result")
    # A successful --quiet command has empty stdout. Failures are raised by _git.
    generated = []
    for entry in entries:
        name = entry["name"]
        catalog = _mapping(catalog_entries[name], f"capture catalog entry for {name}")
        if list(catalog.get("architectures", ())) != entry["architectures"]:
            raise FormulaInventoryError(f"capture architecture drifted for {name}")
        formula_path = entry["formula_path"]
        normalized_formula_sha = hashlib.sha256(
            normalize_formula_source((root / formula_path).read_bytes())
        ).hexdigest()
        components = sorted(
            (
                _tap_component(root, relative)
                for relative in _sequence(catalog.get("tap_paths"), f"tap paths for {name}")
                if relative != formula_path
            ),
            key=lambda item: item["path"],
        )
        extended = {
            **entry,
            "normalized_formula_sha256": normalized_formula_sha,
            "tap_input_components": components,
            "normalized_source_sha256": combined_source_sha256(normalized_formula_sha, components),
            "capture_policy_sha256": _digest(
                catalog.get("capture_policy_sha256"), f"capture policy digest for {name}"
            ),
        }
        sidecar_path = root / f"Kandelo/formula/{name}.json"
        if sidecar_path.exists():
            if not sidecar_path.is_file() or sidecar_path.is_symlink():
                raise FormulaInventoryError(f"legacy sidecar is not a direct file for {name}")
            try:
                sidecar = json.loads(sidecar_path.read_bytes())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise FormulaInventoryError(f"cannot parse legacy sidecar for {name}: {error}") from error
            validate_legacy_sidecar(entry, _mapping(sidecar, f"legacy sidecar for {name}"))
        generated.append(extended)
    graph_identity = [
        {"name": name, "target_dependencies": graph[name]} for name in sorted(graph)
    ]
    return {
        "schema": 1,
        "kind": "kandelo-protected-formula-inventory",
        "formula_tree": _tree_identity(root, "Formula"),
        "sidecar_tree": _tree_identity(root, "Kandelo/formula"),
        "probe_sha256": canonical_sha256(probe),
        "capture_catalog_sha256": canonical_sha256(capture_catalog),
        "graph_sha256": canonical_sha256(graph_identity),
        "formulae": generated,
    }


def generate_formula_inventory(tap_root: Path) -> dict[str, Any]:
    from .policy import generate_formula_capture_catalog, load_formula_build_inputs

    root = tap_root.resolve(strict=True)
    policy = load_formula_build_inputs(
        root / "Kandelo/staging/formula-build-inputs.toml", tap_root=root
    )
    catalog = generate_formula_capture_catalog(root, policy)
    return validate_formula_probe(root, build_static_formula_probe(root, policy), policy, catalog)


def write_formula_inventory_fixture(tap_root: Path, destination: Path) -> None:
    root = tap_root.resolve(strict=True)
    expected = root / "Kandelo/staging/fixtures/formula-inventory.json"
    if destination.resolve(strict=False) != expected.resolve(strict=False):
        raise FormulaInventoryError("Formula inventory fixture must use its protected path")
    parent = expected.parent
    parent.mkdir(parents=True, exist_ok=True)
    relative_parent = parent.relative_to(root)
    current = root
    for component in relative_parent.parts:
        current /= component
        if current.is_symlink():
            raise FormulaInventoryError("Formula inventory fixture parent traverses a symlink")
    if expected.is_symlink() or (expected.exists() and not expected.is_file()):
        raise FormulaInventoryError("Formula inventory fixture is not a direct regular file")
    body = canonical_bytes(generate_formula_inventory(root))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{expected.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, expected)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def load_formula_inventory(body: bytes) -> dict[str, Any]:
    try:
        parsed = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_INVENTORY_BYTES))
    except CanonicalJsonError as error:
        raise FormulaInventoryError(f"Formula inventory is not canonical: {error}") from error
    value = _mapping(parsed, "Formula inventory")
    _exact_keys(value, INVENTORY_KEYS, "Formula inventory")
    if value["schema"] != 1 or value["kind"] != "kandelo-protected-formula-inventory":
        raise FormulaInventoryError("Formula inventory protocol is unsupported")
    for field in ("probe_sha256", "capture_catalog_sha256", "graph_sha256"):
        _digest(value[field], field)
    for field in ("formula_tree", "sidecar_tree"):
        identity = _text(value[field], field, 64)
        if GIT_OBJECT.fullmatch(identity) is None:
            raise FormulaInventoryError(f"{field} is not a Git object identity")
    formulae = list(_sequence(value["formulae"], "inventory Formulae"))
    names: list[str] = []
    for index, candidate in enumerate(formulae):
        entry = _mapping(candidate, f"inventory Formula {index}")
        _exact_keys(entry, INVENTORY_FORMULA_KEYS, f"inventory Formula {index}")
        names.append(_stable_id(entry["name"], f"inventory Formula name {index}"))
        for field in (
            "normalized_formula_sha256",
            "normalized_source_sha256",
            "capture_policy_sha256",
        ):
            _digest(entry[field], f"{field} for {entry['name']}")
    if not names or names != sorted(set(names)):
        raise FormulaInventoryError("inventory Formulae must be sorted and duplicate-free")
    return dict(value)
