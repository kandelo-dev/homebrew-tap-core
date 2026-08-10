"""Protected, generic N -> N+1 tap-history planning and verification.

This module separates immutable history facts from the GitHub workflow that
may write them.  Protection is derived from bounded branch-protection or
ruleset responses; callers cannot supply a Boolean "protected" assertion.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Literal, Protocol
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from .canonical import canonical_bytes, canonical_sha256
from .oci import OciPublicationError, UrllibOciTransportV1, fetch_public_blob
from .records import (
    OciBlobV1,
    OciRecordPlanV1,
    TapRecordError,
    validate_abi_history_record,
)
from .tap_metadata import (
    TapMetadataError,
    check_tap_metadata,
    load_abi_state,
    load_promotion_activation,
    load_promotion_policy,
)


MAX_HISTORY_BYTES = 4 * 1024 * 1024
MAX_BOTTLE_BYTES = 8 * 1024 * 1024 * 1024
MAX_RULESETS = 256
MAX_RULES = 64
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(
    r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*/"
    r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$"
)
BRANCH = re.compile(r"^abi/(0|[1-9][0-9]{0,9})$")
HISTORY_RECORD_MEDIA_TYPE = "application/vnd.kandelo.abi-history.record.v1+json"


class AbiHistoryError(ValueError):
    """Raised when history authority, protection, or readback is incomplete."""


@dataclass(frozen=True)
class GitRefV1:
    object_sha: str
    tree_sha: str


class HistoryRefStore(Protocol):
    def read(self, branch: str) -> GitRefV1 | None: ...

    def create(self, branch: str, object_sha: str) -> GitRefV1: ...


@dataclass(frozen=True)
class _GitHubResponseV1:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise AbiHistoryError(f"GitHub response repeats field {key!r}")
        value[key] = child
    return value


def _reject_json_number(value: str) -> None:
    raise AbiHistoryError(f"GitHub response contains unsupported number {value}")


def _parse_json(body: bytes, field: str) -> Any:
    try:
        return json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_pairs,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AbiHistoryError(f"{field} is invalid JSON: {error}") from error


class GitHubHistoryClient:
    """Bounded GitHub REST adapter for one protected tap repository."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        opener: Callable[[urllib.request.Request], Any] | None = None,
    ) -> None:
        self.repository = _repository(repository, "GitHub history repository")
        if not isinstance(token, str) or "\0" in token:
            raise AbiHistoryError("GitHub history token is invalid")
        try:
            token_bytes = token.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise AbiHistoryError("GitHub history token is not UTF-8") from error
        if len(token_bytes) > 8192:
            raise AbiHistoryError("GitHub history token exceeds its bound")
        self.token = token
        if opener is None:
            handler = urllib.request.build_opener(_NoRedirect())

            def open_request(request: urllib.request.Request) -> Any:
                try:
                    return handler.open(request, timeout=30)
                except urllib.error.HTTPError as error:
                    return error

            self._opener = open_request
        else:
            self._opener = opener

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        maximum: int = 4 * 1024 * 1024,
    ) -> _GitHubResponseV1:
        if not path.startswith("/") or ".." in path or any(
            ord(character) <= 0x20 for character in path
        ):
            raise AbiHistoryError("GitHub history API path is unsafe")
        url = "https://api.github.com" + path
        payload = None if body is None else canonical_bytes(body)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "kandelo-abi-history/1",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": "Bearer " + self.token} if self.token else {}),
            **({"Content-Type": "application/json"} if payload is not None else {}),
        }
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers=headers,
        )
        try:
            response = self._opener(request)
        except (OSError, urllib.error.URLError) as error:
            raise AbiHistoryError(f"GitHub history API request failed: {error}") from error
        try:
            status = int(response.status)
            location = response.headers.get("Location") if hasattr(response.headers, "get") else None
            if 300 <= status < 400 or location is not None:
                raise AbiHistoryError("GitHub history API attempted a redirect")
            content_length = response.headers.get("Content-Length") if hasattr(response.headers, "get") else None
            if content_length is not None:
                if not content_length.isascii() or not content_length.isdigit() or int(content_length) > maximum:
                    raise AbiHistoryError("GitHub history API response exceeds its bound")
            response_body = response.read(maximum + 1)
            if len(response_body) > maximum:
                raise AbiHistoryError("GitHub history API response exceeds its bound")
            if content_length is not None and int(content_length) != len(response_body):
                raise AbiHistoryError("GitHub history API Content-Length drifted")
            headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            return _GitHubResponseV1(status, headers, response_body)
        finally:
            response.close()

    def _json(self, method: str, path: str, *, body: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        response = self._request(method, path, body=body)
        value = None if not response.body else _parse_json(response.body, "GitHub history response")
        return response.status, value

    def _ref_path(self, branch: str) -> str:
        if BRANCH.fullmatch(branch) is None and branch != "main":
            raise AbiHistoryError("GitHub ref adapter accepts only main or exact abi/N")
        encoded = urllib.parse.quote(branch, safe="")
        return f"/repos/{self.repository}/git/ref/heads/{encoded}"

    def read(self, branch: str) -> GitRefV1 | None:
        status, value = self._json("GET", self._ref_path(branch))
        if status == 404:
            return None
        if status != 200:
            raise AbiHistoryError(f"GitHub ref read returned HTTP {status}")
        response = _mapping(value, "GitHub ref response")
        if response.get("ref") != f"refs/heads/{branch}":
            raise AbiHistoryError("GitHub ref response names another branch")
        object_value = _mapping(response.get("object"), "GitHub ref object")
        if object_value.get("type") != "commit":
            raise AbiHistoryError("GitHub history ref does not target a commit")
        commit = _git_sha(object_value.get("sha"), "GitHub ref object")
        status, value = self._json(
            "GET", f"/repos/{self.repository}/git/commits/{commit}"
        )
        if status != 200:
            raise AbiHistoryError(f"GitHub commit read returned HTTP {status}")
        commit_value = _mapping(value, "GitHub commit response")
        if commit_value.get("sha") != commit:
            raise AbiHistoryError("GitHub commit response names another object")
        tree = _mapping(commit_value.get("tree"), "GitHub commit tree")
        return GitRefV1(commit, _git_sha(tree.get("sha"), "GitHub commit tree"))

    def create(self, branch: str, object_sha: str) -> GitRefV1:
        if BRANCH.fullmatch(branch) is None:
            raise AbiHistoryError("GitHub history creation accepts only exact abi/N")
        commit = _git_sha(object_sha, "GitHub history ref object")
        status, _ = self._json(
            "POST",
            f"/repos/{self.repository}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": commit},
        )
        if status not in {201, 422}:
            raise AbiHistoryError(f"GitHub history ref creation returned HTTP {status}")
        created = self.read(branch)
        if created is None or created.object_sha != commit:
            raise AbiHistoryError("GitHub history ref creation did not converge exactly")
        return created

    def _direct_protection(self, branch: str, reference: GitRefV1 | None) -> dict[str, Any] | None:
        if reference is None:
            return None
        encoded = urllib.parse.quote(branch, safe="")
        status, value = self._json(
            "GET", f"/repos/{self.repository}/branches/{encoded}/protection"
        )
        if status in {403, 404}:
            return None
        if status != 200:
            raise AbiHistoryError(f"GitHub branch protection read returned HTTP {status}")
        response = _mapping(value, "GitHub branch protection")

        def enabled(name: str, *, default: bool) -> bool:
            raw = response.get(name)
            if raw is None:
                return default
            child = _mapping(raw, f"GitHub branch protection {name}")
            value = child.get("enabled")
            if not isinstance(value, bool):
                raise AbiHistoryError(f"GitHub branch protection {name} is malformed")
            return value

        return {
            "branch": branch,
            "allow_deletions": enabled("allow_deletions", default=False),
            "allow_force_pushes": enabled("allow_force_pushes", default=False),
            "enforce_admins": enabled("enforce_admins", default=False),
        }

    def _rulesets(self) -> list[dict[str, Any]]:
        status, value = self._json(
            "GET", f"/repos/{self.repository}/rulesets?includes_parents=true&per_page=100"
        )
        if status != 200:
            raise AbiHistoryError(f"GitHub ruleset inventory returned HTTP {status}")
        inventory = list(_sequence(value, "GitHub ruleset inventory"))
        if len(inventory) > MAX_RULESETS:
            raise AbiHistoryError("GitHub ruleset inventory exceeds its bound")
        if len(inventory) == 100:
            raise AbiHistoryError("GitHub ruleset inventory may be paginated")
        normalized: list[dict[str, Any]] = []
        ids: list[int] = []
        for item in inventory:
            summary = _mapping(item, "GitHub ruleset summary")
            identifier = _positive(summary.get("id"), "GitHub ruleset ID")
            ids.append(identifier)
        if len(ids) != len(set(ids)):
            raise AbiHistoryError("GitHub ruleset inventory repeats an identity")
        ids.sort()
        for identifier in ids:
            status, value = self._json(
                "GET", f"/repos/{self.repository}/rulesets/{identifier}"
            )
            if status != 200:
                raise AbiHistoryError(f"GitHub ruleset read returned HTTP {status}")
            ruleset = _mapping(value, "GitHub ruleset")
            if ruleset.get("id") != identifier:
                raise AbiHistoryError("GitHub ruleset response changed identity")
            conditions = _mapping(ruleset.get("conditions"), "GitHub ruleset conditions")
            ref_name = _mapping(conditions.get("ref_name"), "GitHub ruleset ref condition")
            raw_rules = list(_sequence(ruleset.get("rules"), "GitHub ruleset rules"))
            raw_bypass = list(
                _sequence(ruleset.get("bypass_actors", []), "GitHub ruleset bypass actors")
            )
            if len(raw_rules) > MAX_RULES or len(raw_bypass) > MAX_RULES:
                raise AbiHistoryError("GitHub ruleset exceeds its item bound")
            rule_types = sorted(
                {
                    _text(_mapping(rule, "GitHub ruleset rule").get("type"), "GitHub ruleset rule type", 64)
                    for rule in raw_rules
                }
            )
            bypass = []
            for actor_value in raw_bypass:
                actor = _mapping(actor_value, "GitHub ruleset bypass actor")
                bypass.append(
                    {
                        "actor_id": _positive(actor.get("actor_id"), "GitHub bypass actor ID"),
                        "actor_type": _text(actor.get("actor_type"), "GitHub bypass actor type", 128),
                        "bypass_mode": _text(actor.get("bypass_mode"), "GitHub bypass mode", 64),
                    }
                )
            bypass.sort(key=lambda item: (item["actor_id"], item["actor_type"], item["bypass_mode"]))
            normalized.append(
                {
                    "id": identifier,
                    "name": _text(ruleset.get("name"), "GitHub ruleset name", 255),
                    "target": _text(ruleset.get("target"), "GitHub ruleset target", 64),
                    "enforcement": _text(ruleset.get("enforcement"), "GitHub ruleset enforcement", 64),
                    "include": sorted(
                        _text(item, "GitHub ruleset include", 255)
                        for item in _sequence(ref_name.get("include"), "GitHub ruleset includes")
                    ),
                    "exclude": sorted(
                        _text(item, "GitHub ruleset exclude", 255)
                        for item in _sequence(ref_name.get("exclude"), "GitHub ruleset excludes")
                    ),
                    "rules": rule_types,
                    "bypass_actors": bypass,
                }
            )
        return normalized

    def protection_snapshot(
        self, branch: str, *, phase: Literal["precreate", "postcreate"]
    ) -> dict[str, Any]:
        reference = self.read(branch)
        return {
            "schema": 1,
            "kind": "kandelo-abi-history-protection-snapshot",
            "repository": self.repository,
            "branch": branch,
            "phase": phase,
            "ref": (
                None
                if reference is None
                else {"object": reference.object_sha, "tree": reference.tree_sha}
            ),
            "direct": self._direct_protection(branch, reference),
            "rulesets": self._rulesets(),
        }


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise AbiHistoryError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AbiHistoryError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise AbiHistoryError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise AbiHistoryError(f"{field} must be a string")
    try:
        body = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise AbiHistoryError(f"{field} is not UTF-8") from error
    if not body or len(body) > maximum or "\0" in value:
        raise AbiHistoryError(f"{field} is outside its string bound")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**32 - 1:
        raise AbiHistoryError(f"{field} must be a bounded nonnegative integer")
    return value


