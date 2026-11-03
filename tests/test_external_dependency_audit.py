"""Tests for external_dependency_audit.py, run against a local fake Graph and Azure API.

The fake follows Microsoft's documented API shapes. It proves the script's logic and its
read-only behaviour, not how a live tenant responds. Run: python -m unittest -v
"""
import contextlib
import csv
import io
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import external_dependency_audit as eda  # noqa: E402
from solution_dependency_audit import ApiError, AuditError, ReadOnlyClient  # noqa: E402

TOKEN = "secret-test-token"
FUNCTION_KEY = "SuperSecretFunctionKey123"
LOCAL_KEY = "LocalOnlyKey456"
DEV_SITE = "https://contoso.sharepoint.com/sites/app-dev"
PROD_SITE = "https://contoso.sharepoint.com/sites/app"
DEV_LIST = "11111111-1111-1111-1111-111111111111"
PROD_LIST = "22222222-2222-2222-2222-222222222222"
DEV_ONLY_LIST = "33333333-3333-3333-3333-333333333333"
MISSING_LIST = "44444444-4444-4444-4444-444444444444"
SUB = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def function(app, name, route=None, disabled=False):
    template = f"https://{app}.azurewebsites.net{route}" if route else None
    return {"name": name, "properties": {"invoke_url_template": template, "isDisabled": disabled}}


class FakeCloud:
    def __init__(self):
        self.log = []
        self.fail = {}
        self.sites = {
            "contoso.sharepoint.com:/sites/app-dev": {"id": "dev-site", "displayName": "App DEV", "webUrl": DEV_SITE},
            "contoso.sharepoint.com:/sites/app": {"id": "prod-site", "displayName": "App", "webUrl": PROD_SITE},
        }
        self.lists = {
            "dev-site": [
                {"id": DEV_LIST, "name": "Orders", "displayName": "Orders"},
                {"id": DEV_ONLY_LIST, "name": "NewList", "displayName": "New List"},
            ],
            "prod-site": [{"id": PROD_LIST, "name": "Orders", "displayName": "Orders"}],
        }
        self.columns = {
            ("dev-site", DEV_LIST): [
                {"name": "Title", "displayName": "Title", "hidden": False},
                {"name": "Status", "displayName": "Status", "hidden": False},
                {"name": "OnlyDevUsed", "displayName": "Only DEV used", "hidden": False},
                {"name": "OnlyDevUnused", "displayName": "Only DEV unused", "hidden": False},
                {"name": "Renamed", "displayName": "New label", "hidden": False},
                {"name": "HiddenOne", "displayName": "Hidden", "hidden": True},
            ],
            ("prod-site", PROD_LIST): [
                {"name": "Title", "displayName": "Title", "hidden": False},
                {"name": "Status", "displayName": "Status", "hidden": False},
                {"name": "Renamed", "displayName": "Old label", "hidden": False},
            ],
        }
        self.functions = {
            "func-dev": [
                function("func-dev", "GetOrder", "/api/orders/{id}"),
                function("func-dev", "NewThing", "/api/newthing"),
                function("func-dev", "Paused", "/api/paused"),
                function("func-dev", "OtherProject", "/api/otherproject"),
                function("func-dev", "TimerJob"),
            ],
            "func-prod": [
                function("func-prod", "GetOrder", "/api/orders/{id}"),
                function("func-prod", "Paused", "/api/paused", disabled=True),
            ],
        }


