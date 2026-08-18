from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, unquote, urlsplit
import unittest
from unittest.mock import patch

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.oci import (
    CANONICAL_TAG_PREFIX,
    HttpResponseV1,
    OciPublicationError,
    PublishedRecordLocatorV1,
    UrllibOciTransportV1,
    build_oci_manifest,
    fetch_public_blob,
    isolated_oras_transport,
    list_public_record_locators,
    publish_record,
)
from scripts.abi_staging.records import (
    BOTTLE_LAYER_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
    OciBlobV1,
    OciRecordPlanV1,
)


REPOSITORY = "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool"
SOURCE_ASSOCIATION = "kandelo-dev/homebrew-tap-core"
REUSE_TAG_PREFIX = "reuse-sha256-"


class FakeRegistryTransport:
    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, bool]] = []
        self.uploads: dict[str, str] = {}
        self.blob_upload_content_types: list[str | None] = []
        self.association = SOURCE_ASSOCIATION
        self.visibility = "public"
        self.private_anonymous = False
        self.drift_anonymous = False
        self.wrong_digest = False
        self.wrong_size = False
        self.next_status: int | None = None
        self.redirect_url: str | None = None
        self.hostile_upload_location = False
        self.github_upload_location = False
        self.upload_location_query = ""
        self.package_absent_until_first_manifest = False
        self.blobs_hidden_until_first_manifest = False
        self.registry_next_status: int | None = None
        self.empty_write_response = False

    def _response(
        self,
        status: int,
        url: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> HttpResponseV1:
        return HttpResponseV1(status, headers or {}, body, url)

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
        request_headers = {key.lower(): value for key, value in (headers or {}).items()}
        del maximum_bytes
        self.calls.append((method, url, authenticated))
        if self.redirect_url is not None:
            redirected = self.redirect_url
            self.redirect_url = None
            return self._response(307, redirected, headers={"location": redirected})
        if self.next_status is not None:
            status = self.next_status
            self.next_status = None
            return self._response(status, url)
        parsed = urlsplit(url)
        if parsed.netloc == "api.github.com":
            if self.package_absent_until_first_manifest and not any(
                candidate_repository == REPOSITORY
                for candidate_repository, _reference in self.manifests
            ):
                return self._response(404, url)
            payload = canonical_bytes(
                {
                    "repository": {"full_name": self.association},
                    "visibility": self.visibility,
                }
            )
            return self._response(200, url, body=payload)
        prefix = "/v2/"
        if parsed.netloc != "ghcr.io" or not parsed.path.startswith(prefix):
            return self._response(404, url)
        if self.registry_next_status is not None:
            status = self.registry_next_status
            self.registry_next_status = None
            return self._response(status, url)
        remainder = parsed.path[len(prefix) :]
        if remainder.endswith("/tags/list"):
            repository = remainder[: -len("/tags/list")]
            tags = sorted(
                reference
                for candidate_repository, reference in self.manifests
                if candidate_repository == repository
                and not reference.startswith("sha256:")
            )
            return self._response(
                200,
                url,
                body=canonical_bytes({"name": repository, "tags": tags}),
            )
        if "/manifests/" in remainder:
            repository, reference = remainder.split("/manifests/", 1)
            reference = unquote(reference)
            key = (repository, reference)
            if method in {"GET", "HEAD"}:
                if not authenticated and self.private_anonymous:
                    return self._response(401, url)
                if key not in self.manifests:
                    return self._response(404, url)
                stored = self.manifests[key]
                returned = (
                    stored + b"drift"
                    if not authenticated and self.drift_anonymous
                    else stored
                )
                digest = hashlib.sha256(stored).hexdigest()
                return self._response(
                    200,
                    url,
                    body=b"" if method == "HEAD" else returned,
                    headers={
                        "content-length": str(len(stored)),
                        "content-type": "application/vnd.oci.image.manifest.v1+json",
                        "docker-content-digest": "sha256:" + digest,
                    },
                )
            if method == "PUT":
                assert body is not None
                digest = hashlib.sha256(body).hexdigest()
                self.manifests[(repository, reference)] = body
                self.manifests[(repository, "sha256:" + digest)] = body
                return self._response(
                    201,
                    url,
                    headers={"docker-content-digest": "sha256:" + digest},
                )
        if remainder.endswith("/blobs/uploads/"):
            repository = remainder[: -len("/blobs/uploads/")]
            query = parse_qs(parsed.query)
            mount = query.get("mount", [None])[0]
            source = query.get("from", [None])[0]
            if mount is not None and source is not None and (source, mount) in self.blobs:
                self.blobs[(repository, mount)] = self.blobs[(source, mount)]
                return self._response(
                    201,
                    url,
                    headers={
                        "docker-content-digest": mount,
                        **(
                            {"content-length": "0"}
                            if self.empty_write_response
                            else {}
                        ),
                    },
                )
            session = f"session-{len(self.uploads) + 1}"
            self.uploads[session] = repository
            location = (
                "https://registry-attacker.example/v2/steal"
                if self.hostile_upload_location
                else (
                    f"https://ghcr.io/v2/{repository}/blobs/upload/{session}"
                    if self.github_upload_location
                    else f"https://ghcr.io/v2/{repository}/blobs/uploads/{session}"
                )
            )
            return self._response(
                202,
                url,
                headers={"location": location + self.upload_location_query},
            )
        upload_component = next(
            (
                component
                for component in ("/blobs/uploads/", "/blobs/upload/")
                if component in remainder
            ),
            None,
        )
        if upload_component is not None:
            repository, session = remainder.split(upload_component, 1)
            if method != "PUT" or session not in self.uploads:
                return self._response(404, url)
            self.blob_upload_content_types.append(request_headers.get("content-type"))
            digest = parse_qs(parsed.query).get("digest", [""])[0]
            assert body is not None
            self.blobs[(repository, digest)] = body
            return self._response(
                201,
                url,
                headers={
                    "docker-content-digest": digest,
                    **(
                        {"content-length": "0"}
                        if self.empty_write_response
                        else {}
                    ),
                },
            )
        if "/blobs/" in remainder:
            repository, digest = remainder.split("/blobs/", 1)
            key = (repository, unquote(digest))
            if method in {"GET", "HEAD"}:
                if (
                    self.blobs_hidden_until_first_manifest
                    and not any(
                        candidate_repository == repository
                        for candidate_repository, _reference in self.manifests
                    )
                ):
                    return self._response(404, url)
                if not authenticated and self.private_anonymous:
                    return self._response(401, url)
                if key not in self.blobs:
                    return self._response(404, url)
                stored = self.blobs[key]
                returned = (
                    stored + b"drift"
                    if not authenticated and self.drift_anonymous
                    else stored
                )
                observed_digest = key[1]
                if self.wrong_digest:
                    observed_digest = "sha256:" + "f" * 64
                observed_size = len(stored) + (1 if self.wrong_size else 0)
                return self._response(
                    200,
                    url,
                    body=b"" if method == "HEAD" else returned,
                    headers={
                        "content-length": str(observed_size),
                        "docker-content-digest": observed_digest,
                    },
                )
        return self._response(404, url)


class PublicBlobFetchTests(unittest.TestCase):
    def test_fetches_only_the_exact_anonymous_digest_and_size(self) -> None:
        transport = FakeRegistryTransport()
        body = b"exact public lazy input\n"
        digest = hashlib.sha256(body).hexdigest()
        transport.blobs[(REPOSITORY, "sha256:" + digest)] = body

        self.assertEqual(
            fetch_public_blob(
                f"ghcr.io/{REPOSITORY}@sha256:{digest}",
                expected_sha256=digest,
                expected_bytes=len(body),
                transport=transport,
            ),
            body,
        )
        self.assertEqual(transport.calls[-1][2], False)

        for reference, expected_sha256, expected_bytes in (
            (f"https://ghcr.io/{REPOSITORY}@sha256:{digest}", digest, len(body)),
            (f"ghcr.io/{REPOSITORY}@sha256:{'f' * 64}", digest, len(body)),
            (f"ghcr.io/{REPOSITORY}@sha256:{digest}", digest, len(body) + 1),
        ):
            with self.subTest(reference=reference, size=expected_bytes), self.assertRaises(
                OciPublicationError
            ):
                fetch_public_blob(
                    reference,
                    expected_sha256=expected_sha256,
                    expected_bytes=expected_bytes,
                    transport=transport,
                )

    def test_ghcr_blob_storage_redirect_is_exact_and_drops_authority(self) -> None:
        transport = UrllibOciTransportV1(username="", token="")
        payload = b"exact redirected public blob\n"
        digest = hashlib.sha256(payload).hexdigest()
        original = f"https://ghcr.io/v2/{REPOSITORY}/blobs/sha256:{digest}"
        storage = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs09/blobs/"
            f"sha256:{digest}?sv=2025-01-05&sig=fixture"
        )
        calls: list[tuple[str, str, dict[str, str]]] = []

        def perform(method, url, *, headers, body, maximum_bytes):
            del body, maximum_bytes
            calls.append((method, url, dict(headers)))
            if url == original and "authorization" not in headers:
                return HttpResponseV1(
                    401,
                    {
                        "www-authenticate": (
                            'Bearer realm="https://ghcr.io/token",'
                            'service="ghcr.io",'
                            f'scope="repository:{REPOSITORY}:pull"'
                        )
                    },
                    b"",
                    original,
                )
            if url.startswith("https://ghcr.io/token?"):
                return HttpResponseV1(200, {}, b'{"token":"fixture-bearer"}', url)
            if url == original:
                self.assertEqual(headers.get("authorization"), "Bearer fixture-bearer")
                return HttpResponseV1(307, {"location": storage}, b"", storage)
            if url == storage:
                self.assertNotIn("authorization", headers)
                return HttpResponseV1(
                    200,
                    {"content-length": str(len(payload))},
                    payload,
                    storage,
                )
            raise AssertionError(f"unexpected transport request: {method} {url}")

        with patch.object(transport, "_perform", side_effect=perform):
            self.assertEqual(
                fetch_public_blob(
                    f"ghcr.io/{REPOSITORY}@sha256:{digest}",
                    expected_sha256=digest,
                    expected_bytes=len(payload),
                    transport=transport,
                ),
                payload,
            )
        self.assertEqual(calls[-1][1], storage)