def _positive(value: Any, field: str) -> int:
    checked = _integer(value, field)
    if checked == 0:
        raise AbiHistoryError(f"{field} must be positive")
    return checked


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise AbiHistoryError(f"{field} is not a full lowercase Git SHA")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise AbiHistoryError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _repository(value: Any, field: str) -> str:
    checked = _text(value, field, 255)
    if REPOSITORY.fullmatch(checked) is None:
        raise AbiHistoryError(f"{field} is not owner/name")
    return checked


def _branch(value: Any, source_abi: int, field: str = "history branch") -> str:
    checked = _text(value, field, 128)
    if checked != f"abi/{source_abi}" or BRANCH.fullmatch(checked) is None:
        raise AbiHistoryError(f"{field} is not exact abi/N")
    return checked


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def protection_requirement(branch_prefix: str = "abi/") -> dict[str, Any]:
    if branch_prefix != "abi/":
        raise AbiHistoryError("historical branch prefix must remain exact abi/")
    return {
        "schema": 1,
        "kind": "kandelo-abi-history-protection-requirement",
        "branch_pattern": "refs/heads/abi/*",
        "allow_force_pushes": False,
        "allow_deletions": False,
        "enforce_admins": True,
        "required_ruleset_rules": ["deletion", "non_fast_forward"],
        "allow_always_bypass": False,
    }


