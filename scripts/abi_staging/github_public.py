"""Bounded anonymous GitHub discovery for public ABI staging requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from .request import (
    RequestIssuerPolicyV1,
    RequestValidationError,
    parse_request_asset_name,
    validate_request,
)


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
PR_TAG = re.compile(
    r"^abi-staging-pr-([1-9][0-9]*)(?:-sha256-([0-9a-f]{64}))?$"
)


class PublicGitHubError(ValueError):
    """Raised when public GitHub metadata crosses a protected boundary."""


class _PublicGitHubNotFound(PublicGitHubError):
    """Raised only for an exact public GitHub HTTP 404 response."""


class Response(Protocol):
    status: int
    headers: Any

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class DiscoveredRequestV1:
    request_digest: str
    asset_name: str
    asset_url: str
    release_tag: str
    request: MappingProxyType[str, Any]
    created_at: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _default_opener() -> Callable[[urllib.request.Request], Response]:
    opener = urllib.request.build_opener(_NoRedirect())

    def open_request(request: urllib.request.Request) -> Response:
        try:
            return opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            return error

    return open_request


def _header(headers: Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value
    return None


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise PublicGitHubError(f"GitHub JSON contains duplicate field {key!r}")
        value[key] = child
    return value


def _reject_json_number(value: str) -> None:
    raise PublicGitHubError(f"GitHub JSON contains unsupported number {value}")


def _parse_json(body: bytes, field: str) -> Any:
    try:
        return json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_pairs,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, PublicGitHubError) as error:
        if isinstance(error, PublicGitHubError):
            raise
        raise PublicGitHubError(f"{field} is invalid JSON: {error}") from error


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2**64 - 1:
        raise PublicGitHubError(f"{field} must be a positive integer")
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PublicGitHubError(f"{field} must be a string")
    try:
        length = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise PublicGitHubError(f"{field} is not UTF-8") from error
    if length < 1 or length > maximum or "\0" in value:
        raise PublicGitHubError(f"{field} exceeds its string bound")
    return value


def _asset_created_at(value: Any) -> str:
    timestamp = _bounded_text(value, "Release asset creation time", 32)
    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise PublicGitHubError(
            "Release asset creation time is not UTC RFC 3339"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != timestamp:
        raise PublicGitHubError(
            "Release asset creation time is not canonical UTC RFC 3339"
        )
    return timestamp


class GitHubPublicClient:
    def __init__(
        self,
        policy: RequestIssuerPolicyV1,
        *,
        opener: Callable[[urllib.request.Request], Response] | None = None,
        page_size: int = 100,
    ) -> None:
        if page_size < 1 or page_size > 100:
            raise PublicGitHubError("GitHub page size must be between 1 and 100")
        self.policy = policy
        self._opener = opener or _default_opener()
        self.page_size = page_size

    def _validate_transport_url(self, url: str) -> urllib.parse.SplitResult:
        if not isinstance(url, str) or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url):
            raise PublicGitHubError("public URL contains whitespace or a control character")
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise PublicGitHubError(f"public URL is invalid: {error}") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.policy.allowed_release_hosts
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment
        ):
            raise PublicGitHubError("public URL escaped the HTTPS GitHub host boundary")
        return parsed

    def _read_response(self, response: Response, maximum: int, field: str) -> bytes:
        content_length = _header(response.headers, "Content-Length")
        declared: int | None = None
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                raise PublicGitHubError(f"{field} has an invalid Content-Length")
            declared = int(content_length, 10)
            if declared > maximum:
                raise PublicGitHubError(f"{field} exceeds its response-byte limit")
        body = response.read(maximum + 1)
        if len(body) > maximum:
            raise PublicGitHubError(f"{field} exceeds its response-byte limit")
        if declared is not None and declared != len(body):
            raise PublicGitHubError(f"{field} Content-Length does not match received bytes")
        return body

    def _get(self, url: str, maximum: int, accept: str) -> bytes:
        current = url
        for redirect_count in range(self.policy.max_redirects + 1):
            self._validate_transport_url(current)
            request = urllib.request.Request(
                current,
                headers={"Accept": accept, "User-Agent": "kandelo-abi-staging/1"},
                method="GET",
            )
            try:
                response = self._opener(request)
            except (OSError, urllib.error.URLError) as error:
                raise PublicGitHubError(f"public GitHub request failed: {error}") from error
            try:
                status = int(response.status)
                if status in REDIRECT_STATUSES:
                    if redirect_count == self.policy.max_redirects:
                        raise PublicGitHubError("public GitHub redirect limit exceeded")
                    location = _header(response.headers, "Location")
                    if location is None:
                        raise PublicGitHubError("public GitHub redirect omitted Location")
                    self._validate_transport_url(location)
                    current = location
                    continue
                if status == 404:
                    raise _PublicGitHubNotFound(
                        "public GitHub request returned HTTP 404"
                    )
                if status != 200:
                    raise PublicGitHubError(f"public GitHub request returned HTTP {status}")
                return self._read_response(response, maximum, "public GitHub response")
            finally:
                response.close()
        raise PublicGitHubError("public GitHub redirect limit exceeded")

    def _api_json(self, url: str) -> Any:
        return _parse_json(
            self._get(url, self.policy.max_api_response_bytes, "application/vnd.github+json"),
            "GitHub API response",
        )

    def _pages(self, endpoint: str) -> tuple[Mapping[str, Any], ...]:
        result: list[Mapping[str, Any]] = []
        page_digests: set[str] = set()
        for page in range(1, self.policy.max_release_pages + 1):
            separator = "&" if "?" in endpoint else "?"
            url = f"{endpoint}{separator}per_page={self.page_size}&page={page}"
            body = self._get(url, self.policy.max_api_response_bytes, "application/vnd.github+json")
            value = _parse_json(body, "GitHub API page")
            if not isinstance(value, list) or len(value) > self.page_size:
                raise PublicGitHubError("GitHub API page is not a bounded array")
            digest = hashlib.sha256(body).hexdigest()
            if value and digest in page_digests:
                raise PublicGitHubError("GitHub API repeated an identical nonempty page")
            page_digests.add(digest)
            for item in value:
                if not isinstance(item, Mapping):
                    raise PublicGitHubError("GitHub API page contains a non-object item")
                result.append(item)
            if len(value) < self.page_size:
                return tuple(result)
        raise PublicGitHubError("GitHub API pagination exceeded its page bound")

    def _parse_public_request_url(
        self, url: str
    ) -> tuple[str, str, int, str | None]:
        parsed = self._validate_transport_url(url)
        if parsed.hostname != "github.com" or parsed.query:
            raise PublicGitHubError("request asset URL is not an exact public GitHub Release URL")
        prefix = f"/{self.policy.issuer_repository}/releases/download/"
        if not parsed.path.startswith(prefix):
            raise PublicGitHubError("request asset URL names an unauthorized repository")
        remainder = parsed.path[len(prefix) :]
        parts = remainder.split("/")
        if len(parts) != 2:
            raise PublicGitHubError("request asset URL has an invalid path")
        tag, asset_name = parts
        tag_match = PR_TAG.fullmatch(tag)
        if tag_match is None or not tag.startswith(self.policy.request_release_tag_prefix):
            raise PublicGitHubError("request asset URL has an invalid release tag")
        parse_request_asset_name(asset_name)
        return tag, asset_name, int(tag_match.group(1), 10), tag_match.group(2)

    def _discover_request_url(
        self, url: str, *, created_at: str | None = None
    ) -> DiscoveredRequestV1:
        try:
            tag, asset_name, pull_request_number, tag_digest = self._parse_public_request_url(url)
            body = self._get(url, self.policy.max_request_bytes, "application/octet-stream")
            request = validate_request(body, asset_name, self.policy)
        except RequestValidationError as error:
            raise PublicGitHubError(f"public request is invalid: {error}") from error
        if request["pull_request"]["number"] != pull_request_number:
            raise PublicGitHubError("request body and Release tag name different pull requests")
        digest = hashlib.sha256(body).hexdigest()
        if tag_digest is not None and digest != tag_digest:
            raise PublicGitHubError("request body and Release tag name different digests")
        if created_at is not None:
            created_at = _asset_created_at(created_at)
        return DiscoveredRequestV1(
            digest, asset_name, url, tag, request, created_at
        )

    def discover_url(
        self, url: str, *, created_at: str | None = None
    ) -> DiscoveredRequestV1:
        try:
            tag, asset_name, _, tag_digest = self._parse_public_request_url(url)
        except RequestValidationError as error:
            raise PublicGitHubError(f"public request is invalid: {error}") from error
        if tag_digest is not None:
            release = self._release_by_tag(tag)
            matching_assets = []
            for asset in release["assets"]:
                name = _bounded_text(asset.get("name"), "Release asset name", 512)
                asset_url = _bounded_text(
                    asset.get("browser_download_url"),
                    "Release asset URL",
                    self.policy.max_string_bytes,
                )
                asset_created_at = _asset_created_at(asset.get("created_at"))
                _positive_integer(asset.get("id"), "Release asset id")
                if name == asset_name and asset_url == url:
                    matching_assets.append(asset_created_at)
            if len(matching_assets) != 1:
                raise PublicGitHubError(
                    "content-addressed Release metadata and request URL disagree"
                )
            authoritative_created_at = matching_assets[0]
            if (
                created_at is not None
                and _asset_created_at(created_at) != authoritative_created_at
            ):
                raise PublicGitHubError(
                    "Release asset creation time differs from public metadata"
                )
            created_at = authoritative_created_at
        discovered = self._discover_request_url(url, created_at=created_at)
        if tag_digest is not None:
            self._validate_content_addressed_tag_authority(
                tag, release, discovered.request
            )
        return discovered

    def _request_release_tags(self) -> tuple[str, ...]:
        repository = self.policy.issuer_repository
        prefix = self.policy.request_release_tag_prefix
        endpoint = (
            f"https://api.github.com/repos/{repository}/"
            f"git/matching-refs/tags/{urllib.parse.quote(prefix, safe='')}"
        )
        refs = self._pages(endpoint)
        if len(refs) > self.policy.max_release_assets:
            raise PublicGitHubError("ABI staging request tag inventory is too large")
        tags: set[str] = set()
        for value in refs:
            ref = _bounded_text(value.get("ref"), "Git reference", 512)
            expected_prefix = f"refs/tags/{prefix}"
            if not ref.startswith(expected_prefix):
                raise PublicGitHubError("matching-ref response escaped its prefix")
            tag = ref.removeprefix("refs/tags/")
            if PR_TAG.fullmatch(tag) is None or tag in tags:
                raise PublicGitHubError("ABI staging request tag inventory is invalid")
            tags.add(tag)
        return tuple(
            sorted(
                tags,
                key=lambda tag: (
                    int(PR_TAG.fullmatch(tag).group(1), 10),
                    PR_TAG.fullmatch(tag).group(2) or "",
                ),
            )
        )

    def _release_by_tag(self, tag: str) -> Mapping[str, Any]:
        repository = self.policy.issuer_repository
        endpoint = (
            f"https://api.github.com/repos/{repository}/releases/tags/"
            f"{urllib.parse.quote(tag, safe='')}"
        )
        value = self._api_json(endpoint)
        if not isinstance(value, Mapping):
            raise PublicGitHubError("GitHub Release response is not an object")
        _positive_integer(value.get("id"), "Release id")
        release_tag = _bounded_text(value.get("tag_name"), "Release tag", 256)
        if release_tag != tag:
            raise PublicGitHubError("GitHub Release response changed its tag")
        match = PR_TAG.fullmatch(tag)
        assert match is not None
        content_addressed = match.group(2) is not None
        if value.get("prerelease") is not True or value.get("draft") is not False:
            raise PublicGitHubError("ABI staging Release is not a public prerelease")
        if content_addressed:
            target_commitish = _bounded_text(
                value.get("target_commitish"), "Release target commit", 40
            )
            if (
                value.get("immutable") is not True
                or re.fullmatch(r"[0-9a-f]{40}", target_commitish) is None
            ):
                raise PublicGitHubError(
                    "content-addressed ABI staging Release is not immutable and commit-bound"
                )
        assets = value.get("assets")
        if not isinstance(assets, list):
            raise PublicGitHubError("ABI staging Release assets are not an array")
        if content_addressed and len(assets) != 1:
            raise PublicGitHubError(
                "content-addressed ABI staging Release must contain one asset"
            )
        if len(assets) > self.policy.max_release_assets:
            raise PublicGitHubError("ABI staging Release has too many assets")
        if any(not isinstance(asset, Mapping) for asset in assets):
            raise PublicGitHubError("ABI staging Release contains a non-object asset")
        return value

    def _direct_tag_target(self, tag: str) -> str:
        repository = self.policy.issuer_repository
        endpoint = (
            f"https://api.github.com/repos/{repository}/git/ref/tags/"
            f"{urllib.parse.quote(tag, safe='')}"
        )
        value = self._api_json(endpoint)
        if not isinstance(value, Mapping):
            raise PublicGitHubError("Git tag response is not an object")
        ref = _bounded_text(value.get("ref"), "Git tag ref", 512)
        target = value.get("object")
        if ref != f"refs/tags/{tag}" or not isinstance(target, Mapping):
            raise PublicGitHubError("request tag response changed its identity")
        target_type = _bounded_text(target.get("type"), "Git tag target type", 32)
        target_sha = _bounded_text(target.get("sha"), "Git tag target SHA", 40)
        if target_type != "commit" or re.fullmatch(r"[0-9a-f]{40}", target_sha) is None:
            raise PublicGitHubError("request tag is not a direct commit reference")
        return target_sha

    def _validate_content_addressed_tag_authority(
        self, tag: str, release: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
        workflow_ref = request["issuance"]["issuer_workflow_ref"]
        expected_prefix = (
            f"{self.policy.issuer_repository}/{self.policy.issuer_workflow_path}@"
        )
        if not workflow_ref.startswith(expected_prefix):
            raise PublicGitHubError("request issuer workflow is outside protected authority")
        protected_commit = workflow_ref.removeprefix(expected_prefix)
        if (
            release.get("target_commitish") != protected_commit
            or self._direct_tag_target(tag) != protected_commit
        ):
            raise PublicGitHubError(
                "content-addressed request Release differs from its protected issuer commit"
            )

    def scan(self) -> tuple[DiscoveredRequestV1, ...]:
        tags = self._request_release_tags()
        seen_release_ids: set[int] = set()
        discovered: list[DiscoveredRequestV1] = []
        seen_assets: set[tuple[int, str, str]] = set()
        for tag in tags:
            match = PR_TAG.fullmatch(tag)
            assert match is not None
            try:
                release = self._release_by_tag(tag)
            except _PublicGitHubNotFound:
                # WHY: the protected publisher reserves the direct digest tag
                # before its draft Release becomes public. Anonymous scans must
                # ignore only that in-flight state; legacy or other failures
                # remain fatal, and a public content-addressed Release is still
                # fully validated below.
                if match.group(2) is not None:
                    continue
                raise
            release_id = _positive_integer(release.get("id"), "Release id")
            if release_id in seen_release_ids:
                raise PublicGitHubError("GitHub API returned a duplicate Release identity")
            seen_release_ids.add(release_id)
            assets = release["assets"]
            checked_assets: list[tuple[str, str, str]] = []
            for asset in assets:
                asset_id = _positive_integer(asset.get("id"), "Release asset id")
                name = _bounded_text(asset.get("name"), "Release asset name", 512)
                url = _bounded_text(
                    asset.get("browser_download_url"),
                    "Release asset URL",
                    self.policy.max_string_bytes,
                )
                created_at = _asset_created_at(asset.get("created_at"))
                identity = (asset_id, name, url)
                if identity in seen_assets:
                    raise PublicGitHubError("GitHub API returned a duplicate asset")
                seen_assets.add(identity)
                parse_request_asset_name(name)
                checked_assets.append((name, url, created_at))
            for name, url, created_at in checked_assets:
                candidate = self._discover_request_url(url, created_at=created_at)
                if candidate.release_tag != tag or candidate.asset_name != name:
                    raise PublicGitHubError("Release metadata and request URL disagree")
                if match.group(2) is not None:
                    self._validate_content_addressed_tag_authority(
                        tag, release, candidate.request
                    )
                discovered.append(candidate)
        ordered = sorted(
            discovered,
            key=lambda item: (item.request_digest, item.asset_name, item.asset_url),
        )
        identities = [(item.request_digest, item.asset_name, item.asset_url) for item in ordered]
        if len(set(identities)) != len(identities):
            raise PublicGitHubError("public discovery returned a duplicate request")
        return tuple(ordered)

    def pull_request_lifecycle(self, number: int) -> Any:
        _positive_integer(number, "pull-request number")
        repository = self.policy.issuer_repository
        value = self._api_json(f"https://api.github.com/repos/{repository}/pulls/{number}")
        if not isinstance(value, Mapping):
            raise PublicGitHubError("pull-request API response is not an object")
        state = value.get("state")
        head = value.get("head")
        if state not in {"open", "closed"} or not isinstance(head, Mapping):
            raise PublicGitHubError("pull-request API response has invalid lifecycle fields")
        head_sha = _bounded_text(head.get("sha"), "pull-request head", 40)
        if re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            raise PublicGitHubError("pull-request head is not a full lowercase SHA")
        merged_at = value.get("merged_at")
        merge_commit = value.get("merge_commit_sha")
        from .reconcile import PullRequestLifecycleV1

        if state == "open":
            if merged_at is not None:
                raise PublicGitHubError("open pull request cannot be merged")
            return PullRequestLifecycleV1("open", head_sha, None)
        if merged_at is None:
            return PullRequestLifecycleV1("closed", head_sha, None)
        if not isinstance(merged_at, str) or not merged_at:
            raise PublicGitHubError("merged pull request has invalid merged_at")
        if not isinstance(merge_commit, str) or re.fullmatch(r"[0-9a-f]{40}", merge_commit) is None:
            raise PublicGitHubError("merged pull request has invalid merge commit")
        return PullRequestLifecycleV1("merged", head_sha, merge_commit)