def _plan() -> OciRecordPlanV1:
    config = OciBlobV1(
        role="candidate-record",
        media_type=CANDIDATE_RECORD_MEDIA_TYPE,
        body=canonical_bytes(
            {
                "kind": "fixture-candidate",
                "nonendorsed": True,
                "schema": 1,
            }
        ),
        title="candidate-record.json",
    )
    mounted = b"mounted bottle layer\n"
    metadata = canonical_bytes({"formula": "mini-tool"})
    return OciRecordPlanV1(
        repository=REPOSITORY,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        config=config,
        layers=(
            OciBlobV1(
                role="bottle-layer",
                media_type=BOTTLE_LAYER_MEDIA_TYPE,
                body=mounted,
                title="bottle.tar.gz",
                mount_from="kandelo-dev/shared-bottles",
            ),
            OciBlobV1(
                role="bottle-metadata",
                media_type="application/vnd.kandelo.homebrew.bottle.metadata.v1+json",
                body=metadata,
                title="bottle-metadata.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
            "dev.kandelo.abi-staging.nonendorsed": "true",
        },
    )


class OciPublicationTests(unittest.TestCase):
    def test_authenticated_public_reads_use_credentials_only_for_ghcr(self) -> None:
        transport = UrllibOciTransportV1(
            username="protected-reader",
            token="read-token",
            authenticated_public_reads=True,
        )
        inventory_url = f"https://ghcr.io/v2/{REPOSITORY}/tags/list?n=100"
        token_url = (
            "https://ghcr.io/token?service=ghcr.io&scope="
            f"repository%3A{REPOSITORY.replace('/', '%2F')}%3Apull"
        )
        challenge = (
            'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
            f'scope="repository:{REPOSITORY}:pull"'
        )
        calls: list[tuple[str, str, dict[str, str]]] = []

        def perform(method, url, *, headers, body, maximum_bytes):
            del body, maximum_bytes
            calls.append((method, url, dict(headers)))
            if url == inventory_url and len(calls) == 1:
                self.assertTrue(headers["authorization"].startswith("Basic "))
                return HttpResponseV1(
                    401,
                    {"www-authenticate": challenge},
                    b"",
                    inventory_url,
                )
            if url == token_url:
                self.assertTrue(headers["authorization"].startswith("Basic "))
                return HttpResponseV1(200, {}, b'{"token":"fixture-bearer"}', token_url)
            if url == inventory_url:
                self.assertEqual(headers["authorization"], "Bearer fixture-bearer")
                return HttpResponseV1(
                    200,
                    {},
                    canonical_bytes({"name": REPOSITORY, "tags": []}),
                    inventory_url,
                )
            raise AssertionError(f"unexpected request {method} {url}")

        with patch.object(transport, "_perform", side_effect=perform):
            self.assertEqual(
                list_public_record_locators(REPOSITORY, transport=transport), ()
            )
        self.assertEqual([url for _method, url, _headers in calls], [
            inventory_url,
            token_url,
            inventory_url,
        ])

    def test_anonymous_transport_cannot_be_upgraded_to_authenticated_access(self) -> None:
        transport = UrllibOciTransportV1(username="", token="")
        with self.assertRaises(OciPublicationError):
            transport.request(
                "GET",
                "https://api.github.com/orgs/kandelo-dev/packages/container/example",
                authenticated=True,
                maximum_bytes=1024,
            )

    def test_public_record_tags_enumerate_only_immutable_digest_locators(self) -> None:
        transport = FakeRegistryTransport()
        published = publish_record(
            _plan(), transport=transport, expected_source_repository=SOURCE_ASSOCIATION
        )
        locators = list_public_record_locators(REPOSITORY, transport=transport)
        self.assertEqual(
            locators,
            (
                {
                    "repository": "ghcr.io/" + REPOSITORY,
                    "digest": published.digest,
                    "immutable_reference": published.immutable_reference,
                },
            ),
        )
        transport.manifests[(REPOSITORY, "latest")] = build_oci_manifest(_plan())
        with self.assertRaises(OciPublicationError):
            list_public_record_locators(REPOSITORY, transport=transport)

    def test_homebrew_version_aliases_are_inert_to_record_inventory(self) -> None:
        transport = FakeRegistryTransport()
        published = publish_record(
            _plan(), transport=transport, expected_source_repository=SOURCE_ASSOCIATION
        )
        transport.manifests[(REPOSITORY, "1.2.3_4-5")] = b"homebrew-index"
        transport.manifests[
            (REPOSITORY, "6.0.12-153-gcf5bc21_1")
        ] = b"homebrew-index"

        self.assertEqual(
            list_public_record_locators(REPOSITORY, transport=transport),
            (
                {
                    "repository": "ghcr.io/" + REPOSITORY,
                    "digest": published.digest,
                    "immutable_reference": published.immutable_reference,
                },
            ),
        )

    def test_canonical_tags_enumerate_as_immutable_digest_locators(self) -> None:
        transport = FakeRegistryTransport()
        digest = "a" * 64
        transport.manifests[(REPOSITORY, CANONICAL_TAG_PREFIX + digest)] = (
            b"canonical bottle"
        )

        self.assertEqual(
            list_public_record_locators(
                REPOSITORY,
                transport=transport,
                tag_prefix=CANONICAL_TAG_PREFIX,
            ),
            (
                {
                    "repository": "ghcr.io/" + REPOSITORY,
                    "digest": "sha256:" + digest,
                    "immutable_reference": (
                        "ghcr.io/" + REPOSITORY + "@sha256:" + digest
                    ),
                },
            ),
        )

    def test_mixed_candidate_package_selects_each_closed_record_class(self) -> None:
        transport = FakeRegistryTransport()
        record_digest = "a" * 64
        verification_digest = "b" * 64
        reuse_digest = "c" * 64
        transport.manifests[(REPOSITORY, "record-sha256-" + record_digest)] = b"record"
        transport.manifests[
            (
                REPOSITORY,
                "verification-" + "d" * 32 + "-build-sha256-" + verification_digest,
            )
        ] = b"verification"
        transport.manifests[(REPOSITORY, REUSE_TAG_PREFIX + reuse_digest)] = b"reuse"

        candidate_locators = list_public_record_locators(
            REPOSITORY,
            transport=transport,
            allow_verification_tags=True,
            allow_reuse_tags=True,
        )
        reuse_locators = list_public_record_locators(
            REPOSITORY,
            transport=transport,
            tag_prefix=REUSE_TAG_PREFIX,
            allow_verification_tags=True,
            allow_reuse_tags=True,
        )

        self.assertEqual(
            candidate_locators,
            (
                {
                    "repository": "ghcr.io/" + REPOSITORY,
                    "digest": "sha256:" + record_digest,
                    "immutable_reference": (
                        "ghcr.io/" + REPOSITORY + "@sha256:" + record_digest
                    ),
                },
            ),
        )
        self.assertEqual(reuse_locators[0]["digest"], "sha256:" + reuse_digest)

    def test_hidden_anonymous_namespace_is_empty_only_for_public_inventory(self) -> None:
        transport = UrllibOciTransportV1(username="", token="")
        inventory_url = f"https://ghcr.io/v2/{REPOSITORY}/tags/list?n=100"
        token_url = (
            "https://ghcr.io/token?service=ghcr.io&scope="
            f"repository%3A{REPOSITORY.replace('/', '%2F')}%3Apull"
        )
        challenge = (
            'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
            f'scope="repository:{REPOSITORY}:pull"'
        )
        responses = iter(
            (
                HttpResponseV1(
                    401,
                    {"www-authenticate": challenge},
                    b'{"errors":[{"code":"UNAUTHORIZED"}]}',
                    inventory_url,
                ),
                HttpResponseV1(
                    403,
                    {},
                    (
                        b'{"errors":[{"code":"DENIED","message":'
                        b'"requested access to the resource is denied"}]}'
                    ),
                    token_url,
                ),
            )
        )
        with patch.object(transport, "_perform", side_effect=responses):
            self.assertEqual(
                list_public_record_locators(REPOSITORY, transport=transport), ()
            )

    def test_anonymous_bearer_exchange_403_is_retryable_when_not_hidden(self) -> None:
        transport = UrllibOciTransportV1(username="", token="")
        inventory_url = f"https://ghcr.io/v2/{REPOSITORY}/tags/list?n=100"
        token_url = (
            "https://ghcr.io/token?service=ghcr.io&scope="
            f"repository%3A{REPOSITORY.replace('/', '%2F')}%3Apull"
        )
        challenge = (
            'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
            f'scope="repository:{REPOSITORY}:pull"'
        )
        responses = iter(
            (
                HttpResponseV1(
                    401,
                    {"www-authenticate": challenge},
                    b"",
                    inventory_url,
                ),
                HttpResponseV1(403, {}, b'{"message":"temporary denial"}', token_url),
            )
        )
        with patch.object(transport, "_perform", side_effect=responses), self.assertRaises(
            OciPublicationError
        ) as raised:
            list_public_record_locators(REPOSITORY, transport=transport)
        self.assertTrue(raised.exception.retryable)

    def test_manifest_is_canonical_and_preserves_exact_descriptor_order(self) -> None:
        plan = _plan()
        manifest = build_oci_manifest(plan)
        decoded = json.loads(manifest)
        self.assertEqual(decoded["artifactType"], CANDIDATE_RECORD_MEDIA_TYPE)
        self.assertEqual(
            [decoded["config"]["annotations"]["dev.kandelo.abi-staging.role"]]
            + [
                layer["annotations"]["dev.kandelo.abi-staging.role"]
                for layer in decoded["layers"]
            ],
            ["candidate-record", "bottle-layer", "bottle-metadata"],
        )
        self.assertEqual(manifest, canonical_bytes(decoded))
        self.assertNotIn(REPOSITORY.encode(), manifest)

    def test_new_namespace_mount_upload_and_anonymous_readback(self) -> None:
        transport = FakeRegistryTransport()
        transport.package_absent_until_first_manifest = True
        transport.blobs_hidden_until_first_manifest = True
        plan = _plan()
        mounted = plan.layers[0]
        transport.blobs[(mounted.mount_from, mounted.digest)] = mounted.body
        locator = publish_record(
            plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertIsInstance(locator, PublishedRecordLocatorV1)
        manifest = build_oci_manifest(plan)
        digest = hashlib.sha256(manifest).hexdigest()
        self.assertEqual(locator.digest, "sha256:" + digest)
        self.assertEqual(
            locator.immutable_reference, f"ghcr.io/{REPOSITORY}@sha256:{digest}"
        )
        self.assertEqual(transport.manifests[(REPOSITORY, "sha256:" + digest)], manifest)
        self.assertTrue(any("?mount=" in url for _, url, _ in transport.calls))
        self.assertTrue(any("/blobs/uploads/" in url for _, url, _ in transport.calls))
        self.assertTrue(
            any(
                method == "GET" and not authenticated and "@" not in url
                for method, url, authenticated in transport.calls
            )
        )

    def test_existing_private_or_foreign_namespace_fails_before_any_write(self) -> None:
        for case in ("private", "foreign"):
            transport = FakeRegistryTransport()
            if case == "private":
                transport.visibility = "private"
            else:
                transport.association = "kandelo-dev/other"
            with self.subTest(case=case), self.assertRaises(OciPublicationError):
                publish_record(
                    _plan(),
                    transport=transport,
                    expected_source_repository=SOURCE_ASSOCIATION,
                )
            self.assertFalse(
                any(
                    method in {"POST", "PUT"}
                    for method, _url, _authenticated in transport.calls
                )
            )

    def test_existing_identical_manifest_is_idempotent(self) -> None:
        transport = FakeRegistryTransport()
        plan = _plan()
        transport.blobs[(plan.layers[0].mount_from, plan.layers[0].digest)] = plan.layers[0].body
        first = publish_record(
            plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        transport.calls.clear()
        second = publish_record(
            plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertEqual(first, second)
        self.assertFalse(any(method == "PUT" for method, _, _ in transport.calls))

    def test_tag_and_digest_collisions_fail_closed(self) -> None:
        plan = _plan()
        manifest = build_oci_manifest(plan)
        digest = hashlib.sha256(manifest).hexdigest()
        tag = "record-sha256-" + digest
        for collision in ("tag", "digest"):
            transport = FakeRegistryTransport()
            transport.blobs[
                (plan.layers[0].mount_from, plan.layers[0].digest)
            ] = plan.layers[0].body
            reference = tag if collision == "tag" else "sha256:" + digest
            transport.manifests[(REPOSITORY, reference)] = b"different manifest\n"
            with self.subTest(collision=collision), self.assertRaisesRegex(
                OciPublicationError, "collision"
            ):
                publish_record(
                    plan,
                    transport=transport,
                    expected_source_repository=SOURCE_ASSOCIATION,
                )

    def test_association_digest_size_visibility_and_readback_drift_fail(self) -> None:
        cases = ("association", "digest", "size", "private", "drift")
        for case in cases:
            transport = FakeRegistryTransport()
            plan = _plan()
            transport.blobs[
                (plan.layers[0].mount_from, plan.layers[0].digest)
            ] = plan.layers[0].body
            if case == "association":
                transport.association = "kandelo-dev/other"
            elif case == "digest":
                transport.wrong_digest = True
            elif case == "size":
                transport.wrong_size = True
            elif case == "private":
                transport.private_anonymous = True
            else:
                transport.drift_anonymous = True
            with self.subTest(case=case), self.assertRaises(OciPublicationError) as raised:
                publish_record(
                    plan,
                    transport=transport,
                    expected_source_repository=SOURCE_ASSOCIATION,
                )
            expected_guard = (
                "namespace_bootstrap_failed"
                if case == "association"
                else "candidate_public_readback_failed"
            )
            self.assertEqual(raised.exception.guard_code, expected_guard)

    def test_retryable_server_error_and_hostile_redirect_are_distinct(self) -> None:
        plan = _plan()
        retrying = FakeRegistryTransport()
        retrying.registry_next_status = 503
        with self.assertRaises(OciPublicationError) as raised:
            publish_record(
                plan,
                transport=retrying,
                expected_source_repository=SOURCE_ASSOCIATION,
            )
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.kind, "registry-http")
        self.assertEqual(raised.exception.http_status, 503)

        phased = raised.exception.with_phase("candidate-record-publication")
        self.assertEqual(phased.phase, "candidate-record-publication")
        self.assertEqual(phased.kind, "registry-http")
        self.assertEqual(phased.http_status, 503)
        self.assertTrue(phased.retryable)

        redirected = FakeRegistryTransport()
        redirected.redirect_url = "https://registry-attacker.example/v2/steal"
        with self.assertRaises(OciPublicationError) as raised:
            publish_record(
                plan,
                transport=redirected,
                expected_source_repository=SOURCE_ASSOCIATION,
            )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.kind, "registry-contract")
        self.assertIsNone(raised.exception.http_status)
        self.assertIn("redirect", str(raised.exception))

    def test_hostile_upload_location_fails_closed(self) -> None:
        transport = FakeRegistryTransport()
        transport.hostile_upload_location = True
        with self.assertRaises(OciPublicationError) as raised:
            publish_record(
                _plan(),
                transport=transport,
                expected_source_repository=SOURCE_ASSOCIATION,
            )
        self.assertEqual(raised.exception.guard_code, "namespace_bootstrap_failed")
        self.assertIn("hostile", str(raised.exception))

    def test_github_registry_upload_session_location_is_accepted(self) -> None:
        transport = FakeRegistryTransport()
        transport.github_upload_location = True
        locator = publish_record(
            _plan(),
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertIsInstance(locator, PublishedRecordLocatorV1)
        self.assertTrue(
            any(
                method == "PUT"
                and "/blobs/upload/session-" in url
                and authenticated
                for method, url, authenticated in transport.calls
            )
        )

    def test_upload_session_query_is_preserved_when_digest_is_appended(self) -> None:
        transport = FakeRegistryTransport()
        transport.github_upload_location = True
        transport.upload_location_query = "?state=exact-session"
        publish_record(
            _plan(),
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        completion = next(
            url
            for method, url, _authenticated in transport.calls
            if method == "PUT" and "/blobs/upload/session-" in url
        )
        self.assertEqual(
            parse_qs(urlsplit(completion).query),
            {"digest": [_plan().config.digest], "state": ["exact-session"]},
        )

    def test_blob_upload_completion_uses_octet_stream_media_type(self) -> None:
        transport = FakeRegistryTransport()
        transport.github_upload_location = True
        publish_record(
            _plan(),
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertTrue(transport.blob_upload_content_types)
        self.assertEqual(
            set(transport.blob_upload_content_types), {"application/octet-stream"}
        )

    def test_blob_write_response_size_describes_the_empty_http_response(self) -> None:
        transport = FakeRegistryTransport()
        transport.empty_write_response = True
        plan = _plan()
        mounted = plan.layers[0]
        transport.blobs[(mounted.mount_from, mounted.digest)] = mounted.body

        locator = publish_record(
            plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )

        self.assertIsInstance(locator, PublishedRecordLocatorV1)

    def test_oras_credentials_use_stdin_and_ephemeral_config(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(arguments, **options):
            config = Path(arguments[arguments.index("--registry-config") + 1])
            config.write_text("{}\n", encoding="utf-8")
            observed["arguments"] = tuple(arguments)
            observed["config"] = config
            observed["input"] = options["input"]
            return subprocess.CompletedProcess(arguments, 0, b"", b"")

        with patch("scripts.abi_staging.oci.subprocess.run", side_effect=fake_run):
            with isolated_oras_transport(username="publisher", token="secret-token"):
                config = observed["config"]
                self.assertIsInstance(config, Path)
                self.assertTrue(config.is_file())
        self.assertFalse(config.exists())
        self.assertEqual(observed["input"], b"secret-token")
        self.assertNotIn("secret-token", observed["arguments"])

    def test_missing_credentials_fail_before_oras_runs(self) -> None:
        with patch("scripts.abi_staging.oci.subprocess.run") as run:
            with self.assertRaises(OciPublicationError):
                with isolated_oras_transport(username="", token=""):
                    self.fail("missing credentials cannot yield a transport")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