def protection_requirement_sha256(branch_prefix: str = "abi/") -> str:
    return canonical_sha256(protection_requirement(branch_prefix))


def validate_history_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(plan, "ABI history plan")
    _exact_keys(
        value,
        frozenset(
            {
                "source_abi",
                "successor_abi",
                "preactivation_tap_commit",
                "preactivation_tap_tree",
                "branch",
                "expected_current_metadata_sha256",
                "protection_requirement_sha256",
            }
        ),
        "ABI history plan",
    )
    source_abi = _integer(value["source_abi"], "history source ABI")
    successor_abi = _integer(value["successor_abi"], "history successor ABI")
    if source_abi == 2**32 - 1 or successor_abi != source_abi + 1:
        raise AbiHistoryError("ABI history transition must be exactly N to N+1")
    checked = {
        "source_abi": source_abi,
        "successor_abi": successor_abi,
        "preactivation_tap_commit": _git_sha(
            value["preactivation_tap_commit"], "history preactivation commit"
        ),
        "preactivation_tap_tree": _git_sha(
            value["preactivation_tap_tree"], "history preactivation tree"
        ),
        "branch": _branch(value["branch"], source_abi),
        "expected_current_metadata_sha256": _digest(
            value["expected_current_metadata_sha256"], "history current metadata"
        ),
        "protection_requirement_sha256": _digest(
            value["protection_requirement_sha256"], "history protection requirement"
        ),
    }
    if checked["protection_requirement_sha256"] != protection_requirement_sha256():
        raise AbiHistoryError("history protection requirement differs from protected policy")
    return checked