def start(cloud):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _not_allowed(self):
            cloud.log.append((self.command, self.path))
            self._send(405, {"error": {"message": "writes are not allowed"}})

        do_POST = do_PUT = do_PATCH = do_DELETE = _not_allowed

        def do_GET(self):
            cloud.log.append(("GET", self.path))
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"error": {"message": "bad token"}})
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            for fragment, status in cloud.fail.items():
                if fragment in path:
                    return self._send(status, {"error": {"message": f"forced {status}"}})
            match = re.fullmatch(r"/v1\.0/sites/([^/]+(?::/.+?)?)/lists/([^/]+)/columns", path)
            if match:
                return self._send(200, {"value": cloud.columns.get((match.group(1), match.group(2)), [])})
            match = re.fullmatch(r"/v1\.0/sites/([^/:]+)/lists", path)
            if match:
                return self._send(200, {"value": cloud.lists.get(match.group(1), [])})
            match = re.fullmatch(r"/v1\.0/sites/(.+)", path)
            if match:
                site = cloud.sites.get(match.group(1))
                return self._send(200, site) if site else self._send(404, {"error": {"message": "no site"}})
            match = re.fullmatch(r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.Web/sites/([^/]+)/functions", path)
            if match:
                functions = cloud.functions.get(match.group(1))
                if functions is None:
                    return self._send(404, {"error": {"message": "no app"}})
                return self._send(200, {"value": functions})
            self._send(404, {"error": {"message": f"unknown {path}"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def sharepoint_action(site, table, **extra):
    parameters = {"dataset": site, "table": table}
    parameters.update(extra)
    return {"inputs": {"host": {"connectionName": "shared_sharepointonline", "operationId": "GetItems"}, "parameters": parameters}}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cloud = FakeCloud()
        self.server, self.url = start(self.cloud)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.out = self.tmp / "audit"
        self.site_pairs = [{"dev": DEV_SITE, "prod": PROD_SITE}]
        app = {"subscription": SUB, "resource_group": "rg"}
        self.app_pairs = [{"dev": dict(app, name="func-dev"), "prod": dict(app, name="func-prod"), "aliases": ["api-dev.contoso.com"]}]

    def source(self):
        root = self.tmp / "Target"
        (root / "Workflows").mkdir(parents=True)
        (root / "WebResources").mkdir()
        (root / "Connector").mkdir()
        (root / "environmentvariabledefinitions" / "t_site").mkdir(parents=True)
        (root / "environmentvariabledefinitions" / "t_list").mkdir(parents=True)
        flows = {
            "literal": sharepoint_action(DEV_SITE, DEV_LIST, **{"item/Status": "New", "item/OnlyDevUsed": "x", "$select": "Title,Status"}),
            "missing": sharepoint_action(DEV_SITE, DEV_ONLY_LIST),
            "ghost": sharepoint_action(DEV_SITE, MISSING_LIST),
            "unmapped": sharepoint_action("https://contoso.sharepoint.com/sites/other", DEV_LIST),
            "runtime": sharepoint_action("@{triggerBody()?['site']}", "@{variables('list')}"),
            "viaenv": sharepoint_action("@parameters('Site (t_site)')", "@parameters('List (t_list)')"),
        }
        (root / "Workflows" / "flow.json").write_text(json.dumps({"properties": {"definition": {"actions": flows}}}))
        (root / "environmentvariabledefinitions" / "t_site" / "environmentvariabledefinition.xml").write_text(
            f"<environmentvariabledefinition><defaultvalue>{DEV_SITE}</defaultvalue></environmentvariabledefinition>"
        )
        (root / "environmentvariabledefinitions" / "t_list" / "environmentvariabledefinition.xml").write_text(
            f"<environmentvariabledefinition><defaultvalue>{DEV_LIST}</defaultvalue></environmentvariabledefinition>"
        )
        (root / "environmentvariabledefinitions" / "t_site" / "environmentvariablevalues.json").write_text(
            json.dumps({"environmentvariablevalues": {"environmentvariablevalue": {"@schemaname": "t_site", "value": DEV_SITE}}})
        )
        (root / "WebResources" / "a.js").write_text(
            f'fetch("https://func-dev.azurewebsites.net/api/orders/123?code={FUNCTION_KEY}");\n'
            'fetch("https://func-dev.azurewebsites.net/api/newthing");\n'
            'fetch("https://func-prod.azurewebsites.net/api/paused");\n'
            'fetch("https://func-dev.azurewebsites.net/api/nowhere");\n'
            'fetch("https://unmapped.azurewebsites.net/api/x");\n'
            'fetch("https://api-dev.contoso.com/api/orders/9");\n'
            f'const prod = "{PROD_SITE}/Lists/Orders";\n'
        )
        (root / "Connector" / "c_openapidefinition.json").write_text(json.dumps({
            "swagger": "2.0", "host": "func-dev.azurewebsites.net", "basePath": "/api",
            "paths": {"/orders/{id}": {}, "/fromconnector": {}},
        }))
        (root / "local.settings.json").write_text(f'{{"Url": "https://func-dev.azurewebsites.net/api/local?code={LOCAL_KEY}"}}')
        (root / "README.md").write_text("See https://contoso.sharepoint.com/sites/readme-only for docs.")
        with zipfile.ZipFile(root / "App.msapp", "w") as archive:
            archive.writestr("DataSources/Sp.json", json.dumps([{"DatasetName": DEV_SITE, "TableName": DEV_LIST}]))
        return root

    def audit(self, source=None):
        clients = (
            ReadOnlyClient(self.url, lambda _: TOKEN, "Microsoft Graph", api_root="/v1.0/", sleep=lambda _: None, retries=1),
            ReadOnlyClient(self.url, lambda _: TOKEN, "Azure", api_root="/", sleep=lambda _: None, retries=1),
        )
        config = eda.Config(self.tmp / "map.json", [source or self.source()], self.out, self.site_pairs, self.app_pairs)
        return eda.ExternalAudit(config, *clients, log=lambda _: None)

    def rows(self):
        with (self.out / "external-dependencies.csv").open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))

    def row(self, kind, name):
        for row in self.rows():
            if row["kind"] == kind and row["name"].lower() == name.lower():
                return row
        self.fail(f"no {kind} {name}: {[(r['kind'], r['name']) for r in self.rows()]}")

    def names(self, kind):
        return {row["name"] for row in self.rows() if row["kind"] == kind}


class SharePointTests(Harness):
    def test_site_found_and_hardcoded_flagged(self):
        self.assertEqual(self.audit().run(), 3)
        row = self.row("Site", PROD_SITE)
        self.assertEqual(row["status"], eda.IN_PRODUCTION)
        self.assertEqual(row["hardcoded"], "Yes")

    def test_unmapped_site_is_unknown(self):
        self.audit().run()
        self.assertEqual(self.row("Site", "https://contoso.sharepoint.com/sites/other")["status"], eda.UNKNOWN)

    def test_missing_production_site(self):
        del self.cloud.sites["contoso.sharepoint.com:/sites/app"]
        self.audit().run()
        self.assertEqual(self.row("Site", PROD_SITE)["status"], eda.NOT_IN_PRODUCTION)

    def test_list_is_matched_by_display_name_because_ids_differ(self):
        self.audit().run()
        row = self.row("List", "Orders")
        self.assertEqual(row["status"], eda.IN_PRODUCTION)
        self.assertIn("IDs differ", row["detail"])

    def test_list_only_in_dev(self):
        self.audit().run()
        self.assertEqual(self.row("List", "New List")["status"], eda.NOT_IN_PRODUCTION)

    def test_list_not_found_in_dev_is_unknown(self):
        self.audit().run()
        self.assertEqual(self.row("List", MISSING_LIST)["status"], eda.UNKNOWN)

    def test_used_column_missing_from_production(self):
        self.audit().run()
        self.assertEqual(self.row("Column", "OnlyDevUsed")["status"], eda.NOT_IN_PRODUCTION)

    def test_unused_dev_column_is_dev_only(self):
        self.audit().run()
        self.assertEqual(self.row("Column", "OnlyDevUnused")["status"], eda.DEV_ONLY)

    def test_display_name_difference_is_flagged(self):
        self.audit().run()
        row = self.row("Column", "Renamed")
        self.assertEqual(row["status"], eda.UNKNOWN)
        self.assertIn("display name differs", row["detail"])

    def test_hidden_columns_are_ignored(self):
        self.audit().run()
        self.assertNotIn("HiddenOne", self.names("Column"))

    def test_matching_columns_create_no_rows(self):
        self.audit().run()
        self.assertNotIn("Status", self.names("Column"))
        self.assertNotIn("Title", self.names("Column"))

    def test_runtime_values_are_reported_for_manual_check(self):
        self.audit().run()
        rows = [r for r in self.rows() if r["kind"] == "Runtime value"]
        self.assertTrue(rows)
        self.assertTrue(all(r["status"] == eda.UNKNOWN for r in rows))

    def test_environment_variable_is_not_hardcoded(self):
        root = self.tmp / "EnvOnly"
        (root / "Workflows").mkdir(parents=True)
        (root / "environmentvariabledefinitions" / "t_site").mkdir(parents=True)
        (root / "Workflows" / "flow.json").write_text(json.dumps(
            {"actions": {"a": sharepoint_action("@parameters('Site (t_site)')", "Orders")}}
        ))
        (root / "environmentvariabledefinitions" / "t_site" / "environmentvariabledefinition.xml").write_text(
            f"<environmentvariabledefinition><defaultvalue>{DEV_SITE}</defaultvalue></environmentvariabledefinition>"
        )
        self.audit(root).run()
        self.assertEqual(self.row("Site", PROD_SITE)["hardcoded"], "No")

    def test_exported_current_value_is_called_out(self):
        root = self.tmp / "EnvValue"
        (root / "Workflows").mkdir(parents=True)
        folder = root / "environmentvariabledefinitions" / "t_site"
        folder.mkdir(parents=True)
        (root / "Workflows" / "flow.json").write_text(json.dumps(
            {"actions": {"a": sharepoint_action("@parameters('Site (t_site)')", "Orders")}}
        ))
        (folder / "environmentvariablevalues.json").write_text(
            json.dumps({"environmentvariablevalues": {"environmentvariablevalue": {"value": DEV_SITE}}})
        )
        self.audit(root).run()
        self.assertIn("exports its current value", self.row("Site", PROD_SITE)["detail"])

    def flow_only(self, **parameters):
        root = self.tmp / "OneFlow"
        shutil.rmtree(root, True)
        (root / "Workflows").mkdir(parents=True)
        action = {"inputs": {"host": {"connectionName": "shared_sharepointonline", "operationId": "Action"}, "parameters": parameters}}
        (root / "Workflows" / "flow.json").write_text(json.dumps({"actions": {"a": action}}))
        return root

    def test_http_request_action_finds_the_list(self):
        self.audit(self.flow_only(**{"dataset": DEV_SITE, "parameters/method": "GET", "parameters/uri": "_api/web/lists/getbytitle('New List')/items"})).run()
        self.assertEqual(self.row("List", "New List")["status"], eda.NOT_IN_PRODUCTION)

    def test_file_action_finds_the_library(self):
        self.audit(self.flow_only(dataset=DEV_SITE, folderPath="/NewList/2026")).run()
        self.assertEqual(self.row("List", "New List")["status"], eda.NOT_IN_PRODUCTION)

    def test_action_with_no_readable_list_is_unknown(self):
        self.audit(self.flow_only(dataset=DEV_SITE)).run()
        rows = [r for r in self.rows() if r["kind"] == "Runtime value"]
        self.assertTrue(rows)
        self.assertIn("couldn't be read", rows[0]["detail"])

    def test_list_url_is_read(self):
        root = self.tmp / "ListUrl"
        root.mkdir()
        (root / "a.js").write_text(f'open("{DEV_SITE}/Lists/NewList/AllItems.aspx");\n')
        self.audit(root).run()
        self.assertEqual(self.row("List", "New List")["status"], eda.NOT_IN_PRODUCTION)

    def test_filter_and_orderby_columns_are_read(self):
        self.audit(self.flow_only(**{
            "dataset": DEV_SITE, "table": DEV_LIST,
            "$filter": "OnlyDevUsed eq '@{variables('x')}' and Status ne 'Closed'", "$orderby": "OnlyDevUnused desc",
        })).run()
        self.assertEqual(self.row("Column", "OnlyDevUsed")["status"], eda.NOT_IN_PRODUCTION)
        self.assertEqual(self.row("Column", "OnlyDevUnused")["status"], eda.NOT_IN_PRODUCTION)

    def test_graph_permission_error_says_what_token_is_needed(self):
        self.cloud.fail["sites/contoso.sharepoint.com:/sites/app"] = 403
        self.audit().run()
        self.assertIn("Sites.Read.All", self.row("Site", PROD_SITE)["detail"])

    def test_graph_failure_is_unknown_not_clean(self):
        self.cloud.fail["sites/contoso.sharepoint.com:/sites/app"] = 403
        self.audit().run()
        row = self.row("Site", PROD_SITE)
        self.assertEqual(row["status"], eda.UNKNOWN)
        self.assertIn("403", row["detail"])

    def test_canvas_package_is_read(self):
        root = self.tmp / "Canvas"
        root.mkdir()
        with zipfile.ZipFile(root / "App.msapp", "w") as archive:
            archive.writestr("DataSources/Sp.json", json.dumps([{"DatasetName": DEV_SITE, "TableName": DEV_ONLY_LIST}]))
        self.audit(root).run()
        self.assertEqual(self.row("Site", PROD_SITE)["hardcoded"], "Yes")


class FunctionTests(Harness):
    def test_function_in_production_with_a_route_parameter(self):
        self.audit().run()
        row = self.row("Function", "/api/orders/123")
        self.assertEqual(row["status"], eda.IN_PRODUCTION)
        self.assertEqual(row["hardcoded"], "Yes")

    def test_function_only_in_dev(self):
        self.audit().run()
        self.assertEqual(self.row("Function", "/api/newthing")["status"], eda.NOT_IN_PRODUCTION)

    def test_disabled_function_is_not_in_production(self):
        self.audit().run()
        row = self.row("Function", "/api/paused")
        self.assertEqual(row["status"], eda.NOT_IN_PRODUCTION)
        self.assertIn("disabled", row["detail"])

    def test_route_found_nowhere_is_unknown(self):
        self.audit().run()
        self.assertEqual(self.row("Function", "/api/nowhere")["status"], eda.UNKNOWN)

    def test_unmapped_host_is_unknown(self):
        self.audit().run()
        self.assertEqual(self.row("Function", "unmapped.azurewebsites.net/api/x")["status"], eda.UNKNOWN)

    def test_custom_domain_alias_maps_to_the_app(self):
        self.audit().run()
        self.assertEqual(self.row("Function", "/api/orders/9")["status"], eda.IN_PRODUCTION)

    def test_custom_connector_paths_are_read(self):
        self.audit().run()
        self.assertEqual(self.row("Function", "/api/fromconnector")["status"], eda.UNKNOWN)
        self.assertEqual(self.row("Function", "/api/orders/{id}")["status"], eda.IN_PRODUCTION)

    def test_unreferenced_dev_functions_ship_with_the_app(self):
        self.audit().run()
        row = self.row("Function", "OtherProject")
        self.assertEqual(row["status"], eda.DEV_ONLY)
        self.assertIn("deploys as a whole", row["detail"])
        self.assertEqual(self.row("Function", "TimerJob")["status"], eda.DEV_ONLY)

    def test_referenced_dev_function_is_not_also_dev_only(self):
        self.audit().run()
        self.assertNotIn("NewThing", self.names("Function"))
        self.assertNotIn("GetOrder", self.names("Function"))

    def test_missing_production_app(self):
        del self.cloud.functions["func-prod"]
        self.audit().run()
        self.assertEqual(self.row("Function app", "func-prod")["status"], eda.NOT_IN_PRODUCTION)

    def test_azure_failure_is_unknown_not_clean(self):
        self.cloud.fail["/sites/func-prod/functions"] = 500
        self.audit().run()
        row = self.row("Function", "/api/newthing")
        self.assertEqual(row["status"], eda.UNKNOWN)
        self.assertIn("500", row["detail"])

    def one_file(self, text):
        root = self.tmp / "OneFile"
        shutil.rmtree(root, True)
        root.mkdir()
        (root / "a.js").write_text(text)
        return root

    def test_a_route_set_at_runtime_is_unknown_and_matches_nothing(self):
        self.audit(self.one_file('fetch("https://func-dev.azurewebsites.net/api/@{variables(\'fn\')}");\n')).run()
        self.assertEqual(self.row("Function", "/api/...")["status"], eda.UNKNOWN)
        self.assertEqual(self.row("Function", "OtherProject")["status"], eda.DEV_ONLY)

    def test_an_unreachable_azure_app_leaves_a_row(self):
        self.cloud.fail["/sites/func-prod/functions"] = 403
        self.audit(self.one_file("nothing here")).run()
        self.assertEqual(self.row("Function app", "func-prod")["status"], eda.UNKNOWN)

    def test_a_missing_dev_app_leaves_a_row(self):
        del self.cloud.functions["func-dev"]
        self.audit(self.one_file("nothing here")).run()
        self.assertEqual(self.row("Function app", "func-dev")["status"], eda.UNKNOWN)

    def test_an_empty_dev_app_leaves_a_row(self):
        self.cloud.functions["func-dev"] = []
        self.audit(self.one_file("nothing here")).run()
        self.assertEqual(self.row("Function app", "func-dev")["status"], eda.UNKNOWN)

    def test_every_form_of_function_key_is_flagged_and_never_stored(self):
        forms = [
            f"https://func-dev.azurewebsites.net/api/foo?code={FUNCTION_KEY}",
            f"https://func-dev.azurewebsites.net/api/foo?a=1&code={FUNCTION_KEY}",
            f"https://func-dev.azurewebsites.net/api/foo&code={FUNCTION_KEY}",
            f"https://func-dev.azurewebsites.net/api/foo&amp;code={FUNCTION_KEY}",
            f"https://func-dev.azurewebsites.net/api/foo%3Fcode%3D{FUNCTION_KEY}",
            f"https://func-dev.azurewebsites.net/api/foo%3Fa%3D1%26code%3D{FUNCTION_KEY}",
        ]
        for form in forms:
            shutil.rmtree(self.out, True)
            self.audit(self.one_file(f'fetch("{form}");\n')).run()
            rows = [r for r in self.rows() if r["kind"] == "Function key in source"]
            self.assertEqual(len(rows), 1, form)
            for path in self.out.iterdir():
                self.assertNotIn(FUNCTION_KEY, path.read_text(encoding="utf-8-sig"), form)

    def test_a_key_in_a_header_is_flagged_and_never_stored(self):
        self.audit(self.one_file(f'headers["x-functions-key"] = "{FUNCTION_KEY}abcdef";\n')).run()
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "Function key in source"]), 1)
        self.assertNotIn(FUNCTION_KEY, (self.out / "external-dependencies.csv").read_text(encoding="utf-8-sig"))

    def test_a_key_reference_in_a_header_is_not_flagged(self):
        self.audit(self.one_file('headers["x-functions-key"] = "@parameters(\'key\')";\n')).run()
        self.assertEqual([r for r in self.rows() if r["kind"] == "Function key in source"], [])

    def test_function_key_location_is_reported_without_its_value(self):
        self.audit().run()
        rows = [r for r in self.rows() if r["kind"] == "Function key in source"]
        self.assertEqual(len(rows), 1)
        self.assertIn("a.js:1", rows[0]["name"])


