"""Tests for fetch_audit_sources.py, and for how both audits read downloaded branches.

Git operations run against a real local repository. Azure DevOps itself isn't contacted.
Run: python -m unittest -v
"""
import csv
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import external_dependency_audit as eda  # noqa: E402
import fetch_audit_sources as fas  # noqa: E402
import solution_dependency_audit as sda  # noqa: E402
import test_external_dependency_audit as ext  # noqa: E402
import test_solution_dependency_audit as dv  # noqa: E402
from solution_dependency_audit import AuditError, ReadOnlyClient  # noqa: E402

ALLOW_LOCAL = ("-c", "protocol.file.allow=always")
TOKEN = "secret-devops-token"


def git(*args, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


class UrlTests(unittest.TestCase):
    def test_accepts_azure_devops_urls(self):
        repo = fas.parse_repo_url("https://dev.azure.com/Contoso/My%20Project/_git/plugins-core")
        self.assertEqual(repo.origin, "https://dev.azure.com")
        self.assertEqual(repo.name, "plugins-core")
        repo = fas.parse_repo_url("https://contoso.visualstudio.com/DefaultCollection/App/_git/func")
        self.assertEqual(repo.origin, "https://contoso.visualstudio.com")

    def test_strips_the_user_name(self):
        repo = fas.parse_repo_url("https://contoso@dev.azure.com/contoso/app/_git/func")
        self.assertEqual(repo.clean, "https://dev.azure.com/contoso/app/_git/func")

    def test_rejects_embedded_credentials_without_echoing_them(self):
        with self.assertRaises(AuditError) as raised:
            fas.parse_repo_url(f"https://user:{TOKEN}@dev.azure.com/contoso/app/_git/func")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_rejects_other_hosts_so_a_token_cannot_leave(self):
        for url in (
            "https://github.com/contoso/app/_git/func",
            "https://dev.azure.com.evil.example/contoso/app/_git/func",
            "https://evilvisualstudio.com/app/_git/func",
            "http://dev.azure.com/contoso/app/_git/func",
            "ssh://git@ssh.dev.azure.com/v3/contoso/app/func",
            "file:///tmp/repo/_git/func",
        ):
            with self.assertRaises(AuditError, msg=url):
                fas.parse_repo_url(url)

    def test_rejects_malformed_paths(self):
        for url in (
            "https://dev.azure.com/contoso/app",
            "https://dev.azure.com/contoso/_git/a/extra",
            "https://dev.azure.com/contoso/../_git/a",
            "https://dev.azure.com/contoso/app/_git/a?x=1",
        ):
            with self.assertRaises(AuditError, msg=url):
                fas.parse_repo_url(url)

    def test_branch_names(self):
        self.assertEqual(fas.validate_branch("refs/heads/release/1.4"), "release/1.4")
        self.assertEqual(fas.validate_branch("feature/abc-1"), "feature/abc-1")
        for bad in ("", "-x", "--upload-pack=evil", "a b", "a..b", "a~1", "a^", "a:b", "a\nb", "x/", "x.lock"):
            with self.assertRaises(AuditError, msg=bad):
                fas.validate_branch(bad)


class TokenHandlingTests(unittest.TestCase):
    def test_token_goes_through_the_environment_and_only_for_the_host(self):
        before = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        env = fas.git_environment("https://dev.azure.com", TOKEN)
        self.assertEqual(env["GIT_CONFIG_COUNT"], str(before + 1))
        self.assertEqual(env[f"GIT_CONFIG_KEY_{before}"], "http.https://dev.azure.com/.extraHeader")
        self.assertIn(TOKEN, env[f"GIT_CONFIG_VALUE_{before}"])
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_existing_git_settings_are_kept(self):
        saved = {k: v for k, v in os.environ.items() if k.startswith("GIT_CONFIG_")}
        os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.proxy", "GIT_CONFIG_VALUE_0": "http://proxy"})
        try:
            env = fas.git_environment("https://dev.azure.com", TOKEN)
        finally:
            for key in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
                del os.environ[key]
            os.environ.update(saved)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "http://proxy")
        self.assertIn(TOKEN, env["GIT_CONFIG_VALUE_1"])

    def test_no_token_means_no_header(self):
        env = fas.git_environment("https://dev.azure.com", None)
        self.assertFalse(any(value.startswith(fas.AUTH_HEADER_PREFIX) for value in env.values()))

    def test_git_errors_never_show_the_token(self):
        env = fas.git_environment("https://dev.azure.com", TOKEN)
        cleaned = fas._clean_error(f"fatal: failed with Bearer {TOKEN} at https://user@dev.azure.com/x", env)
        self.assertNotIn(TOKEN, cleaned)
        self.assertNotIn("user@", cleaned)

    def test_token_is_not_on_the_command_line_or_in_git_config(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bare, _ = make_repo(tmp)
        env = fas.git_environment("https://dev.azure.com", TOKEN)
        calls = []
        original = subprocess.run

        def spy(command, **kwargs):
            calls.append(command)
            return original(command, **kwargs)

        fas.subprocess.run = spy
        try:
            fas.download_branch(bare.as_uri(), "main", tmp / "out", env, ALLOW_LOCAL)
        finally:
            fas.subprocess.run = original
        self.assertTrue(calls)
        self.assertNotIn(TOKEN, json.dumps(calls))
        self.assertNotIn(TOKEN, (tmp / "out" / ".git" / "config").read_text())


def make_repo(tmp):
    work = tmp / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "plugin.cs").write_text("class A {}\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "one", cwd=work)
    git("checkout", "-q", "-b", "release/1.4", cwd=work)
    (work / "plugin.cs").write_text("class A {}\nclass B {}\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "two", cwd=work)
    commit = git("rev-parse", "HEAD", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    bare = tmp / "bare.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return bare, commit


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bare, self.commit = make_repo(self.tmp)
        self.url = self.bare.as_uri()
        self.env = fas.git_environment("https://dev.azure.com", None)

    def entry(self, name="plugins", branch="release/1.4", kind="plugins", function_app=""):
        repo = fas.RepoUrl(self.url, "https://dev.azure.com", name)
        return {"name": name, "repo": repo, "branch": branch, "kind": kind, "function_app": function_app}

    def test_list_branches(self):
        repo = fas.RepoUrl(self.url, "https://dev.azure.com", "x")
        self.assertEqual(fas.list_branches(repo, self.env, ALLOW_LOCAL), ["main", "release/1.4"])

    def test_local_file_urls_are_blocked_by_default(self):
        with self.assertRaises(AuditError):
            fas.download_branch(self.url, "main", self.tmp / "x", self.env)

    def test_downloads_the_chosen_branch_and_records_its_commit(self):
        lock = fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        item = lock["repositories"][0]
        self.assertEqual(item["commit"], self.commit)
        self.assertEqual(item["branch"], "release/1.4")
        self.assertIn("class B", (self.tmp / "sources" / "plugins" / "plugin.cs").read_text())
        self.assertEqual(json.loads((self.tmp / "audit" / "sources-lock.json").read_text())["repositories"][0]["commit"], self.commit)

    def test_a_missing_branch_fails(self):
        with self.assertRaises(AuditError):
            fas.fetch_all([self.entry(branch="nope")], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)

    def test_switching_branch_replaces_the_files(self):
        fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        (self.tmp / "sources" / "plugins" / "stray.txt").write_text("left behind")
        fas.fetch_all([self.entry(branch="main")], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        folder = self.tmp / "sources" / "plugins"
        self.assertNotIn("class B", (folder / "plugin.cs").read_text())
        self.assertFalse((folder / "stray.txt").exists())

    def test_refuses_a_folder_it_did_not_create(self):
        folder = self.tmp / "sources" / "plugins"
        folder.mkdir(parents=True)
        (folder / "important.txt").write_text("keep me")
        with self.assertRaisesRegex(AuditError, "wasn't created by this tool"):
            fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        self.assertTrue((folder / "important.txt").exists())

    def test_refuses_a_folder_holding_another_repository(self):
        fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        with self.assertRaisesRegex(AuditError, "another repository"):
            fas.download_branch("file:///other/repo", "main", self.tmp / "sources" / "plugins", self.env, ALLOW_LOCAL)

    def test_submodules_are_flagged_not_fetched(self):
        work = self.tmp / "work"
        git("checkout", "-q", "release/1.4", cwd=work)
        (work / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = https://example.com/x\n')
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", "sub", cwd=work)
        git("push", "-q", str(self.bare), "release/1.4", cwd=work)
        lock = fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        self.assertTrue(lock["repositories"][0]["submodules_skipped"])
        self.assertTrue(any("submodules" in warning for warning in lock["warnings"]))

    def test_lock_detects_a_changed_folder(self):
        fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        lock_path = self.tmp / "audit" / "sources-lock.json"
        self.assertEqual(len(sda.load_sources_lock(lock_path)), 1)
        data = json.loads(lock_path.read_text())
        data["repositories"][0]["commit"] = "0" * 40
        lock_path.write_text(json.dumps(data))
        with self.assertRaisesRegex(AuditError, "recorded commit"):
            sda.load_sources_lock(lock_path)

    def locked_folder(self):
        fas.fetch_all([self.entry()], self.tmp / "sources", self.tmp / "audit", None, log=lambda _: None, extra_config=ALLOW_LOCAL)
        lock_path = self.tmp / "audit" / "sources-lock.json"
        folder = Path(json.loads(lock_path.read_text())["repositories"][0]["folder"])
        return lock_path, folder

    def test_lock_detects_an_edited_file(self):
        lock_path, folder = self.locked_folder()
        tracked = next(path for path in folder.rglob("*") if path.is_file() and ".git" not in path.parts)
        tracked.write_text("changed")
        with self.assertRaisesRegex(AuditError, "has changes"):
            sda.load_sources_lock(lock_path)

    def test_lock_detects_a_deleted_file(self):
        lock_path, folder = self.locked_folder()
        tracked = next(path for path in folder.rglob("*") if path.is_file() and ".git" not in path.parts)
        tracked.unlink()
        with self.assertRaisesRegex(AuditError, "has changes"):
            sda.load_sources_lock(lock_path)

    def test_lock_refuses_a_folder_git_cannot_read(self):
        lock_path, folder = self.locked_folder()
        # Git marks its pack files read-only, and Windows won't delete those.
        for parent, _folders, files in os.walk(folder / ".git"):
            for name in files:
                os.chmod(Path(parent) / name, stat.S_IWRITE)
        shutil.rmtree(folder / ".git")
        data = json.loads(lock_path.read_text())
        environment = dict(os.environ, GIT_CEILING_DIRECTORIES=str(folder.parent))
        with mock.patch.dict(os.environ, environment):
            with self.assertRaisesRegex(AuditError, "Git can't read"):
                sda.load_sources_lock(lock_path)
        self.assertEqual(len(data["repositories"]), 1)

    def test_lock_detects_a_missing_folder(self):
        lock_path = self.tmp / "lock.json"
        lock_path.write_text(json.dumps({"repositories": [{"folder": str(self.tmp / "gone"), "commit": "x"}]}))
        with self.assertRaisesRegex(AuditError, "wasn't found"):
            sda.load_sources_lock(lock_path)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, data):
        path = self.tmp / "m.json"
        path.write_text(json.dumps(data))
        return path

    def test_reads_entries(self):
        entries = fas.load_manifest(self.write({"repositories": [
            {"url": "https://dev.azure.com/c/p/_git/plugins", "branch": "release/1", "kind": "plugins"},
            {"url": "https://dev.azure.com/c/p/_git/func", "branch": "main", "kind": "functions", "function_app": "func-prod"},
        ]}))
        self.assertEqual([e["name"] for e in entries], ["plugins", "func"])
        self.assertEqual(entries[1]["function_app"], "func-prod")

    def test_requires_a_branch(self):
        with self.assertRaises(AuditError):
            fas.load_manifest(self.write({"repositories": [{"url": "https://dev.azure.com/c/p/_git/a"}]}))

    def test_rejects_an_empty_list(self):
        with self.assertRaises(AuditError):
            fas.load_manifest(self.write({"repositories": []}))

    def test_rejects_duplicate_folders(self):
        with self.assertRaisesRegex(AuditError, "folder name"):
            fas.load_manifest(self.write({"repositories": [
                {"url": "https://dev.azure.com/c/p1/_git/a", "branch": "main"},
                {"url": "https://dev.azure.com/c/p2/_git/a", "branch": "main"},
            ]}))

    def test_same_repository_twice_with_different_names(self):
        entries = fas.load_manifest(self.write({"repositories": [
            {"url": "https://dev.azure.com/c/p/_git/a", "branch": "main", "name": "a-main"},
            {"url": "https://dev.azure.com/c/p/_git/a", "branch": "rel", "name": "a-rel"},
        ]}))
        self.assertEqual(len(entries), 2)

    def test_folder_names_cannot_escape_the_destination(self):
        entries = fas.load_manifest(self.write({"repositories": [
            {"url": "https://dev.azure.com/c/p/_git/a", "branch": "main", "name": "../../evil"},
        ]}))
        self.assertNotIn("/", entries[0]["name"])
        self.assertNotIn("..", entries[0]["name"])


class ListReposTests(unittest.TestCase):
    def test_lists_repositories_and_hides_disabled_ones(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.path)
                body = json.dumps({"value": [
                    {"name": "func", "remoteUrl": "https://dev.azure.com/c/p/_git/func", "defaultBranch": "refs/heads/main"},
                    {"name": "Plugins", "remoteUrl": "https://dev.azure.com/c/p/_git/Plugins"},
                    {"name": "old", "isDisabled": True},
                ]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = ReadOnlyClient(f"http://127.0.0.1:{server.server_address[1]}", lambda _: "t", "Azure DevOps", api_root="/")
        rows = fas.list_repositories(client, "Contoso", "My Project")
        self.assertEqual([row["name"] for row in rows], ["func", "Plugins"])
        self.assertEqual(rows[0]["default_branch"], "main")
        self.assertIn("/Contoso/My%20Project/_apis/git/repositories?api-version=7.1", seen[0])

    def test_rejects_names_that_change_the_address(self):
        client = ReadOnlyClient("http://127.0.0.1:9", lambda _: "t", "Azure DevOps", api_root="/")
        with self.assertRaises(AuditError):
            fas.list_repositories(client, "Contoso/../other", "p")


class AuthLoadingTests(unittest.TestCase):
    def test_uses_the_plugin_auth_module_with_a_scope_for_each_environment(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "auth.py").write_text(
            "def get_token(scope=None):\n    return 'token-for:' + scope\n"
            "def get_plugin_headers(skill, token):\n    return {'User-Agent': skill, 'Authorization': 'Bearer ' + token}\n"
        )
        self.addCleanup(lambda: sys.modules.pop("auth", None))
        self.addCleanup(lambda: sys.path.remove(str(tmp)) if str(tmp) in sys.path else None)
        sys.modules.pop("auth", None)
        tokens, headers = sda.load_auth(str(tmp))
        self.assertEqual(tokens("https://dev.crm.dynamics.com/"), "token-for:https://dev.crm.dynamics.com/.default")
        self.assertEqual(headers("x")["User-Agent"], "unknown")
        self.assertEqual(os.environ.get("DATAVERSE_PLUGIN_VERSION"), "unknown")

    def test_a_missing_auth_module_is_explained(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaisesRegex(AuditError, "Leave out --auth-dir"):
            sda.load_auth(str(tmp))


class CodeClassificationTests(unittest.TestCase):
    def test_generated_code(self):
        self.assertEqual(sda.classify_code("r/Entities.g.cs", ""), "generated")
        self.assertEqual(sda.classify_code("r/Model.cs", "// <auto-generated>\n"), "generated")
        self.assertEqual(sda.classify_code("r/Model.cs", "[GeneratedCode(\"x\", \"1\")]"), "generated")

    def test_test_code(self):
        for label in ("r/Contoso.Plugins.Tests/A.cs", "r/tests/a.py", "r/UnitTests/A.cs", "r/src/OrderTests.cs", "r/a.spec.ts", "r/test_a.py"):
            self.assertEqual(sda.classify_code(label, ""), "test", label)

    def test_ordinary_code(self):
        for label in ("r/latest/A.cs", "r/src/Contest.cs", "r/attestation/a.py", "r/src/Plugin.cs"):
            self.assertEqual(sda.classify_code(label, ""), "", label)


class FunctionDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def read(self, files):
        for name, text in files.items():
            path = self.tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return eda.read_function_definitions(self.tmp)

    def test_csharp_forms(self):
        names, unresolved = self.read({"F.cs": '''
            [Function("GetOrder")] public void A() {}
            [FunctionName("Legacy")] public void B() {}
            [Function(nameof(ByName))] public void ByName() {}
            [Microsoft.Azure.Functions.Worker.Function("Qualified")] public void C() {}
            [Function(Constants.Hidden)] public void D() {}
        '''})
        self.assertEqual(names, {"getorder", "legacy", "byname", "qualified"})
        self.assertEqual(len(unresolved), 1)
        self.assertIn("F.cs:6", unresolved[0])

    def test_python_node_java_and_function_json(self):
        names, _ = self.read({
            "a.py": '@app.function_name(name="PyNamed")\n@app.route(route="x")\ndef f(req): pass\n',
            "b.py": '@app.route(route="y")\ndef py_route(req): pass\n',
            "c.ts": "app.http('NodeHttp', { handler });\napp.timer(\"NodeTimer\", {});\n",
            "D.java": '@FunctionName("JavaFn") public void run() {}',
            "Classic/function.json": '{"bindings": []}',
        })
        self.assertEqual(names, {"pynamed", "py_route", "nodehttp", "nodetimer", "javafn", "classic"})

    def test_test_and_generated_code_are_ignored(self):
        names, _ = self.read({
            "Func.Tests/T.cs": '[Function("FromTest")] void A() {}',
            "G.g.cs": '[Function("FromGenerated")] void A() {}',
            "Real.cs": '[Function("Real")] void A() {}',
        })
        self.assertEqual(names, {"real"})


class BranchComparisonTests(ext.Harness):
    def branch(self, files, **overrides):
        folder = self.tmp / "func-repo"
        for name, text in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        entry = {"name": "func-repo", "branch": "release/1", "commit": "abcdef1234567890", "folder": str(folder), "kind": "functions", "function_app": "func-prod"}
        entry.update(overrides)
        return entry

    def run_with(self, source_entry):
        root = self.tmp / "T"
        root.mkdir(exist_ok=True)
        clients = (
            ReadOnlyClient(self.url, lambda _: ext.TOKEN, "Microsoft Graph", api_root="/v1.0/", sleep=lambda _: None, retries=1),
            ReadOnlyClient(self.url, lambda _: ext.TOKEN, "Azure", api_root="/", sleep=lambda _: None, retries=1),
        )
        scan = [root, Path(source_entry["folder"])] if Path(source_entry["folder"]).exists() else [root]
        config = eda.Config(self.tmp / "map.json", scan, self.out, self.site_pairs, self.app_pairs, [source_entry])
        return eda.ExternalAudit(config, *clients, log=lambda _: None).run()

    def test_a_function_missing_from_the_branch_would_be_removed(self):
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}'}))
        row = self.row("Function", "paused")
        self.assertEqual(row["status"], eda.REMOVED_BY_BRANCH)
        self.assertIn("would remove", row["detail"])

    def test_a_new_function_in_the_branch_is_flagged(self):
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}\n[Function("Paused")] void B() {}\n[Function("Brand New")] void C() {}'}))
        row = self.row("Function", "brand new")
        self.assertEqual(row["status"], eda.NEW_IN_BRANCH)

    def test_a_matching_branch_creates_no_rows(self):
        code = self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}\n[Function("Paused")] void B() {}'}))
        self.assertEqual(code, 0)
        self.assertEqual(self.rows(), [])

    def test_a_branch_replaces_the_dev_only_view(self):
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}'}))
        self.assertNotIn("OtherProject", self.names("Function"))

    def test_a_constant_name_is_unknown(self):
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}\n[Function(Names.Paused)] void B() {}'}))
        rows = [r for r in self.rows() if r["kind"] == "Function name in code"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], eda.UNKNOWN)

    def test_a_branch_with_no_functions_is_unknown(self):
        self.run_with(self.branch({"README.txt": "nothing"}))
        self.assertEqual(self.row("Source branch", "func-repo")["status"], eda.UNKNOWN)

    def test_an_unnamed_function_app_is_unknown(self):
        self.run_with(self.branch({"F.cs": ""}, function_app=""))
        self.assertIn("Name the function app", self.row("Source branch", "func-repo")["detail"])

    def test_an_unmapped_function_app_is_unknown(self):
        self.run_with(self.branch({"F.cs": ""}, function_app="ghost"))
        self.assertIn("isn't in the map file", self.row("Source branch", "func-repo")["detail"])

    def test_azure_failure_is_unknown(self):
        self.cloud.fail["/sites/func-prod/functions"] = 500
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}'}))
        self.assertEqual(self.row("Function app", "func-prod")["status"], eda.UNKNOWN)

    def test_a_missing_production_app_is_not_in_production(self):
        del self.cloud.functions["func-prod"]
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}'}))
        self.assertEqual(self.row("Function app", "func-prod")["status"], eda.NOT_IN_PRODUCTION)

    def test_an_empty_production_app_is_unknown(self):
        self.cloud.functions["func-prod"] = []
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}'}))
        self.assertEqual(self.row("Function app", "func-prod")["status"], eda.UNKNOWN)

    def test_a_url_in_plug_in_code_is_found(self):
        folder = self.tmp / "T"
        folder.mkdir(exist_ok=True)
        (folder / "Plugin.cs").write_text('var u = "https://func-dev.azurewebsites.net/api/newthing";\n')
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}\n[Function("Paused")] void B() {}'}))
        row = self.row("Function", "/api/newthing")
        self.assertEqual(row["status"], eda.NOT_IN_PRODUCTION)
        self.assertIn("doesn't define it", row["detail"])

    def test_sources_appear_in_the_map(self):
        self.run_with(self.branch({"F.cs": '[Function("GetOrder")] void A() {}'}))
        text = (self.out / "external-map.md").read_text()
        self.assertIn("## Source branches", text)
        self.assertIn("release/1", text)
        self.assertIn("abcdef123456", text)