def _git(root: Path, *arguments: str, allow_missing: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AbiHistoryError(f"cannot inspect local Git history: {error}") from error
    if result.returncode != 0:
        if allow_missing and result.returncode == 1 and not result.stdout.strip():
            return None
        diagnostic = result.stderr.strip()[:512]
        raise AbiHistoryError(f"local Git history command failed: {diagnostic}")
    return result.stdout.strip()


def _checked_git_root(root: Path) -> Path:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise AbiHistoryError("tap checkout must be a real directory")
    top = _git(resolved, "rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve(strict=True) != resolved:
        raise AbiHistoryError("tap checkout is not the exact Git worktree root")
    return resolved


def build_history_plan(
    tap_root: Path,
    *,
    preactivation_tap_commit: str,
    preactivation_tap_tree: str,
) -> dict[str, Any]:
    root = _checked_git_root(tap_root)
    commit = _git_sha(preactivation_tap_commit, "preactivation tap commit")
    tree = _git_sha(preactivation_tap_tree, "preactivation tap tree")
    if _git(root, "rev-parse", "HEAD") != commit or _git(root, "rev-parse", "HEAD^{tree}") != tree:
        raise AbiHistoryError("preactivation tap checkout differs from its exact commit/tree")
    try:
        policy = load_promotion_policy(root / "Kandelo/staging/promotion-policy.toml")
        load_promotion_activation(root / "Kandelo/staging/promotion-activation.toml")
        state = load_abi_state(root / "Kandelo/abi-state.json")
        projection = check_tap_metadata(root)
    except TapMetadataError as error:
        raise AbiHistoryError(f"cannot plan protected ABI history: {error}") from error
    if not policy.require_branch_protection:
        raise AbiHistoryError("ABI history requires branch protection")
    if state.current_abi == 2**32 - 1:
        raise AbiHistoryError("current ABI has no representable successor")
    plan = {
        "source_abi": state.current_abi,
        "successor_abi": state.current_abi + 1,
        "preactivation_tap_commit": commit,
        "preactivation_tap_tree": tree,
        "branch": f"{policy.historical_branch_prefix}{state.current_abi}",
        "expected_current_metadata_sha256": projection["active_projection_sha256"],
        "protection_requirement_sha256": protection_requirement_sha256(
            policy.historical_branch_prefix
        ),
    }
    return validate_history_plan(plan)


class LocalGitRefStore:
    """No-force local ref adapter used by deterministic history tests."""

    def __init__(self, root: Path) -> None:
        self.root = _checked_git_root(root)

    def read(self, branch: str) -> GitRefV1 | None:
        if BRANCH.fullmatch(branch) is None and branch != "main":
            raise AbiHistoryError("Git ref adapter accepts only main or exact abi/N")
        object_sha = _git(
            self.root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            allow_missing=True,
        )
        if object_sha is None:
            return None
        checked_object = _git_sha(object_sha, "local ref object")
        tree = _git(self.root, "rev-parse", f"{checked_object}^{{tree}}")
        if tree is None:
            raise AbiHistoryError("local ref tree is absent")
        return GitRefV1(checked_object, _git_sha(tree, "local ref tree"))

    def create(self, branch: str, object_sha: str) -> GitRefV1:
        if BRANCH.fullmatch(branch) is None:
            raise AbiHistoryError("history ref creation accepts only exact abi/N")
        commit = _git_sha(object_sha, "history ref object")
        _git(
            self.root,
            "update-ref",
            f"refs/heads/{branch}",
            commit,
            "0" * 40,
        )
        created = self.read(branch)
        if created is None:
            raise AbiHistoryError("history ref creation did not become visible")
        return created


def _checked_ref(value: Any, field: str) -> GitRefV1 | None:
    if value is None:
        return None
    reference = _mapping(value, field)
    _exact_keys(reference, frozenset({"object", "tree"}), field)
    return GitRefV1(
        _git_sha(reference["object"], f"{field} object"),
        _git_sha(reference["tree"], f"{field} tree"),
    )


def _ruleset_matches(branch: str, ruleset: Mapping[str, Any]) -> bool:
    _exact_keys(
        ruleset,
        frozenset(
            {
                "id",
                "name",
                "target",
                "enforcement",
                "include",
                "exclude",
                "rules",
                "bypass_actors",
            }
        ),
        "history ruleset",
    )
    _positive(ruleset["id"], "history ruleset ID")
    _text(ruleset["name"], "history ruleset name", 255)
    include = [_text(item, "ruleset include", 255) for item in _sequence(ruleset["include"], "ruleset includes")]
    exclude = [_text(item, "ruleset exclude", 255) for item in _sequence(ruleset["exclude"], "ruleset excludes")]
    rules = [_text(item, "ruleset rule", 64) for item in _sequence(ruleset["rules"], "ruleset rules")]
    bypass = list(_sequence(ruleset["bypass_actors"], "ruleset bypass actors"))
    if len(include) > MAX_RULES or len(exclude) > MAX_RULES or len(rules) > MAX_RULES or len(bypass) > MAX_RULES:
        raise AbiHistoryError("history ruleset exceeds its item bound")
    if include != sorted(set(include)) or exclude != sorted(set(exclude)) or rules != sorted(set(rules)):
        raise AbiHistoryError("history ruleset arrays must be sorted and duplicate-free")
    for actor in bypass:
        value = _mapping(actor, "ruleset bypass actor")
        _exact_keys(
            value,
            frozenset({"actor_id", "actor_type", "bypass_mode"}),
            "ruleset bypass actor",
        )
        _positive(value["actor_id"], "ruleset bypass actor ID")
        _text(value["actor_type"], "ruleset bypass actor type", 128)
        mode = _text(value["bypass_mode"], "ruleset bypass mode", 64)
        if mode not in {"always", "pull_request"}:
            raise AbiHistoryError("ruleset bypass mode is unsupported")
        if mode == "always":
            return False
    ref = f"refs/heads/{branch}"
    matches_include = ref in include or "refs/heads/abi/*" in include
    return (
        ruleset["target"] == "branch"
        and ruleset["enforcement"] == "active"
        and matches_include
        and not exclude
        and {"deletion", "non_fast_forward"}.issubset(rules)
    )


def validate_protection_snapshot(
    plan: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    phase: Literal["precreate", "postcreate"],
    expected_repository: str = "kandelo-dev/homebrew-tap-core",
) -> dict[str, Any]:
    checked_plan = validate_history_plan(plan)
    repository = _repository(expected_repository, "expected tap repository")
    value = _mapping(snapshot, "history protection snapshot")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "repository",
                "branch",
                "phase",
                "ref",
                "direct",
                "rulesets",
            }
        ),
        "history protection snapshot",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-history-protection-snapshot":
        raise AbiHistoryError("history protection snapshot protocol is unsupported")
    if _repository(value["repository"], "protection repository").lower() != repository.lower():
        raise AbiHistoryError("history protection response names another repository")
    if value["branch"] != checked_plan["branch"]:
        raise AbiHistoryError("history protection response names another branch")
    if value["phase"] != phase:
        raise AbiHistoryError("history protection snapshot phase is stale")
    reference = _checked_ref(value["ref"], "history protection ref")
    expected_ref = GitRefV1(
        checked_plan["preactivation_tap_commit"],
        checked_plan["preactivation_tap_tree"],
    )
    if phase == "postcreate" and reference != expected_ref:
        raise AbiHistoryError("postcreate history protection ref differs from the plan")
    if phase == "precreate" and reference not in {None, expected_ref}:
        raise AbiHistoryError("precreate history protection ref differs from the plan")

    direct = value["direct"]
    direct_covered = False
    if direct is not None:
        protection = _mapping(direct, "direct branch protection")
        _exact_keys(
            protection,
            frozenset(
                {"branch", "allow_deletions", "allow_force_pushes", "enforce_admins"}
            ),
            "direct branch protection",
        )
        if protection["branch"] != checked_plan["branch"]:
            raise AbiHistoryError("direct protection response names another branch")
        direct_covered = (
            reference == expected_ref
            and protection["allow_deletions"] is False
            and protection["allow_force_pushes"] is False
            and protection["enforce_admins"] is True
        )

    rulesets = list(_sequence(value["rulesets"], "history rulesets"))
    if len(rulesets) > MAX_RULESETS:
        raise AbiHistoryError("history protection has too many rulesets")
    ruleset_matches = [
        _ruleset_matches(checked_plan["branch"], _mapping(item, "history ruleset"))
        for item in rulesets
    ]
    ruleset_covered = any(ruleset_matches)
    if not direct_covered and not ruleset_covered:
        raise AbiHistoryError("history branch lacks active nonbypass protection")
    source = "branch-protection" if direct_covered else "ruleset"
    return {
        "branch": checked_plan["branch"],
        "covered": True,
        "observed_protection_sha256": canonical_sha256(value),
        "protection_requirement_sha256": checked_plan[
            "protection_requirement_sha256"
        ],
        "ref_object": checked_plan["preactivation_tap_commit"],
        "ref_tree": checked_plan["preactivation_tap_tree"],
        "source": source,
    }


