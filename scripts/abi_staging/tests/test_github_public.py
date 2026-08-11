from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
import unittest

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.github_public import GitHubPublicClient, PublicGitHubError
from scripts.abi_staging.request import load_request_issuer_policy


FIXTURES = TAP_ROOT / "Kandelo/staging/fixtures/request"
ASSET_CREATED_AT = "2026-08-09T10:00:00Z"
POLICY = load_request_issuer_policy(
    TAP_ROOT / "Kandelo/staging/request-issuers.toml",
    expected_tap="kandelo-dev/homebrew-tap-core",
)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def close(self) -> None:
        pass


class FakeOpener:
    def __init__(self):
        self.routes: dict[str, list[FakeResponse]] = defaultdict(list)
        self.calls: list[str] = []

    def add(self, url: str, response: FakeResponse) -> None:
        self.routes[url].append(response)

    def __call__(self, request: Any) -> FakeResponse:
        url = request.full_url
        self.calls.append(url)
        if not self.routes[url]:
            raise AssertionError(f"unexpected public request: {url}")
        return self.routes[url].pop(0)


def json_response(value: object) -> FakeResponse:
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return FakeResponse(200, body, {"Content-Length": str(len(body))})


def fixture_asset(name: str = "current-request.json") -> tuple[bytes, str, str]:
    body = (FIXTURES / name).read_bytes()
    value = json.loads(body)
    head = value["build_source"]["commit"]
    digest = hashlib.sha256(body).hexdigest()
    asset_name = f"candidate-request-{head}-sha256-{digest}.json"
    url = f"https://github.com/Automattic/kandelo/releases/download/abi-staging-pr-19/{asset_name}"
    return body, asset_name, url


def add_asset_download(opener: FakeOpener, public_url: str, body: bytes) -> None:
    final_url = "https://release-assets.githubusercontent.com/objects/exact-request"
    opener.add(public_url, FakeResponse(302, headers={"Location": final_url}))
    opener.add(final_url, FakeResponse(200, body, {"Content-Length": str(len(body))}))


