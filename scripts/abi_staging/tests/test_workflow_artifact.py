from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse
import zipfile


REPOSITORY = "kandelo-dev/homebrew-tap-core"
RUN_ID = 808
RUN_ATTEMPT = 2
HEAD = "7" * 40
WORK_ID = "a" * 64
ARTIFACT_NAME = f"abi-staging-build-{RUN_ID}-{RUN_ATTEMPT}-{WORK_ID}"
WORKFLOW_REF = (
    "kandelo-dev/homebrew-tap-core/"
    ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
)


class Response:
    def __init__(self, status: int, body: bytes, headers=None) -> None:
        self.status = status
        self.body = body
        self.headers = {
            "content-length": str(len(body)),
            **({} if headers is None else headers),
        }

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def close(self) -> None:
        return None


def _archive(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, body in entries.items():
            bundle.writestr(name, body)
        if symlink is not None:
            entry = zipfile.ZipInfo(symlink)
            entry.create_system = 3
            entry.external_attr = (0o120777 << 16)
            bundle.writestr(entry, "target")
    return output.getvalue()


def _opener(archive: bytes, *, artifact_name: str = ARTIFACT_NAME):
    digest = hashlib.sha256(archive).hexdigest()

    def open_request(request):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname == "api.github.com":
            self_auth = request.headers.get("Authorization")
            if not self_auth:
                raise AssertionError("GitHub API request lacked authentication")
            if parsed.path.endswith(f"/actions/runs/{RUN_ID}/attempts/{RUN_ATTEMPT}"):
                body = {
                    "id": RUN_ID,
                    "run_attempt": RUN_ATTEMPT,
                    "event": "schedule",
                    "head_branch": "main",
                    "head_sha": HEAD,
                    "path": ".github/workflows/abi-staging-reconcile.yml",
                    "status": "in_progress",
                    "conclusion": None,
                    "head_repository": {"full_name": REPOSITORY},
                }
                return Response(200, json.dumps(body).encode())
            if parsed.path.endswith("/actions/artifacts/1001"):
                body = {
                    "id": 1001,
                    "name": artifact_name,
                    "size_in_bytes": len(archive),
                    "expired": False,
                    "digest": "sha256:" + digest,
                    "workflow_run": {"id": RUN_ID, "head_sha": HEAD},
                }
                return Response(200, json.dumps(body).encode())
            if parsed.path.endswith("/actions/artifacts/1001/zip"):
                return Response(
                    302,
                    b"",
                    {"location": "https://objects.githubusercontent.com/exact.zip"},
                )
        if url == "https://objects.githubusercontent.com/exact.zip":
            if request.headers.get("Authorization"):
                raise AssertionError("redirected artifact request retained authentication")
            return Response(200, archive)
        raise AssertionError(f"unexpected fixture request {url}")

    return open_request


class WorkflowArtifactTests(unittest.TestCase):
    def test_artifact_service_server_failure_exposes_only_bounded_protected_facts(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_artifact")
        archive = _archive({"result.json": b"{}\n"})
        normal = _opener(archive)

        def unavailable(request):
            parsed = urllib.parse.urlsplit(request.full_url)
            if parsed.hostname == "api.github.com" and parsed.path.endswith(
                "/actions/artifacts/1001"
            ):
                return Response(503, b"temporarily unavailable")
            return normal(request)

        client = module.GitHubWorkflowArtifactClientV1(
            REPOSITORY,
            "fixture-token",
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            head_sha=HEAD,
            workflow_ref=WORKFLOW_REF,
            opener=unavailable,
        )
        with self.assertRaises(module.WorkflowArtifactServiceError) as raised:
            client.artifact_by_id(
                artifact_id=1001,
                name=ARTIFACT_NAME,
                sha256=hashlib.sha256(archive).hexdigest(),
            )
        self.assertEqual(raised.exception.kind, "artifact-service-unavailable")
        self.assertEqual(raised.exception.http_status, 503)

    def test_exact_needs_output_id_and_digest_are_required_before_safe_extraction(self) -> None:
        try:
            module = importlib.import_module("scripts.abi_staging.workflow_artifact")
        except ModuleNotFoundError:
            module = None
        self.assertIsNotNone(module, "protected workflow artifact bridge is absent")
        assert module is not None
        archive = _archive({"attempt-record.json": b"{}\n", "source/file": b"bytes\n"})
        client = module.GitHubWorkflowArtifactClientV1(
            REPOSITORY,
            "fixture-token",
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            head_sha=HEAD,
            workflow_ref=WORKFLOW_REF,
            opener=_opener(archive),
        )
        artifact = client.artifact_by_id(
            artifact_id=1001,
            name=ARTIFACT_NAME,
            sha256=hashlib.sha256(archive).hexdigest(),
        )
        self.assertEqual(artifact.id, 1001)
        self.assertFalse(hasattr(artifact, "job_id"))
        with tempfile.TemporaryDirectory() as temporary:
            inventory = client.extract_artifact(
                artifact,
                Path(temporary) / "handoff",
                max_files=8,
                max_bytes=1024,
            )
            self.assertEqual(
                sorted(inventory), ["attempt-record.json", "source/file"]
            )
            self.assertEqual(
                (Path(temporary) / "handoff/source/file").read_bytes(), b"bytes\n"
            )

    def test_mutable_name_and_archive_symlink_fail_closed(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_artifact")
        archive = _archive({"result.json": b"{}\n"})
        client = module.GitHubWorkflowArtifactClientV1(
            REPOSITORY,
            "fixture-token",
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            head_sha=HEAD,
            workflow_ref=WORKFLOW_REF,
            opener=_opener(archive, artifact_name="candidate-latest"),
        )
        with self.assertRaises(module.WorkflowArtifactError):
            client.artifact_by_id(
                artifact_id=1001,
                name=ARTIFACT_NAME,
                sha256=hashlib.sha256(archive).hexdigest(),
            )

        hostile = _archive({"result.json": b"{}\n"}, symlink="escape")
        client = module.GitHubWorkflowArtifactClientV1(
            REPOSITORY,
            "fixture-token",
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            head_sha=HEAD,
            workflow_ref=WORKFLOW_REF,
            opener=_opener(hostile),
        )
        artifact = client.artifact_by_id(
            artifact_id=1001,
            name=ARTIFACT_NAME,
            sha256=hashlib.sha256(hostile).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(module.WorkflowArtifactError):
                client.extract_artifact(
                    artifact,
                    Path(temporary) / "handoff",
                    max_files=8,
                    max_bytes=1024,
                )


if __name__ == "__main__":
    unittest.main()