def ensure_history_ref(
    plan: Mapping[str, Any],
    store: HistoryRefStore,
    protection_snapshot: Mapping[str, Any],
    *,
    mode: Literal["disabled", "observe", "active"],
    expected_repository: str = "kandelo-dev/homebrew-tap-core",
) -> dict[str, Any]:
    checked = validate_history_plan(plan)
    existing = store.read(checked["branch"])
    expected = GitRefV1(
        checked["preactivation_tap_commit"], checked["preactivation_tap_tree"]
    )
    if existing is not None and existing != expected:
        raise AbiHistoryError("history branch already exists at another object/tree")
    evidence = validate_protection_snapshot(
        checked,
        protection_snapshot,
        phase="precreate",
        expected_repository=expected_repository,
    )
    snapshot_ref = _checked_ref(protection_snapshot["ref"], "history protection ref")
    if existing != snapshot_ref:
        raise AbiHistoryError("history ref changed after protection preflight")
    if existing is not None:
        return {
            "action": "already-exact",
            "ref": {"object": existing.object_sha, "tree": existing.tree_sha},
            "protection_evidence": evidence,
        }
    if mode == "disabled":
        return {"action": "disabled", "ref": None, "protection_evidence": evidence}
    if mode == "observe":
        return {"action": "would-create", "ref": None, "protection_evidence": evidence}
    if mode != "active":
        raise AbiHistoryError("history creation mode is unsupported")
    main = store.read("main")
    if main != expected:
        raise AbiHistoryError("tap main moved before history ref creation")
    created = store.create(checked["branch"], checked["preactivation_tap_commit"])
    if created != expected or store.read(checked["branch"]) != expected:
        raise AbiHistoryError("created history ref differs from the exact preactivation source")
    return {
        "action": "created",
        "ref": {"object": created.object_sha, "tree": created.tree_sha},
        "protection_evidence": evidence,
    }