class GitHubPublicClientTests(unittest.TestCase):
    def test_scan_paginates_request_tags_and_matches_manual_discovery(self) -> None:
        opener = FakeOpener()
        client = GitHubPublicClient(POLICY, opener=opener, page_size=1)
        body, asset_name, public_url = fixture_asset()
        refs = (
            "https://api.github.com/repos/Automattic/kandelo/"
            "git/matching-refs/tags/abi-staging-pr-"
        )
        opener.add(
            f"{refs}?per_page=1&page=1",
            json_response([{
                "ref": "refs/tags/abi-staging-pr-19",
                "object": {"type": "commit", "sha": "1" * 40},
            }]),
        )
        opener.add(f"{refs}?per_page=1&page=2", json_response([]))
        release = (
            "https://api.github.com/repos/Automattic/kandelo/"
            "releases/tags/abi-staging-pr-19"
        )
        opener.add(
            release,
            json_response({
                "id": 9,
                "tag_name": "abi-staging-pr-19",
                "prerelease": True,
                "draft": False,
                "assets": [{
                    "id": 11,
                    "name": asset_name,
                    "browser_download_url": public_url,
                    "created_at": ASSET_CREATED_AT,
                }],
            }),
        )
        add_asset_download(opener, public_url, body)
        add_asset_download(opener, public_url, body)

        scanned = client.scan()
        manual = client.discover_url(public_url, created_at=ASSET_CREATED_AT)
        self.assertEqual(scanned, (manual,))
        self.assertEqual(manual.request_digest, hashlib.sha256(body).hexdigest())
        self.assertEqual(manual.release_tag, "abi-staging-pr-19")
        self.assertEqual(manual.created_at, ASSET_CREATED_AT)
        broad = "https://api.github.com/repos/Automattic/kandelo/releases"
        self.assertFalse(
            any(url == broad or url.startswith(f"{broad}?") for url in opener.calls)
        )

    def test_duplicate_request_tag_refs_fail_closed(self) -> None:
        opener = FakeOpener()
        client = GitHubPublicClient(POLICY, opener=opener)
        refs = (
            "https://api.github.com/repos/Automattic/kandelo/"
            "git/matching-refs/tags/abi-staging-pr-"
        )
        duplicate = {
            "ref": "refs/tags/abi-staging-pr-19",
            "object": {"type": "commit", "sha": "1" * 40},
        }
        opener.add(
            f"{refs}?per_page=100&page=1",
            json_response([duplicate, duplicate]),
        )
        with self.assertRaises(PublicGitHubError):
            client.scan()

    def test_asset_order_is_irrelevant_and_duplicate_discovery_is_rejected(self) -> None:
        fixtures = [fixture_asset(name) for name in [
            "current-request.json",
            "same-head-reissued-request.json",
            "historical-request.json",
        ]]

        def scan(order: list[int], *, duplicate: bool = False):
            opener = FakeOpener()
            client = GitHubPublicClient(POLICY, opener=opener)
            refs = (
                "https://api.github.com/repos/Automattic/kandelo/"
                "git/matching-refs/tags/abi-staging-pr-"
            )
            opener.add(
                f"{refs}?per_page=100&page=1",
                json_response([{
                    "ref": "refs/tags/abi-staging-pr-19",
                    "object": {"type": "commit", "sha": "1" * 40},
                }]),
            )
            assets = []
            for asset_id, index in enumerate(order, start=11):
                body, name, url = fixtures[index]
                assets.append({
                    "id": asset_id,
                    "name": name,
                    "browser_download_url": url,
                    "created_at": ASSET_CREATED_AT,
                })
                add_asset_download(opener, url, body)
            if duplicate:
                body, name, url = fixtures[order[0]]
                assets.append({
                    "id": 99,
                    "name": name,
                    "browser_download_url": url,
                    "created_at": ASSET_CREATED_AT,
                })
                add_asset_download(opener, url, body)
            endpoint = (
                "https://api.github.com/repos/Automattic/kandelo/"
                "releases/tags/abi-staging-pr-19"
            )
            opener.add(
                endpoint,
                json_response({
                    "id": 9,
                    "tag_name": "abi-staging-pr-19",
                    "prerelease": True,
                    "draft": False,
                    "assets": assets,
                }),
            )
            return client.scan()

        self.assertEqual(scan([0, 1, 2]), scan([2, 0, 1]))
        with self.assertRaises(PublicGitHubError):
            scan([0, 1], duplicate=True)

    def test_scan_rejects_hostile_tag_and_release_inventories(self) -> None:
        refs = (
            "https://api.github.com/repos/Automattic/kandelo/"
            "git/matching-refs/tags/abi-staging-pr-"
        )
        release = (
            "https://api.github.com/repos/Automattic/kandelo/"
            "releases/tags/abi-staging-pr-19"
        )
        body, asset_name, public_url = fixture_asset()
        asset = {
            "id": 11,
            "name": asset_name,
            "browser_download_url": public_url,
            "created_at": ASSET_CREATED_AT,
        }

        for invalid_ref in (
            "refs/tags/unrelated",
            "refs/tags/abi-staging-pr-latest",
        ):
            with self.subTest(invalid_ref=invalid_ref):
                opener = FakeOpener()
                opener.add(
                    f"{refs}?per_page=100&page=1",
                    json_response([{
                        "ref": invalid_ref,
                        "object": {"type": "commit", "sha": "1" * 40},
                    }]),
                )
                with self.assertRaises(PublicGitHubError):
                    GitHubPublicClient(POLICY, opener=opener).scan()

        limited_policy = replace(POLICY, max_release_assets=1)
        opener = FakeOpener()
        opener.add(
            f"{refs}?per_page=100&page=1",
            json_response([
                {"ref": "refs/tags/abi-staging-pr-19"},
                {"ref": "refs/tags/abi-staging-pr-20"},
            ]),
        )
        with self.assertRaises(PublicGitHubError):
            GitHubPublicClient(limited_policy, opener=opener).scan()

        release_mutations = (
            {"tag_name": "abi-staging-pr-20"},
            {"prerelease": False},
            {"draft": True},
            {"assets": [asset, asset]},
        )
        baseline = {
            "id": 9,
            "tag_name": "abi-staging-pr-19",
            "prerelease": True,
            "draft": False,
            "assets": [asset],
        }
        for mutation in release_mutations:
            with self.subTest(mutation=mutation):
                opener = FakeOpener()
                opener.add(
                    f"{refs}?per_page=100&page=1",
                    json_response([{"ref": "refs/tags/abi-staging-pr-19"}]),
                )
                opener.add(release, json_response({**baseline, **mutation}))
                with self.assertRaises(PublicGitHubError):
                    GitHubPublicClient(POLICY, opener=opener).scan()

        opener = FakeOpener()
        opener.add(
            f"{refs}?per_page=100&page=1",
            FakeResponse(200, b"x" * (POLICY.max_api_response_bytes + 1)),
        )
        with self.assertRaises(PublicGitHubError):
            GitHubPublicClient(POLICY, opener=opener).scan()

    def test_url_authority_and_redirect_boundaries_are_exact(self) -> None:
        body, asset_name, public_url = fixture_asset()
        invalid_urls = [
            public_url.replace("Automattic/kandelo", "other/project"),
            public_url.replace("abi-staging-pr-19", "abi-staging-pr-latest"),
            public_url.replace(asset_name, "current.json"),
            public_url + "?download=1",
            public_url + "#fragment",
            public_url + "\n",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(PublicGitHubError):
                    GitHubPublicClient(POLICY, opener=FakeOpener()).discover_url(url)

        redirects = [
            "http://release-assets.githubusercontent.com/item",
            "https://user@release-assets.githubusercontent.com/item",
            "https://release-assets.githubusercontent.com/item#fragment",
            "https://example.com/item",
        ]
        for location in redirects:
            with self.subTest(location=location):
                opener = FakeOpener()
                opener.add(public_url, FakeResponse(302, headers={"Location": location}))
                with self.assertRaises(PublicGitHubError):
                    GitHubPublicClient(POLICY, opener=opener).discover_url(public_url)

        opener = FakeOpener()
        current = public_url
        for index in range(6):
            next_url = f"https://release-assets.githubusercontent.com/hop/{index}"
            opener.add(current, FakeResponse(302, headers={"Location": next_url}))
            current = next_url
        with self.assertRaises(PublicGitHubError):
            GitHubPublicClient(POLICY, opener=opener).discover_url(public_url)

    def test_body_length_response_limit_and_digest_drift_are_rejected(self) -> None:
        body, _, public_url = fixture_asset()
        cases = [
            FakeResponse(200, body, {"Content-Length": str(len(body) + 1)}),
            FakeResponse(200, b"x" * (POLICY.max_request_bytes + 1)),
            FakeResponse(200, body[:-1], {"Content-Length": str(len(body) - 1)}),
        ]
        for response in cases:
            with self.subTest(size=len(response._body)):
                opener = FakeOpener()
                opener.add(public_url, response)
                with self.assertRaises(PublicGitHubError):
                    GitHubPublicClient(POLICY, opener=opener).discover_url(public_url)


if __name__ == "__main__":
    unittest.main()