class SafetyTests(Harness):
    def test_only_get_requests_are_sent(self):
        self.audit().run()
        self.assertTrue(self.cloud.log)
        self.assertEqual({entry[0] for entry in self.cloud.log}, {"GET"})

    def test_no_secret_reaches_any_output(self):
        self.audit().run()
        for path in self.out.iterdir():
            text = path.read_text(encoding="utf-8-sig")
            for secret in (TOKEN, FUNCTION_KEY, LOCAL_KEY):
                self.assertNotIn(secret, text, path.name)

    def test_function_key_is_never_sent_to_any_service(self):
        self.audit().run()
        self.assertNotIn(FUNCTION_KEY, json.dumps(self.cloud.log))

    def test_local_settings_and_docs_are_skipped(self):
        self.audit().run()
        text = json.dumps(self.rows())
        self.assertNotIn("/api/local", text)
        self.assertNotIn("readme-only", text)
        self.assertNotIn("local.settings.json", text)

    def test_map_rejects_the_same_site_twice(self):
        path = self.tmp / "m.json"
        path.write_text(json.dumps({"sharepoint_sites": [{"dev": PROD_SITE, "prod": PROD_SITE}]}))
        with self.assertRaisesRegex(AuditError, "same site"):
            eda.load_map(path)

    def test_map_rejects_the_same_app_twice(self):
        path = self.tmp / "m.json"
        app = {"subscription": SUB, "resource_group": "rg", "name": "func"}
        path.write_text(json.dumps({"function_apps": [{"dev": app, "prod": app}]}))
        with self.assertRaisesRegex(AuditError, "same app"):
            eda.load_map(path)

    def test_map_requires_full_app_details(self):
        path = self.tmp / "m.json"
        path.write_text(json.dumps({"function_apps": [{"dev": {"name": "a"}, "prod": {"name": "b"}}]}))
        with self.assertRaisesRegex(AuditError, "subscription"):
            eda.load_map(path)

    def test_failed_token_command_stops_the_audit(self):
        provider = eda.token_from_command("definitely-not-a-real-command --flag")
        with self.assertRaises(AuditError):
            provider("https://graph.microsoft.com")

    def test_token_command_does_not_use_a_shell(self):
        marker = self.tmp / "marker"
        provider = eda.token_from_command(f"echo hello; touch {marker}")
        provider("x")
        self.assertFalse(marker.exists())


