"""Tests for stage_solution.py, run against a local fake Dataverse Web API."""
import base64
import contextlib
import http.client
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import stage_solution as stage  # noqa: E402

TOKEN = "secret-test-token"


class FakeOrg:
    def __init__(self, name, answer=None):
        self.id = str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
        self.name = name
        self.answer = answer or {"StageSolutionResults": {"StageSolutionStatus": "Passed", "MissingDependencies": []}}
        self.status = 200
        self.headers = {}
        self.throttle = 0
        self.posts = []

    def start(self, test):
        org = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if "WhoAmI" in self.path:
                    return self._send(200, {"OrganizationId": org.id})
                return self._send(200, {"name": org.name})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                org.posts.append((self.path, json.loads(self.rfile.read(length)), self.headers.get("Authorization")))
                if org.throttle:
                    org.throttle -= 1
                    return self._send(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"})
                return self._send(org.status, org.answer, org.headers)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        test.addCleanup(server.server_close)
        test.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"


def solution_zip(path, name="Core", version="1.2.0.0", managed="1", entry="solution.xml"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, (
            "<ImportExportXml><SolutionManifest>"
            f"<UniqueName>{name}</UniqueName><Version>{version}</Version><Managed>{managed}</Managed>"
            "</SolutionManifest></ImportExportXml>"
        ))
    return path


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox, self.dev, self.prod = FakeOrg("Sandbox"), FakeOrg("Dev"), FakeOrg("Prod")
        self.sandbox_url = self.sandbox.start(self)
        self.dev_url = self.dev.start(self)
        self.prod_url = self.prod.start(self)
        self.zip = solution_zip(self.tmp / "Core_managed.zip")
        self.out = self.tmp / "audit" / "stage-Core.json"
        self.err = io.StringIO()
        self.printed = io.StringIO()

    def run_it(self, *extra, sandbox=None):
        argv = [
            "--zip", str(self.zip), "--sandbox", sandbox or self.sandbox_url, "--dev", self.dev_url,
            "--prod", self.prod_url, "--out", str(self.out),
        ] + list(extra)
        with mock.patch.object(stage, "load_auth", return_value=(lambda _: TOKEN, None)), \
                mock.patch.object(stage, "validate"), \
                contextlib.redirect_stdout(self.printed), contextlib.redirect_stderr(self.err):
            return stage.main(argv)


class StageTests(Harness):
    def test_a_passed_dry_run_saves_the_result(self):
        self.assertEqual(self.run_it(), 0)
        saved = json.loads(self.out.read_text())
        self.assertEqual(saved["StageSolutionResults"]["StageSolutionStatus"], "Passed")

    def test_it_posts_the_file_to_the_sandbox_only(self):
        self.run_it()
        self.assertEqual(self.dev.posts, [])
        self.assertEqual(self.prod.posts, [])
        path, body, authorization = self.sandbox.posts[0]
        self.assertTrue(path.endswith("/api/data/v9.2/StageSolution"))
        self.assertEqual(base64.b64decode(body["CustomizationFile"]), self.zip.read_bytes())
        self.assertEqual(authorization, f"Bearer {TOKEN}")

    def test_a_failed_dry_run_is_saved_and_exits_three(self):
        self.sandbox.answer = {"StageSolutionResults": {
            "StageSolutionStatus": "Failed", "MissingDependencies": [{"RequiredComponentSchemaName": "b_col"}],
        }}
        self.assertEqual(self.run_it(), 3)
        self.assertEqual(len(json.loads(self.out.read_text())["StageSolutionResults"]["MissingDependencies"]), 1)
        self.assertIn("Missing dependencies: 1", self.printed.getvalue())

    def test_the_numeric_passed_status_is_accepted(self):
        self.sandbox.answer = {"StageSolutionResults": {"StageSolutionStatus": 1, "MissingDependencies": []}}
        self.assertEqual(self.run_it(), 0)

    def test_check_sends_nothing(self):
        self.assertEqual(self.run_it("--check"), 0)
        self.assertEqual(self.sandbox.posts, [])
        self.assertIn("Core 1.2.0.0", self.printed.getvalue())
        self.assertFalse(self.out.exists())

    def test_default_output_names_the_solution(self):
        args = stage.parse_args(["--zip", "a.zip", "--sandbox", "https://s", "--dev", "https://d", "--prod", "https://p"])
        self.assertIsNone(args.out)

    def test_an_http_error_writes_no_file(self):
        self.sandbox.status = 403
        self.sandbox.answer = {"error": {"message": "no privilege"}}
        self.assertEqual(self.run_it(), 1)
        self.assertIn("no privilege", self.err.getvalue())
        self.assertFalse(self.out.exists())

    def test_a_failed_rerun_removes_the_earlier_result(self):
        self.assertEqual(self.run_it(), 0)
        self.assertTrue(self.out.exists())
        self.sandbox.status = 500
        self.sandbox.answer = {"error": {"message": "broken"}}
        self.assertEqual(self.run_it(), 1)
        self.assertFalse(self.out.exists())

    def test_check_leaves_an_earlier_result_alone(self):
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(self.run_it("--check"), 0)
        self.assertTrue(self.out.exists())

    def test_a_cut_off_response_is_reported(self):
        solution = stage.read_solution_file(self.zip)
        with mock.patch.object(stage.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = http.client.IncompleteRead(b"")
            with self.assertRaisesRegex(stage.AuditError, "Could not reach the sandbox"):
                stage.stage(self.sandbox_url, solution, {})

    def test_a_result_that_cannot_be_saved_is_reported(self):
        blocked = self.tmp / "file-not-folder"
        blocked.write_text("x")
        with self.assertRaisesRegex(stage.AuditError, "couldn't be saved"):
            stage.save_result({"a": 1}, blocked / "stage.json")

    def test_a_response_without_results_writes_no_file(self):
        self.sandbox.answer = {"unexpected": True}
        self.assertEqual(self.run_it(), 1)
        self.assertFalse(self.out.exists())

    def test_throttling_is_retried(self):
        self.sandbox.throttle = 2
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(len(self.sandbox.posts), 3)

    def test_a_redirect_is_refused(self):
        self.sandbox.status = 302
        self.sandbox.headers = {"Location": "http://127.0.0.1:9/stolen"}
        self.assertEqual(self.run_it(), 1)
        self.assertFalse(self.out.exists())

    def test_the_token_is_never_saved_or_printed(self):
        self.run_it()
        self.assertNotIn(TOKEN, self.out.read_text())
        self.assertNotIn(TOKEN, self.printed.getvalue() + self.err.getvalue())


class SafetyTests(Harness):
    def assert_refused(self, code, message):
        self.assertEqual(code, 1)
        self.assertIn(message, self.err.getvalue())
        self.assertEqual(self.sandbox.posts + self.dev.posts + self.prod.posts, [])

    def test_refuses_production(self):
        self.assert_refused(self.run_it(sandbox=self.prod_url), "same environment")

    def test_refuses_dev(self):
        self.assert_refused(self.run_it(sandbox=self.dev_url + "/"), "same environment")

    def test_refuses_production_under_another_url(self):
        self.sandbox.id = self.prod.id
        self.assert_refused(self.run_it(), "same organization")

    def test_refuses_an_unmanaged_file(self):
        self.zip = solution_zip(self.tmp / "unmanaged.zip", managed="0")
        self.assert_refused(self.run_it(), "isn't a managed solution")

    def test_refuses_when_dev_and_production_are_the_same_url(self):
        argv = ["--zip", str(self.zip), "--sandbox", self.prod_url, "--dev", self.dev_url, "--prod", self.dev_url, "--out", str(self.out)]
        with mock.patch.object(stage, "load_auth", return_value=(lambda _: TOKEN, None)), mock.patch.object(stage, "validate"), \
                contextlib.redirect_stdout(self.printed), contextlib.redirect_stderr(self.err):
            code = stage.main(argv)
        self.assert_refused(code, "can't tell what production is")

    def test_refuses_when_dev_and_production_are_one_organization(self):
        self.dev.id = self.prod.id
        self.assert_refused(self.run_it(), "--dev and --prod point to the same organization")

    def test_a_shared_organization_id_can_be_confirmed(self):
        self.sandbox.id = self.prod.id
        self.assertEqual(self.run_it("--allow-same-org-id"), 0)
        self.assertEqual(len(self.sandbox.posts), 1)

    def test_confirming_an_organization_id_never_allows_the_same_url(self):
        self.assert_refused(self.run_it("--allow-same-org-id", sandbox=self.prod_url), "same environment")

    def test_refuses_a_file_managed_and_unmanaged_together(self):
        self.zip = solution_zip(self.tmp / "both.zip", managed="2")
        self.assert_refused(self.run_it(), "isn't a managed solution")

    def test_refuses_a_zip_without_a_manifest(self):
        with zipfile.ZipFile(self.tmp / "other.zip", "w") as archive:
            archive.writestr("readme.txt", "hello")
        self.zip = self.tmp / "other.zip"
        self.assert_refused(self.run_it(), "isn't a solution file")

    def test_refuses_a_file_that_is_not_a_zip(self):
        self.zip = self.tmp / "bad.zip"
        self.zip.write_bytes(b"not a zip")
        self.assert_refused(self.run_it(), "Could not read")

    def test_reads_the_manifest_whatever_its_case(self):
        self.zip = solution_zip(self.tmp / "case.zip", entry="Solution.xml")
        self.assertEqual(self.run_it(), 0)

    def test_urls_must_be_https(self):
        args = stage.parse_args(["--zip", str(self.zip), "--sandbox", "http://s", "--dev", "https://d", "--prod", "https://p"])
        with self.assertRaisesRegex(stage.AuditError, "must be an https"):
            stage.validate(args)

    def test_bad_arguments_exit_with_two(self):
        with contextlib.redirect_stderr(self.err):
            self.assertEqual(stage.main(["--zip", "missing.zip", "--sandbox", "https://s", "--dev", "https://d", "--prod", "https://p"]), 2)

    def test_the_file_must_exist(self):
        args = stage.parse_args(["--zip", "missing.zip", "--sandbox", "https://s", "--dev", "https://d", "--prod", "https://p"])
        with self.assertRaisesRegex(stage.AuditError, "wasn't found"):
            stage.validate(args)


class AuditIntegrationTests(unittest.TestCase):
    def test_the_saved_file_has_the_shape_the_audit_reads(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        result = {"@odata.context": "x", "StageSolutionResults": {
            "StageSolutionStatus": "Passed", "MissingDependencies": [],
            "SolutionDetails": {"SolutionUniqueName": "Core", "SolutionVersion": "1.0.0.0"},
        }}
        path = tmp / "deep" / "stage-Core.json"
        stage.save_result(result, path)
        self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual([item.name for item in path.parent.iterdir()], ["stage-Core.json"])


if __name__ == "__main__":
    unittest.main()