class PluginCodeTests(dv.Harness):
    def add_assembly(self, dev_types, prod_types, prod_exists=True):
        fx = self.fx
        assembly = dv.g("assembly")
        fx.dev.table("pluginassemblies", "pluginassemblyid", [{"pluginassemblyid": assembly, "name": "Contoso.Plugins"}])
        fx.dev.tables["solutioncomponents"].append({
            "solutioncomponentid": dv.g("sc-asm"), "_solutionid_value": fx.sol["Target"], "objectid": assembly,
            "componenttype": 91, "rootcomponentbehavior": None, "rootsolutioncomponentid": None,
        })
        fx.dev.table("plugintypes", "plugintypeid", [
            {"plugintypeid": dv.g(f"dev:{t}"), "typename": t, "_pluginassemblyid_value": assembly} for t in dev_types
        ])
        prod_id = dv.g("prod-assembly")
        fx.prod.table("pluginassemblies", "pluginassemblyid", [{"pluginassemblyid": prod_id, "name": "Contoso.Plugins"}] if prod_exists else [])
        fx.prod.table("plugintypes", "plugintypeid", [
            {"plugintypeid": dv.g(f"prod:{t}"), "typename": t, "_pluginassemblyid_value": prod_id} for t in prod_types
        ])

    def code(self, files):
        folder = self.tmp / "plugins-repo"
        for name, text in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return folder

    def test_type_in_production_but_not_in_the_branch(self):
        self.add_assembly(["Contoso.Plugins.A", "Contoso.Plugins.B"], ["Contoso.Plugins.A", "Contoso.Plugins.B"])
        folder = self.code({"A.cs": "public class A : IPlugin {}"})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        row = self.row("Contoso.Plugins.B")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("no scanned branch defines it", row["production_detail"])

    def test_new_type_in_the_branch_is_flagged(self):
        self.add_assembly(["Contoso.Plugins.A", "Contoso.Plugins.New"], ["Contoso.Plugins.A"])
        folder = self.code({"A.cs": "public class A : IPlugin {}\npublic class New : IPlugin {}"})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        row = self.row("Contoso.Plugins.New")
        self.assertEqual(row["status"], sda.IN_FLIGHT)
        self.assertIn("ships whole", row["production_detail"])

    def test_dev_type_left_out_of_the_branch_is_fine(self):
        self.add_assembly(["Contoso.Plugins.A", "Contoso.Plugins.Other"], ["Contoso.Plugins.A"])
        folder = self.code({"A.cs": "public class A : IPlugin {}"})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        self.assertFalse(any(r["required_name"] == "Contoso.Plugins.Other" for r in self.rows()))

    def test_a_missing_repository_is_reported_once(self):
        self.add_assembly(["Contoso.Plugins.A", "Contoso.Plugins.B"], ["Contoso.Plugins.A"])
        folder = self.code({"Other.cs": "public class Unrelated {}"})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        row = self.row("All plug-in types")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("Add the repository", row["production_detail"])

    def test_classes_in_test_code_do_not_count(self):
        self.add_assembly(["Contoso.Plugins.A"], ["Contoso.Plugins.A"])
        folder = self.code({"Tests/A.cs": "public class A {}"})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        self.assertEqual(self.row("All plug-in types")["status"], sda.UNKNOWN)

    def test_a_failed_read_is_unknown(self):
        self.add_assembly(["Contoso.Plugins.A"], ["Contoso.Plugins.A"])
        self.fx.prod.fail["plugintypes"] = 500
        folder = self.code({"A.cs": "public class A {}"})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        self.assertEqual(self.row("All plug-in types")["status"], sda.UNKNOWN)

    def test_in_flight_names_in_plug_in_code_are_found(self):
        folder = self.code({"src/Handler.cs": 'var c = entity["b_hidden"];\n'})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        row = self.row("b_tbl.b_hidden")
        self.assertEqual(row["status"], sda.IN_FLIGHT)
        self.assertIn("plugins-repo/src/Handler.cs:1", row["evidence"])

    def test_a_name_only_in_generated_code_closes_itself(self):
        folder = self.code({"Model.cs": "// <auto-generated>\npublic class b_hidden {}\n"})
        audit = self.audit(scan_dirs=[folder], skip_impact=True)
        audit.run()
        row = self.row("b_tbl.b_hidden")
        self.assertTrue(row["explanation"].startswith("False positive (automatic)"))
        self.assertGreaterEqual(audit.counts()["False positive"], 1)

    def test_a_name_also_in_real_code_stays_open(self):
        folder = self.code({
            "Model.cs": "// <auto-generated>\npublic class b_hidden {}\n",
            "Handler.cs": 'var c = entity["b_hidden"];\n',
        })
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        rows = [r for r in self.rows() if r["required_name"] == "b_tbl.b_hidden"]
        self.assertEqual(len(rows), 1)
        self.assertIn("Handler.cs", rows[0]["dependent_name"])
        self.assertEqual(rows[0]["explanation"], "")

    def test_a_name_only_in_test_code_needs_confirming(self):
        folder = self.code({"Plugins.Tests/T.cs": 'var c = "b_hidden";\n'})
        self.audit(scan_dirs=[folder], skip_impact=True).run()
        self.assertIn("test code", self.row("b_tbl.b_hidden")["reconcile_note"])

    def test_sources_appear_in_the_map_and_log(self):
        folder = self.code({"A.cs": "class A {}"})
        source = {"name": "plugins-repo", "branch": "release/1", "commit": "abcdef1234567890", "kind": "plugins"}
        self.audit(scan_dirs=[folder], skip_impact=True, sources=[source]).run()
        self.assertIn("abcdef123456", (self.out / "dependency-map.md").read_text())
        self.assertEqual(json.loads((self.out / "run-log.json").read_text())["sources"][0]["branch"], "release/1")


if __name__ == "__main__":
    unittest.main()