class OutputTests(Harness):
    def test_clean_run_exits_zero(self):
        root = self.tmp / "Clean"
        root.mkdir()
        (root / "a.js").write_text('fetch("https://example.com/not-relevant");')
        self.app_pairs = []
        self.assertEqual(self.audit(root).run(), 0)

    def test_explanations_survive_a_rerun_and_clear_false_positives(self):
        source = self.source()
        self.audit(source).run()
        rows = self.rows()
        for row in rows:
            row["explanation"] = "False positive: reviewed by the team."
        with (self.out / "external-dependencies.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=eda.CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        self.assertEqual(self.audit(source).run(), 0)
        self.assertTrue(all(r["explanation"].startswith("False positive") for r in self.rows()))

    def test_zero_files_is_not_clean(self):
        root = self.tmp / "Nothing"
        root.mkdir()
        self.app_pairs = []
        audit = self.audit(root)
        self.assertEqual(audit.run(), 3)
        self.assertTrue(any("read no files" in blocker for blocker in audit.gate_blockers()))

    def test_unreadable_package_becomes_a_row(self):
        root = self.tmp / "BadPackage"
        root.mkdir()
        (root / "Bad.msapp").write_bytes(b"not a zip")
        self.app_pairs = []
        self.audit(root).run()
        self.assertEqual(self.row("File not searched", "BadPackage/Bad.msapp")["status"], eda.UNKNOWN)

    def test_explanations_survive_a_spreadsheet_save(self):
        source = self.source()
        self.audit(source).run()
        rows = self.rows()
        rows[0]["explanation"] = "Revisado: sin función."
        with (self.out / "external-dependencies.csv").open("w", newline="", encoding="cp1252") as handle:
            writer = csv.DictWriter(handle, fieldnames=eda.CSV_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        self.audit(source).run()
        self.assertTrue(any(r["explanation"] == "Revisado: sin función." for r in self.rows()))

    def test_map_is_written_with_a_diagram(self):
        self.audit().run()
        text = (self.out / "external-map.md").read_text()
        self.assertIn("```mermaid", text)
        self.assertIn(":::missing", text)
        self.assertIn(":::devonly", text)
        self.assertIn("Hardcoded addresses", text)


class KeyLeakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        self.root.mkdir()

    def discover(self):
        discovery = eda.Discovery([])
        discovery.scan([self.root])
        return discovery

    def assert_key_never_stored(self, discovery):
        for (host, route) in discovery.functions:
            self.assertNotIn(FUNCTION_KEY.lower(), route.lower())
        self.assertTrue(discovery.key_locations)

    def test_a_key_in_a_custom_connector_path_is_never_stored(self):
        (self.root / "c_openapidefinition.json").write_text(json.dumps({
            "swagger": "2.0", "host": "func-dev.azurewebsites.net", "basePath": "/api",
            "paths": {f"/orders?code={FUNCTION_KEY}": {}},
        }))
        self.assert_key_never_stored(self.discover())

    def test_a_key_behind_double_encoding_is_never_stored(self):
        (self.root / "flow.json").write_text(json.dumps({
            "uri": f"https://func-dev.azurewebsites.net/api/orders%253Fcode%253D{FUNCTION_KEY}",
        }))
        self.assert_key_never_stored(self.discover())

    def test_functions_in_other_clouds_are_recognized(self):
        (self.root / "a.js").write_text('fetch("https://x.azurewebsites.us/api/a");\nfetch("https://y.chinacloudsites.cn/api/b");\n')
        hosts = {host for host, _route in self.discover().functions}
        self.assertEqual(hosts, {"x.azurewebsites.us", "y.chinacloudsites.cn"})


class ClosingFindingsTests(unittest.TestCase):
    def finding(self, **values):
        values.setdefault("status", eda.NOT_IN_PRODUCTION)
        return eda.Finding("SharePoint", "List", "Orders", **values)

    def test_accepted_closes_what_ships_in_the_release(self):
        self.assertFalse(self.finding(explanation="Accepted: the release script adds it.").is_open)

    def test_accepted_cannot_close_a_hardcoded_address(self):
        self.assertTrue(self.finding(hardcoded="Yes", explanation="Accepted: fine.").is_open)

    def test_a_false_positive_closes_either(self):
        self.assertFalse(self.finding(hardcoded="Yes", explanation="False positive: a sample URL.").is_open)

    def test_another_explanation_leaves_it_open(self):
        self.assertTrue(self.finding(explanation="Checked with the owner.").is_open)


class CommandLineTests(unittest.TestCase):
    def test_a_plain_http_address_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = eda.main(["--map", "m.json", "--scan-dir", ".", "--graph-url", "http://graph.example"])
        self.assertEqual(code, 2)
        self.assertIn("must be an https", err.getvalue())

    def test_nothing_to_scan_is_a_bad_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(eda.main(["--map", "m.json"]), 2)

    def test_skipped_submodules_become_a_finding(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        config = eda.Config(tmp / "m.json", [], tmp / "audit", [], [], [{"name": "plugins", "url": "https://x", "submodules_skipped": True}])
        client = ReadOnlyClient("http://127.0.0.1:9", lambda _: TOKEN, "Test", api_root="/")
        audit = eda.ExternalAudit(config, client, client, log=lambda _: None)
        audit.add_scan_findings()
        finding = next(iter(audit.findings.values()))
        self.assertEqual((finding.kind, finding.name, finding.status), ("Submodules not searched", "plugins", eda.UNKNOWN))


class HelperTests(unittest.TestCase):
    def test_route_matching(self):
        self.assertTrue(eda.route_matches("/api/orders/123", "/api/orders/{id}"))
        self.assertTrue(eda.route_matches("/API/Orders/{x}", "/api/orders/{id}"))
        self.assertFalse(eda.route_matches("/api/orders", "/api/orders/{id}"))
        self.assertFalse(eda.route_matches("/api/orders/1/x", "/api/orders/{id}"))
        self.assertFalse(eda.route_matches("/api/other", "/api/orders"))

    def test_site_url_stops_at_the_site(self):
        match = eda.SITE_RE.search('"https://Contoso.sharepoint.com/sites/App/Lists/Orders/AllItems.aspx"')
        self.assertEqual(eda.normalize_site(match.group(1), match.group(2)), PROD_SITE)

    def test_site_url_for_the_root_site(self):
        match = eda.SITE_RE.search('"https://contoso.sharepoint.com/Shared Documents"')
        self.assertEqual(eda.normalize_site(match.group(1), match.group(2)), "https://contoso.sharepoint.com")


if __name__ == "__main__":
    unittest.main()
