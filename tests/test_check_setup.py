"""Tests for check_setup.py. Tools are simulated, apart from a real local Git repository."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_setup as setup  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[1]
ACCOUNT = '{"user": "someone@contoso.com", "tenant": "11111111-1111-1111-1111-111111111111"}'


def tools(answers):
    """Simulate command-line tools. Answers map a command's first words to (exit code, output)."""
    def which(name):
        return f"/bin/{name}" if any(key.split()[0] == name for key in answers) else None

    def run(command, **_kwargs):
        name = " ".join([Path(command[0]).name, *command[1:]])
        for key, (code, output) in answers.items():
            if name.startswith(key):
                return mock.Mock(returncode=code, stdout=output, stderr="")
        return mock.Mock(returncode=1, stdout="", stderr="unexpected")

    return which, run


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def simulate(self, answers):
        which, run = tools(answers)
        patchers = [mock.patch.object(setup.shutil, "which", which), mock.patch.object(setup.subprocess, "run", run)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class ToolTests(Harness):
    def test_python_versions(self):
        self.assertEqual(setup.check_python((3, 13, 1)).status, setup.OK)
        self.assertEqual(setup.check_python((3, 9, 0)).status, setup.OK)
        old = setup.check_python((3, 8, 18))
        self.assertEqual(old.status, setup.FIX)
        self.assertIn("python.org", old.fix)

    def test_git_versions(self):
        self.simulate({"git --version": (0, "git version 2.43.0\n")})
        self.assertEqual(setup.check_git().status, setup.OK)
        self.simulate({"git --version": (0, "git version 2.30.9\n")})
        self.assertEqual(setup.check_git().status, setup.FIX)

    def test_windows_git_version_reads_correctly(self):
        self.simulate({"git --version": (0, "git version 2.45.1.windows.1\n")})
        check = setup.check_git()
        self.assertEqual(check.status, setup.OK)
        self.assertEqual(check.detail, "Version 2.45.1")

    def test_git_missing(self):
        self.simulate({})
        self.assertIn("git-scm.com", setup.check_git().fix)

    def test_azure_cli_missing(self):
        self.simulate({})
        check = setup.check_azure_cli(self.tmp)
        self.assertEqual(check.status, setup.FIX)
        self.assertIn("install-azure-cli", check.fix)

    def test_azure_cli_missing_but_the_plugin_can_sign_in(self):
        self.simulate({})
        (self.tmp / "scripts").mkdir()
        (self.tmp / "scripts" / "auth.py").write_text("")
        self.assertEqual(setup.check_azure_cli(self.tmp).status, setup.INFO)

    def test_azure_cli_signed_out(self):
        self.simulate({"az account show": (1, "")})
        check = setup.check_azure_cli(self.tmp)
        self.assertEqual(check.status, setup.FIX)
        self.assertIn("az login --tenant", check.fix)

    def test_azure_cli_shows_the_account_and_tenant_to_confirm(self):
        self.simulate({"az account show": (0, ACCOUNT)})
        check = setup.check_azure_cli(self.tmp)
        self.assertEqual(check.status, setup.OK)
        self.assertIn("someone@contoso.com", check.detail)
        self.assertIn("11111111-1111", check.detail)
        self.assertIn("Confirm", check.detail)

    def test_unreadable_azure_cli_output_needs_a_fix(self):
        self.simulate({"az account show": (0, "not json")})
        self.assertEqual(setup.check_azure_cli(self.tmp).status, setup.FIX)

    def test_pac_missing(self):
        self.simulate({})
        self.assertIn("developer/cli", setup.check_pac().fix)

    def test_pac_without_a_profile(self):
        self.simulate({"pac auth list": (0, "No profiles were found on this computer.\n")})
        check = setup.check_pac()
        self.assertEqual(check.status, setup.FIX)
        self.assertIn("pac auth create", check.fix)

    def test_pac_with_no_active_profile(self):
        self.simulate({"pac auth list": (0, "Index Active Kind\n[1]         UNIVERSAL\n")})
        self.assertIn("pac auth select", setup.check_pac().fix)

    def test_pac_shows_the_active_profile_to_confirm(self):
        self.simulate({"pac auth list": (0, "Index Active Kind\n[1]   *      UNIVERSAL   someone@contoso.com\n")})
        check = setup.check_pac()
        self.assertEqual(check.status, setup.OK)
        self.assertIn("someone@contoso.com", check.detail)

    def test_no_check_ever_asks_for_a_token(self):
        commands = []

        def run(command, **_kwargs):
            commands.append(" ".join(command))
            return mock.Mock(returncode=0, stdout=ACCOUNT, stderr="")

        with mock.patch.object(setup.subprocess, "run", run), mock.patch.object(setup.shutil, "which", lambda name: f"/bin/{name}"):
            setup.collect(self.tmp, None)
        self.assertTrue(commands)
        self.assertFalse([command for command in commands if "get-access-token" in command])


class FolderTests(Harness):
    def git(self, *arguments):
        subprocess.run(["git", *arguments], cwd=self.tmp, capture_output=True, check=True)

    def test_this_repository_is_refused(self):
        check = setup.check_folder(REPOSITORY)[0]
        self.assertEqual(check.status, setup.FIX)
        self.assertIn("public repository", check.detail)

    def test_another_plugin_folder_is_not_refused(self):
        (self.tmp / ".claude-plugin").mkdir()
        (self.tmp / ".claude-plugin" / "plugin.json").write_text('{"name": "something-else"}')
        self.assertEqual(setup.check_folder(self.tmp)[0].status, setup.INFO)

    def test_a_folder_outside_git_is_noted(self):
        self.assertEqual(setup.check_folder(self.tmp)[0].status, setup.INFO)

    def test_a_repository_that_would_commit_the_folders_needs_a_fix(self):
        self.git("init", "-q")
        check = setup.check_folder(self.tmp)[0]
        self.assertEqual(check.status, setup.FIX)
        self.assertIn("build/ and sources/", check.detail)

    def test_only_the_unprotected_folder_is_named(self):
        self.git("init", "-q")
        (self.tmp / ".gitignore").write_text("build/\n")
        check = setup.check_folder(self.tmp)[0]
        self.assertIn("Git would commit sources/.", check.detail)

    def test_a_protected_repository_passes(self):
        self.git("init", "-q")
        (self.tmp / ".gitignore").write_text("build/\nsources/\n")
        check = setup.check_folder(self.tmp)[0]
        self.assertEqual(check.status, setup.OK)
        self.assertIn("Confirm the repository is private", check.detail)


class ConfigTests(Harness):
    def good(self, **changes):
        data = {
            "targets": ["Core", "Sales"],
            "dev": "https://contoso-dev.crm.dynamics.com",
            "prod": "https://contoso.crm.dynamics.com",
            "includes": {"power_pages": False, "sharepoint": True, "azure_functions": True, "plugins": True},
        }
        data.update(changes)
        return data

    def assert_problem(self, data, words):
        problems = setup.validate_config(data)
        self.assertTrue(any(words in problem for problem in problems), problems)

    def test_the_example_is_valid(self):
        data = json.loads((REPOSITORY / "examples" / "audit-config.example.json").read_text())
        self.assertEqual(setup.validate_config(data), [])

    def test_a_minimal_config_is_valid(self):
        self.assertEqual(setup.validate_config(self.good()), [])

    def test_only_an_object_is_accepted(self):
        self.assert_problem(["a"], "one JSON object")

    def test_unknown_settings_are_refused_without_repeating_their_value(self):
        problems = setup.validate_config(self.good(client_secret="abc123"))
        self.assertTrue(any("'client_secret'" in problem for problem in problems))
        self.assertFalse(any("abc123" in problem for problem in problems))

    def test_a_credential_in_an_address_is_refused_without_repeating_it(self):
        problems = setup.validate_config(self.good(dev="https://user:hunter2@contoso-dev.crm.dynamics.com"))
        self.assertTrue(any("no credentials" in problem for problem in problems))
        self.assertFalse(any("hunter2" in problem for problem in problems))

    def test_a_key_in_an_address_is_refused(self):
        self.assert_problem(self.good(sandbox="https://x.crm.dynamics.com/?code=abc"), "no credentials or query")

    def test_addresses_must_use_https(self):
        self.assert_problem(self.good(prod="http://contoso.crm.dynamics.com"), "https://")

    def test_required_settings(self):
        data = self.good()
        del data["prod"]
        self.assert_problem(data, "prod is missing")
        self.assert_problem(self.good(targets=[]), "at least one")
        self.assert_problem(self.good(targets=["bad name"]), "unique names")

    def test_environments_must_differ(self):
        self.assert_problem(self.good(prod="https://CONTOSO-dev.crm.dynamics.com/"), "dev and prod")
        self.assert_problem(self.good(sandbox="https://contoso.crm.dynamics.com"), "sandbox and prod")
        self.assert_problem(self.good(sandbox="https://contoso-dev.crm.dynamics.com"), "sandbox and dev")
        self.assert_problem(self.good(release="https://contoso.crm.dynamics.com"), "release and prod")

    def test_the_scope_must_be_answered_in_full(self):
        self.assert_problem(self.good(includes={"sharepoint": True}), "true or false")
        self.assert_problem(self.good(includes={"power_pages": "yes", "sharepoint": True, "azure_functions": True, "plugins": True}), "true or false")
        extra = dict(self.good()["includes"], api_key="abc")
        self.assert_problem(self.good(includes=extra), "can only hold")

    def test_azure_devops_names_are_plain(self):
        self.assertEqual(setup.validate_config(self.good(azure_devops=[{"organization": "contoso", "project": "My App"}])), [])
        self.assert_problem(self.good(azure_devops=[{"organization": "contoso/x", "project": "App"}]), "azure_devops")
        self.assert_problem(self.good(azure_devops=[{"organization": "contoso"}]), "azure_devops")
        self.assert_problem(self.good(azure_devops={"organization": "contoso", "project": "App"}), "azure_devops")

    def test_the_site_id_is_a_guid(self):
        self.assert_problem(self.good(power_pages_site_id="my-site"), "GUID")
        self.assertEqual(setup.validate_config(self.good(power_pages_site_id="22222222-2222-2222-2222-222222222222")), [])

    def test_check_config_shows_the_values_to_confirm(self):
        path = self.tmp / "audit-config.json"
        path.write_text(json.dumps(self.good()))
        check = setup.check_config(path)[0]
        self.assertEqual(check.status, setup.OK)
        self.assertIn("Solutions Core, Sales.", check.detail)
        self.assertIn("Sandbox not set.", check.detail)
        self.assertIn("Includes sharepoint, azure_functions, plugins.", check.detail)

    def test_a_missing_config_says_how_to_create_it(self):
        check = setup.check_config(self.tmp / "nope.json")[0]
        self.assertEqual(check.status, setup.FIX)
        self.assertIn("audit-config.example.json", check.fix)

    def test_broken_json_is_reported_without_repeating_it(self):
        path = self.tmp / "audit-config.json"
        path.write_text('{"client_secret": "abc123"')
        check = setup.check_config(path)[0]
        self.assertEqual(check.status, setup.FIX)
        self.assertNotIn("abc123", check.detail)


class CommandLineTests(Harness):
    READY = {
        "git --version": (0, "git version 2.43.0\n"),
        "git rev-parse": (1, ""),
        "az account show": (0, ACCOUNT),
        "pac auth list": (0, "[1]   *      UNIVERSAL\n"),
    }

    def main(self, *arguments):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = setup.main(["--project", str(self.tmp), *arguments])
        return code, out.getvalue()

    def test_a_ready_machine_exits_zero(self):
        self.simulate(self.READY)
        code, printed = self.main()
        self.assertEqual(code, 0)
        self.assertIn("Ready.", printed)

    def test_something_to_fix_exits_three_and_prints_the_fix(self):
        self.simulate(dict(self.READY, **{"az account show": (1, "")}))
        code, printed = self.main()
        self.assertEqual(code, 3)
        self.assertIn("[FIX] Azure CLI sign-in", printed)
        self.assertIn("Run: az login --tenant", printed)
        self.assertIn("Things to fix: 1.", printed)

    def test_a_bad_config_exits_three(self):
        self.simulate(self.READY)
        path = self.tmp / "audit-config.json"
        path.write_text("{}")
        code, printed = self.main("--config", str(path))
        self.assertEqual(code, 3)
        self.assertIn("targets must list", printed)

    def test_a_missing_project_folder_is_a_bad_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup.main(["--project", str(self.tmp / "nope")]), 2)

    def test_a_name_the_console_cannot_encode_does_not_stop_it(self):
        path = self.tmp / "audit-config.json"
        path.write_text(json.dumps({"targets": ["Core"], "dev": "https://a.example", "prod": "https://b.example", "includes": {}}))
        result = subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / "check_setup.py"), "--project", str(self.tmp), "--config", str(path)],
            capture_output=True, env=dict(os.environ, PYTHONIOENCODING="ascii"),
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_it_runs_as_a_script(self):
        result = subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / "check_setup.py"), "--project", str(self.tmp)],
            capture_output=True, text=True, env=dict(os.environ),
        )
        self.assertIn(result.returncode, (0, 3), result.stderr)
        self.assertIn("[OK] Python", result.stdout)


if __name__ == "__main__":
    unittest.main()