def validate_history_creation_handoff(
    plan: Mapping[str, Any], handoff: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the exact protected result crossing into history verification."""

    checked = validate_history_plan(plan)
    value = _mapping(handoff, "history ref creation")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "plan_sha256",
                "action",
                "ref",
                "protection_evidence",
            }
        ),
        "history ref creation",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-history-ref-creation":
        raise AbiHistoryError("history ref creation protocol is unsupported")
    if _digest(value["plan_sha256"], "history creation plan") != canonical_sha256(
        checked
    ):
        raise AbiHistoryError("history creation plan digest differs from the exact plan")
    action = _text(value["action"], "history creation action", 32)
    if action not in {"created", "already-exact"}:
        raise AbiHistoryError("history creation action is not publishable")
    reference = _checked_ref(value["ref"], "history creation ref")
    expected = GitRefV1(
        checked["preactivation_tap_commit"], checked["preactivation_tap_tree"]
    )
    if reference != expected:
        raise AbiHistoryError("history creation ref differs from the exact plan")

    evidence = _mapping(value["protection_evidence"], "history protection evidence")
    _exact_keys(
        evidence,
        frozenset(
            {
                "branch",
                "covered",
                "observed_protection_sha256",
                "protection_requirement_sha256",
                "ref_object",
                "ref_tree",
                "source",
            }
        ),
        "history protection evidence",
    )
    if (
        evidence["covered"] is not True
        or evidence["branch"] != checked["branch"]
        or _git_sha(evidence["ref_object"], "history protection ref object")
        != expected.object_sha
        or _git_sha(evidence["ref_tree"], "history protection ref tree")
        != expected.tree_sha
        or _digest(
            evidence["protection_requirement_sha256"],
            "history protection requirement",
        )
        != checked["protection_requirement_sha256"]
    ):
        raise AbiHistoryError("history creation protection differs from the exact plan")
    _digest(
        evidence["observed_protection_sha256"], "observed history protection"
    )
    if evidence["source"] not in {"branch-protection", "ruleset"}:
        raise AbiHistoryError("history creation protection source is unsupported")
    return json.loads(canonical_bytes(value))


def _load_metadata(root: Path) -> Mapping[str, Any]:
    path = root / "Kandelo/metadata.json"
    try:
        metadata = path.lstat()
        body = path.read_bytes()
    except OSError as error:
        raise AbiHistoryError(f"cannot read historical metadata: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or not 1 <= len(body) <= 32 * 1024 * 1024:
        raise AbiHistoryError("historical metadata must be a bounded regular file")
    try:
        value = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AbiHistoryError(f"historical metadata is invalid JSON: {error}") from error
    if not isinstance(value, Mapping):
        raise AbiHistoryError("historical metadata must be an object")
    return value


def _default_anonymous_reader(url: str, maximum: int) -> bytes:
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= MAX_BOTTLE_BYTES:
        raise AbiHistoryError("public bottle byte bound is invalid")
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise AbiHistoryError(f"public bottle URL is invalid: {error}") from error
    match = re.fullmatch(
        r"/v2/(?P<repository>"
        r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
        r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
        r")/blobs/sha256:(?P<digest>[0-9a-f]{64})",
        parsed.path,
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ghcr.io"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise AbiHistoryError("public bottle URL escaped exact anonymous GHCR")
    try:
        return fetch_public_blob(
            f"ghcr.io/{match.group('repository')}@sha256:{match.group('digest')}",
            expected_sha256=match.group("digest"),
            expected_bytes=maximum,
            transport=UrllibOciTransportV1(username="", token=""),
        )
    except OciPublicationError as error:
        raise AbiHistoryError(f"anonymous bottle readback failed: {error}") from error


def verify_history_snapshot(
    tap_root: Path,
    plan: Mapping[str, Any],
    store: HistoryRefStore,
    protection_snapshot: Mapping[str, Any],
    *,
    anonymous_reader: Callable[[str, int], bytes] = _default_anonymous_reader,
    expected_repository: str = "kandelo-dev/homebrew-tap-core",
) -> dict[str, Any]:
    checked = validate_history_plan(plan)
    root = _checked_git_root(tap_root)
    expected = GitRefV1(
        checked["preactivation_tap_commit"], checked["preactivation_tap_tree"]
    )
    if store.read(checked["branch"]) != expected:
        raise AbiHistoryError("public history branch differs from the planned ref/tree")
    if _git(root, "rev-parse", "HEAD") != expected.object_sha or _git(root, "rev-parse", "HEAD^{tree}") != expected.tree_sha:
        raise AbiHistoryError("history verification checkout differs from the planned ref/tree")
    evidence = validate_protection_snapshot(
        checked,
        protection_snapshot,
        phase="postcreate",
        expected_repository=expected_repository,
    )
    try:
        projection = check_tap_metadata(root)
    except TapMetadataError as error:
        raise AbiHistoryError(f"historical Formula metadata is invalid: {error}") from error
    if projection["active_projection_sha256"] != checked["expected_current_metadata_sha256"]:
        raise AbiHistoryError("historical Formula metadata differs from the planned projection")
    metadata = _load_metadata(root)
    packages = _sequence(metadata.get("packages"), "historical packages")
    readbacks: list[dict[str, Any]] = []
    for package_index, package_value in enumerate(packages):
        package = _mapping(package_value, f"historical package {package_index}")
        formula = _text(package.get("name"), "historical Formula name", 128)
        bottles = _sequence(package.get("bottles"), f"historical bottles for {formula}")
        if not bottles:
            raise AbiHistoryError(f"historical Formula {formula} has no public bottle")
        for bottle_index, bottle_value in enumerate(bottles):
            bottle = _mapping(bottle_value, f"historical bottle {formula} {bottle_index}")
            architecture = _text(bottle.get("arch"), "historical bottle architecture", 64)
            url = _text(bottle.get("url"), "historical bottle URL", 4096)
            digest = _digest(bottle.get("sha256"), "historical bottle digest")
            size = _positive(bottle.get("bytes"), "historical bottle bytes")
            if size > MAX_BOTTLE_BYTES:
                raise AbiHistoryError("historical bottle exceeds its byte bound")
            try:
                body = anonymous_reader(url, size)
            except AbiHistoryError:
                raise
            except Exception as error:
                raise AbiHistoryError(f"anonymous bottle readback failed: {error}") from error
            if not isinstance(body, bytes) or len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                raise AbiHistoryError(f"historical bottle readback drifted for {formula}/{architecture}")
            readbacks.append(
                {
                    "formula": formula,
                    "architecture": architecture,
                    "sha256": digest,
                    "bytes": size,
                    "url": url,
                }
            )
    ordered = sorted(readbacks, key=lambda item: (item["formula"], item["architecture"]))
    if readbacks != ordered or len({(item["formula"], item["architecture"]) for item in ordered}) != len(ordered):
        raise AbiHistoryError("historical bottle projection is not sorted and unique")
    metadata_verification = {
        "schema": 1,
        "kind": "kandelo-abi-history-metadata-verification",
        "ref_object": expected.object_sha,
        "ref_tree": expected.tree_sha,
        "projection": projection,
    }
    public_readback = {
        "schema": 1,
        "kind": "kandelo-abi-history-public-readback",
        "ref_object": expected.object_sha,
        "bottles": ordered,
    }
    return {
        "protection_evidence": evidence,
        "metadata_verification_sha256": canonical_sha256(metadata_verification),
        "public_readback_sha256": canonical_sha256(public_readback),
        "metadata_verification": metadata_verification,
        "public_readback": public_readback,
    }


def build_history_record(
    plan: Mapping[str, Any],
    *,
    created_ref_object: str,
    protection_evidence: Mapping[str, Any],
    metadata_verification_sha256: str,
    public_readback_sha256: str,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    record = {
        "schema": 1,
        "kind": "kandelo-abi-history-record",
        "plan": validate_history_plan(plan),
        "created_ref_object": _git_sha(created_ref_object, "created history ref"),
        "protection_evidence": _plain(protection_evidence),
        "metadata_verification_sha256": _digest(
            metadata_verification_sha256, "history metadata verification"
        ),
        "public_readback_sha256": _digest(
            public_readback_sha256, "history public readback"
        ),
        "run": _plain(run),
    }
    try:
        validate_abi_history_record(record)
    except TapRecordError as error:
        raise AbiHistoryError(f"ABI history record is invalid: {error}") from error
    if len(canonical_bytes(record)) > MAX_HISTORY_BYTES:
        raise AbiHistoryError("ABI history record exceeds its byte bound")
    return json.loads(canonical_bytes(record))


def build_history_oci_plan(
    record: Mapping[str, Any], *, repository: str
) -> OciRecordPlanV1:
    try:
        validate_abi_history_record(record)
    except TapRecordError as error:
        raise AbiHistoryError(f"ABI history record is invalid: {error}") from error
    checked_repository = _repository(repository, "history record repository").lower()
    body = canonical_bytes(record)
    plan = _mapping(record["plan"], "history plan")
    return OciRecordPlanV1(
        repository=checked_repository,
        artifact_type=HISTORY_RECORD_MEDIA_TYPE,
        config=OciBlobV1(
            role="abi-history-record",
            media_type=HISTORY_RECORD_MEDIA_TYPE,
            body=body,
            title="abi-history-record.json",
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=HISTORY_RECORD_MEDIA_TYPE,
                body=body,
                title="abi-history-record.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.classification": "protected-abi-history",
            "dev.kandelo.abi-staging.kind": "abi-history",
            "dev.kandelo.abi-staging.source-abi": str(plan["source_abi"]),
            "dev.kandelo.abi-staging.successor-abi": str(plan["successor_abi"]),
            "org.opencontainers.image.source": "https://github.com/"
            + str(_mapping(record["run"], "history run")["repository"]),
        },
    )


def history_record_repository(tap_repository: str, source_abi: int) -> str:
    repository = _repository(tap_repository, "history tap repository").lower()
    abi = _integer(source_abi, "history record ABI")
    owner, name = repository.split("/", 1)
    return f"{owner}/{name}-abi-{abi}-records/history"
