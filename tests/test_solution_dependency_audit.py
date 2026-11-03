"""Tests for solution_dependency_audit.py, run against a local fake Dataverse Web API.

The fake follows Microsoft's documented API shapes. It proves the script's logic and its
read-only behaviour, not how a live tenant responds. Run: python -m unittest -v
"""
import contextlib
import csv
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse
import uuid
import zipfile
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import solution_dependency_audit as sda  # noqa: E402

TOKEN = "secret-test-token"


def g(name):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


class FakeEnvironment:
    """A small OData emulator holding tables, metadata and function results."""

    def __init__(self, org):
        self.org = g(org)
        self.org_name = org
        self.log = []
        self.tables = {"organizations": [{"organizationid": self.org, "name": org}]}
        self.keys = {"organizations": "organizationid"}
        self.entities = {}
        self.relationships = {}
        self.optionsets = {}
        self.missing = {}
        self.required = {}
        self.dependents = {}
        self.fail = {}
        self.page_size = {}
        self.throttle_next = 0
        self.expire_token_next = False
        self.bad_next_link = False
        self.redirect_next = False

    def table(self, entity_set, key, rows):
        self.keys[entity_set] = key
        self.tables.setdefault(entity_set, []).extend(rows)

    def entity(self, logical, metadata_id=None, entity_set=None, columns=(), primary="name", primary_id=None):
        metadata_id = metadata_id or g(f"entity:{logical}")
        self.entities[logical] = {
            "MetadataId": metadata_id, "LogicalName": logical,
            "EntitySetName": entity_set or f"{logical}s",
            "PrimaryIdAttribute": primary_id or f"{logical}id", "PrimaryNameAttribute": primary,
            "IsCustomEntity": "_" in logical, "IsManaged": False,
            "Attributes": [{"MetadataId": g(f"column:{logical}.{c}"), "LogicalName": c} for c in columns],
        }
        return metadata_id


def start_server(env):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload, headers=None):
            body = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _not_allowed(self):
            env.log.append((self.command, self.path))
            self._send(405, {"error": {"message": "writes are not allowed"}})

        do_POST = do_PUT = do_PATCH = do_DELETE = _not_allowed

        def do_GET(self):
            env.log.append(("GET", self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"error": {"message": "bad token"}})
            if env.expire_token_next:
                env.expire_token_next = False
                return self._send(401, {"error": {"message": "expired"}})
            if env.throttle_next:
                env.throttle_next -= 1
                return self._send(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"})
            if env.redirect_next:
                env.redirect_next = False
                return self._send(302, None, {"Location": "http://127.0.0.1:9/stolen"})
            parts = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parts.path).split("/api/data/v9.2/", 1)[1]
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            for fragment, status in env.fail.items():
                if fragment in urllib.parse.unquote(self.path):
                    return self._send(status, {"error": {"message": f"forced {status}"}})
            try:
                status, payload = route(env, path, query, self.server)
            except KeyError as exc:
                status, payload = 404, {"error": {"message": f"not found: {exc}"}}
            self._send(status, payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def compare(row, column, value):
    actual = row.get(column)
    if value in ("true", "false"):
        return actual is (value == "true")
    if re.fullmatch(r"-?\d+", value):
        return str(actual) == value
    return str(actual).lower() == value.strip("'").replace("''", "'").lower()


def run_filter(rows, expression):
    clauses = [re.fullmatch(r"(\S+) eq (.+)", part) for part in expression.split(" and ")]
    if not all(clauses):
        raise ValueError(expression)
    return [r for r in rows if all(compare(r, c.group(1), c.group(2)) for c in clauses)]


def select(row, query):
    if "$select" not in query:
        return row
    return {c: row.get(c) for c in query["$select"].split(",")}


def route(env, path, query, server):
    if path == "WhoAmI":
        return 200, {"OrganizationId": env.org, "UserId": g("user")}
    match = re.fullmatch(r"RetrieveMissingDependencies\(SolutionUniqueName='(.+)'\)", path)
    if match:
        return 200, {"value": env.missing.get(match.group(1), [])}
    match = re.fullmatch(r"Retrieve(Required|Dependent)Components\(ObjectId=@o,ComponentType=@t\)", path)
    if match:
        source = env.required if match.group(1) == "Required" else env.dependents
        return 200, {"value": source.get((query["@o"], int(query["@t"])), [])}
    if path == "EntityDefinitions":
        rows = list(env.entities.values())
        if "$filter" in query:
            rows = run_filter(rows, query["$filter"])
        return 200, {"value": [{"MetadataId": row["MetadataId"]} for row in rows]}
    match = re.fullmatch(r"EntityDefinitions\((?:LogicalName='(.+?)'|([0-9a-f-]+))\)(?:/(Attributes|Keys)\((?:LogicalName='(.+?)'|([0-9a-f-]+))\))?", path)
    if match:
        logical, entity_id, child, child_name, child_id = match.groups()
        entity = env.entities[logical] if logical else next(e for e in env.entities.values() if e["MetadataId"] == entity_id)
        if child:
            for attribute in entity["Attributes"]:
                if attribute["LogicalName"] == child_name or attribute["MetadataId"] == child_id:
                    return 200, attribute
            raise KeyError(child_name or child_id)
        result = dict(entity)
        if "Attributes" not in query.get("$expand", ""):
            result.pop("Attributes")
        return 200, result
    match = re.fullmatch(r"(RelationshipDefinitions|GlobalOptionSetDefinitions)\((?:(?:SchemaName|Name)='(.+?)'|([0-9a-f-]+))\)", path)
    if match:
        kind, name, item_id = match.groups()
        items = env.relationships if kind == "RelationshipDefinitions" else env.optionsets
        return 200, items[name] if name else next(i for i in items.values() if i["MetadataId"] == item_id)
    match = re.fullmatch(r"([a-z]+)\(([0-9a-f-]+)\)", path)
    if match:
        entity_set, record_id = match.groups()
        for row in env.tables[entity_set]:
            if row[env.keys[entity_set]] == record_id:
                return 200, select(row, query)
        raise KeyError(record_id)
    if path in env.tables:
        rows = env.tables[path]
        if "$filter" in query:
            try:
                rows = run_filter(rows, query["$filter"])
            except ValueError as exc:
                return 400, {"error": {"message": f"bad filter {exc}"}}
        if "$top" in query:
            rows = rows[: int(query["$top"])]
        size = env.page_size.get(path)
        offset = int(query.get("offset", 0))
        if size and offset + size < len(rows):
            base = f"http://127.0.0.1:{server.server_address[1]}"
            if env.bad_next_link:
                base = "http://evil.example.com"
            next_query = dict(query, offset=offset + size)
            next_url = f"{base}/api/data/v9.2/{path}?{urllib.parse.urlencode(next_query)}"
            return 200, {"value": [select(r, query) for r in rows[offset:offset + size]], "@odata.nextLink": next_url}
        return 200, {"value": [select(r, query) for r in rows[offset:]]}
    raise KeyError(path)


def dep(dependent, dependent_type, required, required_type, kind=2, parent=None, base=None, introduced=None):
    row = {
        "dependentcomponentobjectid": dependent, "dependentcomponenttype": dependent_type,
        "requiredcomponentobjectid": required, "requiredcomponenttype": required_type, "dependencytype": kind,
    }
    if parent:
        row["requiredcomponentparentid"] = parent
    if base:
        row["requiredcomponentbasesolutionid"] = base
    if introduced is not None:
        row["requiredcomponentintroducedversion"] = introduced
    return row


class Fixture:
    """A DEV and a production environment covering every status the audit can report."""

    def __init__(self):
        dev = self.dev = FakeEnvironment("Contoso DEV")
        prod = self.prod = FakeEnvironment("Contoso PROD")
        self.sol = {name: g(f"solution:{name}") for name in ("Target", "ProjB", "ProjC", "Default", "Active", "MsBase")}
        dev.table("solutions", "solutionid", [
            {"solutionid": self.sol["Target"], "uniquename": "Target", "ismanaged": False, "version": "1.0.0.0", "isvisible": True},
            {"solutionid": self.sol["ProjB"], "uniquename": "ProjB", "ismanaged": False, "version": "0.1.0.0", "isvisible": True},
            {"solutionid": self.sol["ProjC"], "uniquename": "ProjC", "ismanaged": False, "version": "0.1.0.0", "isvisible": True},
            {"solutionid": self.sol["Default"], "uniquename": "Default", "ismanaged": False, "version": "1.0", "isvisible": True},
            {"solutionid": self.sol["Active"], "uniquename": "Active", "ismanaged": False, "version": "1.0", "isvisible": True},
            {"solutionid": self.sol["MsBase"], "uniquename": "MsBase", "ismanaged": True, "version": "2.0.0.0", "isvisible": True},
        ])
        prod.table("solutions", "solutionid", [
            {"solutionid": g("prod:msbase"), "uniquename": "MsBase", "ismanaged": True, "version": "1.0.0.0", "isvisible": True},
        ])
        self.account = dev.entity("account", entity_set="accounts", columns=("shared_col", "b_col", "b_noparent", "shared_prod_col"))
        self.b_tbl = dev.entity("b_tbl", columns=("b_viatable", "b_hidden", "b_lookup"))
        self.t_tbl = dev.entity("t_tbl", columns=("t_col",))
        self.c_other = dev.entity("c_other_tbl", columns=())
        dev.entity("connectionreference", entity_set="connectionreferences", primary="connectionreferencedisplayname")
        prod.entity("account", metadata_id=g("prod:account"), entity_set="accounts", columns=("shared_col", "shared_prod_col"))
        prod.entity("connectionreference", entity_set="connectionreferences", primary="connectionreferencedisplayname")
        self.b_col = g("column:account.b_col")
        self.shared_col = g("column:account.shared_col")
        self.b_noparent = g("column:account.b_noparent")
        self.b_viatable = g("column:b_tbl.b_viatable")
        self.shared_prod_col = g("column:account.shared_prod_col")

        self.form, self.flow, self.view = g("form"), g("flow"), g("view")
        self.ms_flow, self.child_flow = g("msflow"), g("childflow")
        dev.table("systemforms", "formid", [{"formid": self.form, "name": "=Main form"}])
        dev.table("workflows", "workflowid", [
            {"workflowid": self.flow, "name": "Target flow"},
            {"workflowid": self.ms_flow, "name": "Microsoft flow"},
            {"workflowid": self.child_flow, "name": "Child flow"},
        ])
        dev.table("savedqueries", "savedqueryid", [{"savedqueryid": self.view, "name": "Active items"}])
        self.web = g("web")
        dev.table("webresourceset", "webresourceid", [{"webresourceid": self.web, "name": "c_/scripts/helpers.js"}])
        self.var = g("envvar")
        dev.table("environmentvariabledefinitions", "environmentvariabledefinitionid", [
            {"environmentvariabledefinitionid": self.var, "schemaname": "d_setting"},
        ])
        self.choice = g("choice")
        dev.optionsets["b_choice"] = {"MetadataId": self.choice, "Name": "b_choice"}
        self.conn = g("connref")
        dev.table("solutioncomponentdefinitions", "solutioncomponentdefinitionid", [
            {"solutioncomponentdefinitionid": g("def"), "solutioncomponenttype": 10500, "primaryentityname": "connectionreference", "name": "connectionreference"},
        ])
        dev.table("connectionreferences", "connectionreferenceid", [
            {"connectionreferenceid": self.conn, "connectionreferencelogicalname": "c_conn", "connectionreferencedisplayname": "C connection"},
        ])
        prod.table("workflows", "workflowid", [])
        prod.table("connectionreferences", "connectionreferenceid", [])
        prod.table("environmentvariabledefinitions", "environmentvariabledefinitionid", [])

        def component(solution, object_id, ctype, behavior=None, root=None):
            return {
                "solutioncomponentid": g(f"sc:{solution}:{object_id}"), "_solutionid_value": self.sol[solution],
                "objectid": object_id, "componenttype": ctype, "rootcomponentbehavior": behavior,
                "rootsolutioncomponentid": root,
            }

        account_row = component("ProjB", self.account, 1, behavior=1)
        dev.table("solutioncomponents", "solutioncomponentid", [
            account_row,
            component("Target", self.t_tbl, 1, behavior=0),
            component("Target", self.form, 60),
            component("Target", self.flow, 29),
            component("Target", self.view, 26),
            component("ProjB", self.b_tbl, 1, behavior=0),
            component("ProjB", self.b_col, 2, root=account_row["solutioncomponentid"]),
            component("ProjB", self.b_noparent, 2, root=account_row["solutioncomponentid"]),
            component("ProjB", self.shared_prod_col, 2, root=account_row["solutioncomponentid"]),
            component("ProjB", self.choice, 9),
            component("ProjC", self.c_other, 1, behavior=1),
            component("ProjC", self.web, 61),
            component("ProjC", self.conn, 10500),
            component("ProjC", self.child_flow, 29),
            component("MsBase", self.ms_flow, 29),
            component("Default", self.var, 380),
        ])
        dev.page_size["solutioncomponents"] = 4

        dev.missing["Target"] = [
            dep(self.form, 60, self.b_col, 2, parent=self.account),
            dep(self.form, 60, self.shared_col, 2, parent=self.account),
            dep(self.form, 60, self.b_noparent, 2),
            dep(self.form, 60, self.b_viatable, 2, parent=self.b_tbl),
            dep(self.flow, 29, self.b_tbl, 1, kind=4),
            dep(self.view, 26, self.web, 61),
            dep(self.flow, 29, self.ms_flow, 29, base=self.sol["MsBase"], introduced=2.0),
            dep(self.flow, 29, g("mystery"), 999),
            dep(self.flow, 29, self.conn, 10500),
            dep(self.flow, 29, self.var, 380),
        ]
        dev.required[(self.b_tbl, 1)] = [
            dep(self.b_tbl, 1, self.choice, 9, kind=1),
            dep(self.b_tbl, 1, self.account, 1, kind=1),
        ]
        dev.required[(self.choice, 9)] = [dep(self.choice, 9, self.b_tbl, 1)]
        dev.dependents[(self.b_col, 2)] = [dep(self.view, 26, self.b_col, 2), dep(self.form, 60, self.b_col, 2)]
        prod.fail["webresourceset"] = 500


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = Fixture()
        self.dev_server, self.dev_url = start_server(self.fx.dev)
        self.prod_server, self.prod_url = start_server(self.fx.prod)
        for server in (self.dev_server, self.prod_server):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.out = self.tmp / "audit"

    def client(self, url, label, **kwargs):
        kwargs.setdefault("sleep", lambda _: None)
        return sda.ReadOnlyClient(url, lambda _: TOKEN, label, **kwargs)

    def audit(self, **config):
        config.setdefault("targets", ["Target"])
        config.setdefault("out", self.out)
        return sda.Audit(
            sda.Config(**config), self.client(self.dev_url, "DEV"), self.client(self.prod_url, "PROD"),
            log=lambda _: None,
        )

    def rows(self):
        with (self.out / "dependencies.csv").open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))

    def row(self, required_name, dependent_name=None):
        for row in self.rows():
            if row["required_name"] == required_name and (dependent_name is None or row["dependent_name"] == dependent_name):
                return row
        self.fail(f"no row for {required_name}: {[r['required_name'] for r in self.rows()]}")

    def source_tree(self):
        root = self.tmp / "Target"
        shutil.rmtree(root, True)
        (root / "Workflows").mkdir(parents=True)
        (root / "Other").mkdir()
        (root / "WebResources").mkdir()
        (root / "Workflows" / "flow.json").write_text(
            '{"select": "b_hidden,_b_lookup_value", "child": "%s", "own": "t_col", "word": "account"}\n' % self.fx.child_flow
        )
        (root / "WebResources" / "a.js").write_text(
            'formContext.getAttribute("shared_prod_col");\nimport "../scripts/helpers.js";\n'
        )
        (root / "Other" / "Solution.xml").write_text('<MissingDependency schemaName="b_hidden" other="c_conn"/>')
        (root / "Plugin.dll").write_bytes(b"MZ\x00\x00b_viatable")
        (root / "notes.txt").write_bytes('uses c_conn here'.encode("utf-16"))
        with zipfile.ZipFile(root / "App.msapp", "w") as archive:
            archive.writestr("Src/App.pa.yaml", "Items: c_other_tbls\n")
        site = self.tmp / "portal"
        shutil.rmtree(site, True)
        (site / "table-permissions").mkdir(parents=True)
        (site / "table-permissions" / "b.yml").write_text("entitylogicalname: b_tbl\n")
        (site / "bin").mkdir()
        (site / "bin" / "skip.txt").write_text("c_conn")
        return [root, site]

    def stage_file(self):
        path = self.tmp / "stage.json"
        path.write_text(json.dumps({"StageSolutionResults": {
            "StageSolutionStatus": "Passed",
            "SolutionDetails": {"SolutionUniqueName": "Target", "SolutionVersion": "1.0.0.0"},
            "SolutionValidationResults": [{"Message": "A validation warning", "SolutionValidationResultType": "Warning"}],
            "MissingDependencies": [
                {"RequiredComponentId": self.fx.b_col, "RequiredComponentSchemaName": "b_col", "RequiredComponentType": "Attribute",
                 "DependentComponentId": self.fx.form, "DependentComponentSchemaName": "Main form", "DependentComponentType": "SystemForm"},
                {"RequiredComponentId": self.fx.shared_col, "RequiredComponentSchemaName": "shared_col", "RequiredComponentType": "Attribute"},
                {"RequiredComponentId": self.fx.child_flow, "RequiredComponentSchemaName": "Child flow", "RequiredComponentType": "Workflow",
                 "DependentComponentSchemaName": "Target flow", "DependentComponentType": "Workflow"},
                {"RequiredComponentId": g("nowhere"), "RequiredComponentSchemaName": "ghost", "RequiredComponentParentSchemaName": "account",
                 "RequiredComponentType": "Attribute", "RequiredSolutionName": "Mystery"},
            ],
        }}))
        return path


class GraphTests(Harness):
    def test_statuses_cover_every_case(self):
        code = self.audit(skip_impact=True).run()
        self.assertEqual(code, 3)
        expected = {
            "account.b_col": sda.IN_FLIGHT,
            "account.shared_col": sda.IN_PRODUCTION,
            "account.b_noparent": sda.IN_FLIGHT,
            "b_tbl.b_viatable": sda.IN_FLIGHT,
            "b_tbl": sda.IN_FLIGHT,
            "b_choice": sda.IN_FLIGHT,
            "account": sda.IN_PRODUCTION,
            "c_/scripts/helpers.js": sda.UNKNOWN,
            "Microsoft flow": sda.VERSION_TOO_OLD,
            "c_conn": sda.IN_FLIGHT,
            "d_setting": sda.IN_FLIGHT,
        }
        for name, status in expected.items():
            self.assertEqual(self.row(name)["status"], status, name)
        self.assertTrue(any(r["required_name"].startswith("Type 999") and r["status"] == sda.UNKNOWN for r in self.rows()))

    def test_owner_details(self):
        self.audit(skip_impact=True).run()
        self.assertEqual(self.row("b_tbl.b_viatable")["owning_solutions"], "ProjB (via table)")
        self.assertEqual(self.row("c_conn")["owning_solutions"], "ProjC")
        self.assertIn("Default only", self.row("d_setting")["production_detail"])
        self.assertIn("Production has MsBase 1.0.0.0; DEV has 2.0.0.0", self.row("Microsoft flow")["production_detail"])
        self.assertEqual(self.row("c_conn")["required_type"], "connectionreference")

    def test_chain_walk_stops_at_production_and_survives_cycles(self):
        self.audit(skip_impact=True).run()
        choice = self.row("b_choice", "b_tbl")
        self.assertEqual(choice["depth"], "2")
        self.assertEqual(self.row("account", "b_tbl")["status"], sda.IN_PRODUCTION)
        self.assertEqual(self.row("b_tbl", "b_choice")["depth"], "3")

    def test_chain_depth_limit_is_reported(self):
        audit = self.audit(skip_impact=True, max_depth=1)
        audit.run()
        self.assertTrue(any("Stopped walking" in blocker for blocker in audit.gate_blockers()))

    def test_dependency_type_labels(self):
        self.audit(skip_impact=True).run()
        self.assertEqual(self.row("b_tbl", "Target flow")["dependency_type"], "Unpublished")

    def test_impact(self):
        self.audit().run()
        self.assertEqual(self.row("account.b_col")["used_by_count"], "2")
        impact = (self.out / "impact.csv").read_text(encoding="utf-8-sig")
        self.assertIn("Active items", impact)

    def test_failed_production_check_is_unknown_not_clean(self):
        self.audit(skip_impact=True).run()
        row = self.row("c_/scripts/helpers.js")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("forced 500", row["production_detail"])

    def clean_run(self, **stage):
        self.fx.dev.missing["Target"] = [dep(self.fx.form, 60, self.fx.shared_col, 2, parent=self.fx.account)]
        root = self.tmp / "clean"
        root.mkdir(exist_ok=True)
        (root / "a.txt").write_text("nothing relevant")
        results = {"StageSolutionStatus": "Passed", "MissingDependencies": [],
                   "SolutionDetails": {"SolutionUniqueName": "Target", "SolutionVersion": "1.0.0.0"}}
        results.update(stage)
        path = self.tmp / "clean-stage.json"
        path.write_text(json.dumps({"StageSolutionResults": results}))
        return self.audit(scan_dirs=[root], stage_jsons=[path], skip_impact=True)

    def test_clean_target_exits_zero(self):
        self.assertEqual(self.clean_run().run(), 0)

    def test_numeric_passed_status_is_accepted(self):
        self.assertEqual(self.clean_run(StageSolutionStatus=1).run(), 0)

    def test_no_evidence_is_not_clean(self):
        self.fx.dev.missing["Target"] = []
        self.assertEqual(self.audit(skip_impact=True).run(), 3)

    def test_failed_dry_run_is_not_clean(self):
        audit = self.clean_run(StageSolutionStatus="Failed")
        self.assertEqual(audit.run(), 3)
        self.assertTrue(any("didn't pass" in blocker for blocker in audit.gate_blockers()))

    def test_unexpected_dry_run_status_is_not_clean(self):
        self.assertEqual(self.clean_run(StageSolutionStatus="Completed").run(), 3)

    def test_dry_run_of_another_solution_is_not_clean(self):
        audit = self.clean_run(SolutionDetails={"SolutionUniqueName": "Other", "SolutionVersion": "1.0.0.0"})
        self.assertEqual(audit.run(), 3)
        self.assertTrue(any("tested 'Other'" in blocker for blocker in audit.gate_blockers()))

    def test_dry_run_of_another_version_is_not_clean(self):
        audit = self.clean_run(SolutionDetails={"SolutionUniqueName": "Target", "SolutionVersion": "0.9.0.0"})
        self.assertEqual(audit.run(), 3)

    def test_dry_run_validation_error_is_not_clean(self):
        audit = self.clean_run(SolutionValidationResults=[{"Message": "Bad", "SolutionValidationResultType": "Error"}])
        self.assertEqual(audit.run(), 3)

    def test_dry_run_without_details_is_not_clean(self):
        self.assertEqual(self.clean_run(SolutionDetails=None).run(), 3)

    def test_failed_chain_walk_is_not_clean(self):
        self.fx.dev.fail["RetrieveRequiredComponents"] = 500
        audit = self.audit(skip_impact=True)
        audit.run()
        self.assertTrue(any("Could not walk" in blocker for blocker in audit.gate_blockers()))

    def test_response_without_rows_fails(self):
        with self.assertRaises(sda.ApiError):
            self.client(self.dev_url, "DEV").get_all("WhoAmI")

    def test_shared_component_is_reported(self):
        self.fx.dev.tables["solutioncomponents"].append({
            "solutioncomponentid": g("shared-row"), "_solutionid_value": self.fx.sol["ProjB"], "objectid": self.fx.form,
            "componenttype": 60, "rootcomponentbehavior": None, "rootsolutioncomponentid": None,
        })
        self.audit(skip_impact=True).run()
        row = self.row("'=Main form", "Target and ProjB")
        self.assertEqual(row["status"], sda.IN_FLIGHT)
        self.assertIn("exports ProjB's edits", row["production_detail"])

    def test_table_shell_in_two_solutions_is_not_reported(self):
        self.fx.dev.tables["solutioncomponents"].append({
            "solutioncomponentid": g("shell-row"), "_solutionid_value": self.fx.sol["Target"], "objectid": self.fx.account,
            "componenttype": 1, "rootcomponentbehavior": 1, "rootsolutioncomponentid": None,
        })
        self.audit(skip_impact=True).run()
        self.assertFalse(any(r["dependent_type"] == "Solutions" for r in self.rows()))

    def test_role_with_the_same_name_but_another_id_is_not_in_production(self):
        role = g("role")
        self.fx.dev.table("roles", "roleid", [{"roleid": role, "name": "Sales Manager"}])
        self.fx.prod.table("roles", "roleid", [{"roleid": g("other-role"), "name": "Sales Manager"}])
        self.fx.dev.tables["solutioncomponents"].append({
            "solutioncomponentid": g("role-row"), "_solutionid_value": self.fx.sol["ProjB"], "objectid": role,
            "componenttype": 20, "rootcomponentbehavior": None, "rootsolutioncomponentid": None,
        })
        self.fx.dev.missing["Target"].append(dep(self.fx.form, 60, role, 20))
        self.audit(skip_impact=True).run()
        self.assertEqual(self.row("Sales Manager")["status"], sda.IN_FLIGHT)


class SafetyTests(Harness):
    def test_only_get_requests_are_sent(self):
        self.audit(scan_dirs=self.source_tree(), stage_jsons=[self.stage_file()]).run()
        for env in (self.fx.dev, self.fx.prod):
            self.assertTrue(env.log)
            self.assertEqual({entry[0] for entry in env.log}, {"GET"})

    def test_token_never_reaches_output_files(self):
        self.audit(scan_dirs=self.source_tree(), stage_jsons=[self.stage_file()]).run()
        for path in self.out.iterdir():
            self.assertNotIn(TOKEN, path.read_text(encoding="utf-8-sig"), path.name)

    def test_client_has_no_write_methods(self):
        for name in ("post", "put", "patch", "delete", "request"):
            self.assertFalse(hasattr(sda.ReadOnlyClient, name), name)

    def test_next_link_to_another_host_is_refused(self):
        self.fx.dev.bad_next_link = True
        client = self.client(self.dev_url, "DEV")
        with self.assertRaises(sda.ApiError):
            client.get_all("solutioncomponents")
        self.assertFalse(any("evil" in str(entry[1]) for entry in self.fx.dev.log))

    def test_redirect_is_not_followed(self):
        self.fx.dev.redirect_next = True
        with self.assertRaises(sda.ApiError):
            self.client(self.dev_url, "DEV").get("WhoAmI")

    def test_retries_when_throttled(self):
        self.fx.dev.throttle_next = 2
        self.assertIn("OrganizationId", self.client(self.dev_url, "DEV").get("WhoAmI"))

    def test_gives_up_after_too_many_throttles(self):
        self.fx.dev.throttle_next = 50
        with self.assertRaises(sda.ApiError) as raised:
            self.client(self.dev_url, "DEV", retries=2).get("WhoAmI")
        self.assertEqual(raised.exception.status, 429)

    def test_expired_token_is_refreshed_once(self):
        calls = []

        def provider(_):
            calls.append(1)
            return TOKEN

        self.fx.dev.expire_token_next = True
        client = sda.ReadOnlyClient(self.dev_url, provider, "DEV", sleep=lambda _: None)
        self.assertIn("OrganizationId", client.get("WhoAmI"))
        self.assertEqual(len(calls), 2)

    def test_paging_reads_every_row(self):
        rows = self.client(self.dev_url, "DEV").get_all("solutioncomponents")
        self.assertEqual(len(rows), len(self.fx.dev.tables["solutioncomponents"]))

    def test_same_environment_is_refused(self):
        audit = sda.Audit(
            sda.Config(targets=["Target"], out=self.out), self.client(self.dev_url, "DEV"),
            self.client(self.dev_url + "/", "PROD"), log=lambda _: None,
        )
        with self.assertRaisesRegex(sda.AuditError, "same environment"):
            audit.run()

    def test_same_organization_under_another_url_is_refused(self):
        twin_server, twin_url = start_server(self.fx.dev)
        self.addCleanup(twin_server.server_close)
        self.addCleanup(twin_server.shutdown)
        audit = sda.Audit(
            sda.Config(targets=["Target"], out=self.out), self.client(self.dev_url, "DEV"),
            self.client(twin_url, "PROD"), log=lambda _: None,
        )
        with self.assertRaisesRegex(sda.AuditError, "same organization"):
            audit.run()

    def test_missing_solution_fails_without_output(self):
        with self.assertRaisesRegex(sda.AuditError, "wasn't found"):
            self.audit(targets=["Nope"]).run()
        self.assertFalse((self.out / "dependencies.csv").exists())

    def test_failed_graph_call_fails_without_output(self):
        self.fx.dev.fail["RetrieveMissingDependencies"] = 500
        with self.assertRaisesRegex(sda.AuditError, "RetrieveMissingDependencies failed"):
            self.audit().run()
        self.assertFalse((self.out / "dependencies.csv").exists())

    def test_spreadsheet_formulas_are_neutralized(self):
        self.audit(skip_impact=True).run()
        self.assertEqual(self.row("account.b_col")["dependent_name"], "'=Main form")

    def test_bad_arguments_are_rejected(self):
        args = sda.parse_args(["--target", "bad name", "--dev", "https://a", "--prod", "https://b"])
        with self.assertRaises(sda.AuditError):
            sda.validate(args)
        args = sda.parse_args(["--target", "ok", "--dev", "http://a", "--prod", "https://b"])
        with self.assertRaises(sda.AuditError):
            sda.validate(args)


class TextScanTests(Harness):
    def run_scan(self, **config):
        self.audit(scan_dirs=self.source_tree(), skip_impact=True, **config).run()
        return [row for row in self.rows() if row["dependent_type"] == "File"]

    def test_finds_names_the_graph_cannot_see(self):
        found = {row["required_name"] for row in self.run_scan()}
        self.assertIn("b_tbl.b_hidden", found)
        self.assertIn("b_tbl.b_lookup", found)  # matched from _b_lookup_value
        self.assertIn("Child flow", found)  # matched by GUID, resolved only on the hit
        self.assertIn("c_/scripts/helpers.js", found)  # relative link
        self.assertIn("c_other_tbl", found)  # inside the canvas package, by entity set name
        self.assertIn("c_conn", found)  # UTF-16 file

    def test_scans_a_downloaded_site(self):
        rows = self.run_scan()
        self.assertTrue(any(r["required_name"] == "b_tbl" and r["dependent_name"].startswith("portal/") for r in rows))

    def test_skips_the_targets_own_names_and_system_words(self):
        found = {row["required_name"] for row in self.run_scan()}
        self.assertNotIn("t_tbl.t_col", found)
        self.assertNotIn("account", found)

    def test_skips_binary_build_and_solution_xml(self):
        rows = self.run_scan()
        files = {row["dependent_name"] for row in rows}
        self.assertFalse(any("Plugin.dll" in f or "/bin/" in f or "Solution.xml" in f for f in files))
        self.assertFalse(any(r["required_name"] == "b_tbl.b_viatable" for r in rows))

    def test_production_hits_are_not_blockers(self):
        rows = self.run_scan()
        hit = next(r for r in rows if r["required_name"] == "account.shared_prod_col")
        self.assertEqual(hit["status"], sda.IN_PRODUCTION)

    def test_evidence_holds_a_location_but_no_file_content(self):
        rows = self.run_scan()
        hit = next(r for r in rows if r["required_name"] == "account.shared_prod_col")
        self.assertIn("Target/WebResources/a.js:1", hit["evidence"])
        self.assertNotIn("getAttribute", hit["evidence"])

    def test_text_only_rows_ask_for_confirmation(self):
        rows = self.run_scan()
        hit = next(r for r in rows if r["required_name"] == "b_tbl.b_hidden")
        self.assertIn("text scan", hit["reconcile_note"])

    def test_large_solutions_avoid_one_request_per_component(self):
        self.audit(scan_dirs=[], skip_impact=True).run()
        baseline = len(self.fx.dev.log)
        self.fx.dev.log.clear()
        for index in range(40):
            self.fx.dev.tables["solutioncomponents"].append({
                "solutioncomponentid": g(f"pp{index}"), "_solutionid_value": self.fx.sol["ProjC"],
                "objectid": g(f"page{index}"), "componenttype": 26, "rootcomponentbehavior": None,
                "rootsolutioncomponentid": None,
            })
        root = self.tmp / "empty"
        root.mkdir()
        self.audit(scan_dirs=[root], skip_impact=True).run()
        named = [entry for entry in self.fx.dev.log if "savedqueries(" in urllib.parse.unquote(str(entry[1]))]
        self.assertLessEqual(len(named), 3, f"baseline {baseline}")

    def test_a_column_name_shared_with_the_target_is_still_searched(self):
        self.fx.dev.entities["t_tbl"]["Attributes"].append({"MetadataId": g("column:t_tbl.b_hidden"), "LogicalName": "b_hidden"})
        rows = self.run_scan()
        hit = next(r for r in rows if r["required_name"] == "b_tbl.b_hidden")
        self.assertIn("may be the target's own", hit["reconcile_note"])

    def test_a_table_in_no_solution_is_searched(self):
        self.fx.dev.entity("z_orphan", columns=("z_orphan_col",))
        root = self.tmp / "orphan"
        root.mkdir()
        (root / "a.js").write_text('use("z_orphan_col");\n')
        self.audit(scan_dirs=[root], skip_impact=True).run()
        row = self.row("z_orphan.z_orphan_col")
        self.assertEqual(row["status"], sda.IN_FLIGHT)
        self.assertIn("Default only", row["production_detail"])

    def test_dotted_plug_in_type_names_are_searched(self):
        self.assertTrue(sda.usable_token("contoso.plugins.accountprecreate"))
        self.assertFalse(sda.usable_token("sales manager"))

    def test_zero_files_blocks_the_gate(self):
        root = self.tmp / "empty-folder"
        root.mkdir()
        audit = self.audit(scan_dirs=[root], skip_impact=True)
        audit.run()
        self.assertTrue(any("read no files" in blocker for blocker in audit.gate_blockers()))

    def test_unreadable_files_become_rows(self):
        root = self.tmp / "unreadable"
        root.mkdir()
        (root / "Bad.msapp").write_bytes(b"not a zip")
        (root / "Bundle.zip").write_bytes(b"PK")
        self.audit(scan_dirs=[root], skip_impact=True).run()
        rows = [r for r in self.rows() if r["required_name"].startswith("Not searched")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["status"] == sda.UNKNOWN for r in rows))

    def test_unnamed_components_block_the_gate(self):
        self.fx.dev.fail["optionset"] = 500
        self.fx.dev.fail["GlobalOptionSetDefinitions"] = 500
        audit = self.audit(scan_dirs=self.source_tree(), skip_impact=True)
        audit.run()
        self.assertTrue(any("couldn't search" in blocker for blocker in audit.gate_blockers()))


class ReconcileTests(Harness):
    def run_all(self):
        audit = self.audit(scan_dirs=self.source_tree(), stage_jsons=[self.stage_file()], skip_impact=True)
        audit.run()
        return audit

    def test_sources_merge_by_component(self):
        self.run_all()
        self.assertEqual(self.row("account.b_col")["sources"], "graph; stage")

    def test_dry_run_conflict_becomes_unknown(self):
        self.run_all()
        row = self.row("account.shared_col")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("graph says In production", row["reconcile_note"])

    def test_stage_only_row_with_an_owner_is_in_flight(self):
        self.audit(stage_jsons=[self.stage_file()], skip_impact=True).run()
        row = self.row("Child flow")
        self.assertEqual(row["status"], sda.IN_FLIGHT)
        self.assertEqual(row["owning_solutions"], "ProjC")
        self.assertEqual(row["sources"], "stage")

    def test_stage_only_row_without_an_owner_is_unknown(self):
        self.run_all()
        self.assertEqual(self.row("account.ghost")["status"], sda.UNKNOWN)
        self.assertIn("dry run", self.row("account.ghost")["reconcile_note"])

    def test_graph_row_missing_from_dry_run_needs_explaining(self):
        self.run_all()
        self.assertIn("not by the dry run", self.row("account.b_noparent")["reconcile_note"])

    def test_chain_rows_are_not_expected_in_the_dry_run(self):
        self.run_all()
        self.assertEqual(self.row("b_choice", "b_tbl")["reconcile_note"], "")

    def test_invalid_stage_file_fails(self):
        bad = self.tmp / "bad.json"
        bad.write_text('{"nope": 1}')
        with self.assertRaisesRegex(sda.AuditError, "MissingDependencies"):
            self.audit(stage_jsons=[bad]).run()

    def test_validation_messages_reach_the_map(self):
        self.run_all()
        self.assertIn("A validation warning", (self.out / "dependency-map.md").read_text())

    def test_gate_lists_what_is_missing(self):
        audit = self.audit(skip_impact=True)
        audit.run()
        blockers = " ".join(audit.gate_blockers())
        self.assertIn("text scan didn't run", blockers)
        self.assertIn("dry run isn't included", blockers)

    def test_explanations_survive_a_rerun(self):
        self.run_all()
        path = self.out / "dependencies.csv"
        rows = self.rows()
        target = next(r for r in rows if r["required_name"] == "b_tbl.b_hidden")
        target["explanation"] = "False positive: a comment in an old sample."
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=sda.CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        audit = self.run_all()
        self.assertEqual(self.row("b_tbl.b_hidden")["explanation"], "False positive: a comment in an old sample.")
        self.assertGreaterEqual(audit.counts()["False positive"], 1)

    def test_one_explanation_clears_a_component_across_its_rows(self):
        audit = self.run_all()
        for row in audit.rows.values():
            if row.note:
                row.explanation = "Explained in the register."
        self.assertFalse(any("only some sources" in blocker for blocker in audit.gate_blockers()))

    def test_file_rows_do_not_ask_about_the_dry_run(self):
        self.run_all()
        rows = [r for r in self.rows() if r["dependent_type"] == "File" and "graph" in r["sources"]]
        self.assertTrue(rows)
        self.assertTrue(all("not by the dry run" not in r["reconcile_note"] for r in rows))

    def test_an_explained_unknown_no_longer_blocks_the_gate(self):
        audit = self.run_all()
        for row in audit.rows.values():
            row.explanation = "Reviewed: see the register."
        self.assertFalse(any("Unknown rows" in blocker for blocker in audit.gate_blockers()))
        self.assertTrue(audit._open_rows())

    def test_explanations_survive_a_spreadsheet_save(self):
        self.run_all()
        rows = self.rows()
        target = next(r for r in rows if r["required_name"] == "b_tbl.b_hidden")
        target["explanation"] = "Revisado: columna obsoleta, sin función."
        with (self.out / "dependencies.csv").open("w", newline="", encoding="cp1252") as handle:
            writer = csv.DictWriter(handle, fieldnames=sda.CSV_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        self.run_all()
        self.assertEqual(self.row("b_tbl.b_hidden")["explanation"], "Revisado: columna obsoleta, sin función.")

    def test_a_csv_without_the_columns_warns(self):
        self.run_all()
        (self.out / "dependencies.csv").write_text("a,b\n1,2\n")
        audit = self.run_all()
        self.assertTrue(any("earlier explanations" in warning for warning in audit.warnings))

    def test_row_ids_are_stable_between_runs(self):
        self.run_all()
        first = {(r["dependent_name"], r["required_name"]): r["id"] for r in self.rows()}
        self.run_all()
        second = {(r["dependent_name"], r["required_name"]): r["id"] for r in self.rows()}
        self.assertEqual(first, second)


class MapTests(Harness):
    def test_map_groups_by_solution_and_uses_status_colours(self):
        self.audit(scan_dirs=self.source_tree(), skip_impact=True).run()
        text = (self.out / "dependency-map.md").read_text()
        self.assertIn("```mermaid", text)
        self.assertIn('["ProjB"]', text)
        self.assertIn('["Target"]', text)
        self.assertIn(":::inflight", text)
        self.assertIn(":::version", text)
        self.assertIn(":::unknown", text)
        self.assertIn("-.->", text)

    def test_map_lists_fixes_by_rows_cleared(self):
        self.audit(scan_dirs=self.source_tree(), skip_impact=True).run()
        text = (self.out / "dependency-map.md").read_text()
        table = text.split("## Fix these first", 1)[1].split("## Structure", 1)[0]
        counts = [int(m) for m in re.findall(r"^\| (\d+) \|", table, flags=re.M)]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_map_limit_is_reported(self):
        self.audit(skip_impact=True, map_limit=2).run()
        self.assertIn("first 2 of", (self.out / "dependency-map.md").read_text())

    def test_labels_cannot_break_the_diagram(self):
        self.assertNotIn('"', sda._mermaid_text('a"b[c]{d}`e<f>\ng'))


class MultiSolutionHarness(Harness):
    """A release made of Core and Sales. Sales needs a table that Core holds."""

    def setUp(self):
        super().setUp()
        fx = self.fx
        self.ids = {name: g(f"solution:{name}") for name in ("Core", "Sales", "Portal")}
        fx.dev.table("solutions", "solutionid", [
            {"solutionid": self.ids["Core"], "uniquename": "Core", "ismanaged": False, "version": "1.0.0.0", "isvisible": True},
            {"solutionid": self.ids["Sales"], "uniquename": "Sales", "ismanaged": False, "version": "2.0.0.0", "isvisible": True},
            {"solutionid": self.ids["Portal"], "uniquename": "Portal", "ismanaged": False, "version": "3.0.0.0", "isvisible": True},
        ])
        self.core_tbl = fx.dev.entity("core_tbl", columns=("core_col",))
        self.sales_tbl = fx.dev.entity("sales_tbl", columns=("sales_col",))
        self.sales_form, self.core_form, self.portal_form = g("sales-form"), g("core-form"), g("portal-form")
        fx.dev.tables["systemforms"].extend([
            {"formid": self.sales_form, "name": "Sales form"},
            {"formid": self.core_form, "name": "Core form"},
            {"formid": self.portal_form, "name": "Portal form"},
        ])
        self.hold("Core", self.core_tbl, 1, behavior=0)
        self.hold("Core", self.core_form, 60)
        self.hold("Sales", self.sales_tbl, 1, behavior=0)
        self.hold("Sales", self.sales_form, 60)
        self.hold("Portal", self.portal_form, 60)
        fx.dev.missing["Core"] = []
        fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.core_tbl, 1)]
        fx.dev.missing["Portal"] = []

    def hold(self, solution, object_id, ctype, behavior=None, root=None):
        solution_id = self.ids.get(solution) or self.fx.sol[solution]
        row_id = g(f"mc:{solution}:{object_id}")
        self.fx.dev.tables["solutioncomponents"].append({
            "solutioncomponentid": row_id, "_solutionid_value": solution_id,
            "objectid": object_id, "componenttype": ctype, "rootcomponentbehavior": behavior,
            "rootsolutioncomponentid": root,
        })
        return row_id

    def release(self, **config):
        config.setdefault("targets", ["Sales", "Core"])
        config.setdefault("skip_impact", True)
        return self.audit(**config)

    def stage(self, name, version, missing=(), status="Passed", **extra):
        path = self.tmp / f"stage-{name}-{len(list(self.tmp.iterdir()))}.json"
        path.write_text(json.dumps({"StageSolutionResults": dict({
            "StageSolutionStatus": status,
            "SolutionDetails": {"SolutionUniqueName": name, "SolutionVersion": version},
            "MissingDependencies": list(missing),
        }, **extra)}))
        return path

    def quiet_folders(self):
        folders = []
        for name in ("Core", "Sales"):
            root = self.tmp / name
            root.mkdir(exist_ok=True)
            (root / "a.txt").write_text("nothing relevant")
            folders.append(root)
        return folders

    def map_text(self):
        return (self.out / "dependency-map.md").read_text()


class MultiSolutionGraphTests(MultiSolutionHarness):
    def test_a_component_in_another_target_is_in_target(self):
        self.release().run()
        row = self.row("core_tbl")
        self.assertEqual(row["status"], sda.IN_TARGET)
        self.assertEqual(row["target_solutions"], "Sales")
        self.assertEqual(row["owning_solutions"], "Core")

    def test_a_clean_release_exits_zero(self):
        audit = self.release(
            scan_dirs=self.quiet_folders(),
            stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")],
        )
        self.assertEqual(audit.run(), 0, audit.gate_blockers())

    def test_every_target_must_exist_and_all_missing_ones_are_named(self):
        with self.assertRaisesRegex(sda.AuditError, "Nope, Nada weren't found"):
            self.release(targets=["Core", "Nope", "Nada"]).run()
        self.assertFalse((self.out / "dependencies.csv").exists())

    def test_a_failed_graph_call_names_the_target(self):
        self.fx.dev.fail["RetrieveMissingDependencies(SolutionUniqueName='Sales')"] = 500
        with self.assertRaisesRegex(sda.AuditError, "failed for Sales"):
            self.release().run()

    def test_rows_name_the_target_that_needs_them(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.fx.b_tbl, 1)]
        self.fx.dev.missing["Sales"].append(dep(self.sales_form, 60, self.fx.b_tbl, 1))
        self.release().run()
        self.assertEqual(self.row("b_tbl", "Core form")["target_solutions"], "Core")
        self.assertEqual(self.row("b_tbl", "Sales form")["target_solutions"], "Sales")
        self.assertEqual(self.row("b_tbl", "Sales form")["status"], sda.IN_FLIGHT)

    def test_a_dependent_in_two_targets_lists_both(self):
        self.hold("Sales", self.core_form, 60)
        dependency = dep(self.core_form, 60, self.fx.b_tbl, 1)
        self.fx.dev.missing["Core"] = [dependency]
        self.fx.dev.missing["Sales"] = [dependency]
        self.release().run()
        self.assertEqual(self.row("b_tbl", "Core form")["target_solutions"], "Core; Sales")

    def test_chain_rows_name_each_target_that_reaches_them(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.fx.b_tbl, 1)]
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        self.release().run()
        self.assertEqual(self.row("b_choice", "b_tbl")["target_solutions"], "Core; Sales")

    def test_shared_chains_are_read_only_once(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.fx.b_tbl, 1)]
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        self.release().run()
        walks = [entry for entry in self.fx.dev.log if f"@o={self.fx.b_tbl}" in str(entry[1]) and "RetrieveRequired" in str(entry[1])]
        self.assertEqual(len(walks), 1)

    def test_other_targets_are_not_in_flight(self):
        audit = self.release()
        audit.run()
        audit_ids = set(audit._in_flight_solutions())
        self.assertNotIn(self.ids["Core"], audit_ids)
        self.assertNotIn(self.ids["Sales"], audit_ids)
        self.assertIn(self.ids["Portal"], audit_ids)

    def test_an_in_flight_list_cannot_bring_a_target_back(self):
        audit = self.release(in_flight=["Core", "ProjB"])
        audit.run()
        self.assertEqual(audit._in_flight_solutions(), [self.fx.sol["ProjB"]])


class ReleaseOrderTests(MultiSolutionHarness):
    def test_needed_solution_comes_first(self):
        audit = self.release()
        audit.run()
        self.assertEqual(audit.release_order, ["Core", "Sales"])
        self.assertEqual(audit.release_cycle, [])
        text = self.map_text()
        self.assertIn("1. Core", text)
        self.assertIn("2. Sales (components needed from Core: 1)", text)

    def test_ties_are_alphabetical_whatever_the_argument_order(self):
        self.fx.dev.missing["Sales"] = []
        for targets in (["Sales", "Portal", "Core"], ["Core", "Sales", "Portal"]):
            audit = self.release(targets=targets)
            audit.run()
            self.assertEqual(audit.release_order, ["Core", "Portal", "Sales"])

    def test_a_longer_chain_is_ordered(self):
        self.fx.dev.missing["Portal"] = [dep(self.portal_form, 60, self.sales_form, 60)]
        audit = self.release(targets=["Portal", "Sales", "Core"])
        audit.run()
        self.assertEqual(audit.release_order, ["Core", "Sales", "Portal"])

    def test_a_cycle_is_an_unknown_row_that_names_the_components(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.sales_form, 60)]
        audit = self.release()
        code = audit.run()
        self.assertEqual(code, 3)
        self.assertEqual(audit.release_cycle, ["Core", "Sales"])
        row = self.row("Circular dependency")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertEqual(row["dependent_type"], "Solution")
        self.assertIn("Core needs Sales (Sales form)", row["production_detail"])
        self.assertIn("Sales needs Core (core_tbl)", row["production_detail"])
        self.assertIn("**Can't be ordered:** Core, Sales", self.map_text())
        self.assertTrue(any("Unknown rows" in blocker for blocker in audit.gate_blockers()))

    def test_nothing_but_a_fix_clears_a_cycle(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.sales_form, 60)]
        audit = self.release()
        audit.run()
        for explanation in ("Reviewed.", "False positive: no.", "Accepted: no."):
            for row in audit.rows.values():
                row.explanation = explanation
            self.assertTrue(any("need each other" in blocker for blocker in audit.gate_blockers()), explanation)
            self.assertTrue(any(row.required_name == "Circular dependency" for row in audit._open_rows()), explanation)

    def test_a_solution_after_a_cycle_is_waiting_rather_than_in_it(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.sales_form, 60)]
        self.fx.dev.missing["Portal"] = [dep(self.portal_form, 60, self.core_form, 60)]
        audit = self.release(targets=["Core", "Sales", "Portal"])
        audit.run()
        self.assertEqual(audit.release_order, [])
        self.assertEqual(audit.release_cycle, ["Core", "Sales"])
        self.assertEqual(audit.release_waiting, ["Portal"])
        self.assertIn("Waiting for those: Portal", self.map_text())

    def test_a_chain_through_an_in_flight_component_does_not_set_the_order(self):
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        self.fx.dev.required[(self.fx.b_tbl, 1)] = [dep(self.fx.b_tbl, 1, self.core_tbl, 1)]
        audit = self.release()
        audit.run()
        self.assertEqual(self.row("core_tbl", "b_tbl")["status"], sda.IN_TARGET)
        self.assertEqual(dict(audit.target_edges), {})

    def test_one_target_has_nothing_to_order(self):
        self.audit(skip_impact=True).run()
        self.assertIn("nothing to order", self.map_text())

    def test_run_log_records_targets_and_order(self):
        self.release().run()
        log = json.loads((self.out / "run-log.json").read_text())
        self.assertEqual([item["name"] for item in log["targets"]], ["Sales", "Core"])
        self.assertEqual(log["release_order"]["order"], ["Core", "Sales"])

    def test_plan_release_handles_a_self_reference(self):
        self.assertEqual(sda.plan_release(["A"], {("A", "A")}), (["A"], [], []))

    def test_plan_release_separates_a_bridge_from_the_cycles(self):
        edges = {("A", "B"), ("B", "A"), ("C", "D"), ("D", "C"), ("C", "M"), ("M", "A")}
        self.assertEqual(sda.plan_release(["A", "B", "C", "D", "M"], edges), ([], ["A", "B", "C", "D"], ["M"]))

    def test_components_production_has_do_not_set_the_order(self):
        self.fx.prod.entity("core_tbl", columns=())
        self.fx.prod.entity("sales_tbl", columns=())
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.sales_tbl, 1)]
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.core_tbl, 1)]
        audit = self.release()
        audit.run()
        self.assertEqual(audit.release_cycle, [])
        self.assertEqual(audit.release_order, ["Core", "Sales"])
        self.assertEqual(dict(audit.target_edges), {})
        self.assertEqual(self.row("core_tbl")["status"], sda.IN_TARGET)

    def test_a_shell_of_a_table_production_has_does_not_set_the_order(self):
        self.fx.dev.missing["Sales"] = []
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.fx.account, 1)]
        self.hold("Sales", self.fx.account, 1, behavior=1)
        audit = self.release()
        audit.run()
        self.assertEqual(dict(audit.target_edges), {})

    def test_a_failed_production_check_keeps_the_order_and_blocks(self):
        self.fx.prod.fail["core_tbl"] = 500
        audit = self.release()
        audit.run()
        self.assertEqual(audit.release_order, ["Core", "Sales"])
        self.assertTrue(any("release order may be wrong" in blocker for blocker in audit.gate_blockers()))

    def test_a_view_on_a_table_another_target_ships_whole_sets_the_order(self):
        view = g("sales-view")
        self.fx.dev.tables["savedqueries"].append({"savedqueryid": view, "name": "Sales view"})
        self.fx.dev.missing["Sales"] = []
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, view, 26, parent=self.sales_tbl)]
        audit = self.release()
        audit.run()
        self.assertEqual(self.row("Sales view")["status"], sda.IN_TARGET)
        self.assertEqual(audit.release_order, ["Sales", "Core"])

    def test_the_solution_map_draws_targets_and_blockers(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.fx.b_tbl, 1)]
        self.release().run()
        text = self.map_text()
        section = text.split("## Solutions", 1)[1].split("## Structure", 1)[0]
        self.assertIn('["In this release"]', section)
        self.assertIn('["ProjB"]:::inflight', section)
        self.assertRegex(section, r't\d -->\|"1"\| t\d')

    def test_the_component_map_has_a_box_for_each_target(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.fx.b_tbl, 1)]
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_col, 2, parent=self.fx.account)]
        self.release().run()
        section = self.map_text().split("## Structure", 1)[1]
        self.assertIn('["Core"]', section)
        self.assertIn('["Sales"]', section)


class SharedComponentTests(MultiSolutionHarness):
    def test_a_component_in_two_targets_is_a_warning_not_a_row(self):
        self.hold("Sales", self.core_form, 60)
        audit = self.release()
        audit.run()
        self.assertFalse(any(r["dependent_type"] == "Solutions" for r in self.rows()))
        self.assertEqual(audit.cross_target[0]["solutions"], ["Core", "Sales"])
        self.assertIn("## Components in more than one target solution", self.map_text())
        self.assertIn("| Core form | Form | Core, Sales |", self.map_text())
        self.assertTrue(any("more than one target" in warning for warning in audit.warnings))

    def test_a_component_in_two_targets_does_not_block_a_clean_release(self):
        self.hold("Sales", self.core_form, 60)
        audit = self.release(
            scan_dirs=self.quiet_folders(),
            stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")],
        )
        self.assertEqual(audit.run(), 0, audit.gate_blockers())

    def test_a_table_shell_in_two_targets_is_not_reported(self):
        self.hold("Sales", self.core_tbl, 1, behavior=1)
        audit = self.release()
        audit.run()
        self.assertEqual(audit.cross_target, [])

    def add_column(self, table, column):
        self.fx.dev.entities[table]["Attributes"].append({"MetadataId": g(f"column:{table}.{column}"), "LogicalName": column})
        return g(f"column:{table}.{column}")

    def test_in_flight_parts_of_a_table_shipped_with_all_objects_are_found(self):
        column = self.add_column("core_tbl", "b_leak")
        shell = self.hold("ProjB", self.core_tbl, 1, behavior=1)
        self.hold("ProjB", column, 2, root=shell)
        audit = self.release(scan_dirs=self.quiet_folders(), stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")])
        self.assertEqual(audit.run(), 3)
        row = self.row("core_tbl.b_leak", "Core and ProjB")
        self.assertEqual(row["status"], sda.IN_FLIGHT)
        self.assertIn("with all objects", row["production_detail"])

    def test_parts_of_a_table_kept_as_a_shell_are_not_flagged(self):
        column = self.add_column("sales_tbl", "b_other")
        self.fx.dev.tables["solutioncomponents"] = [
            row for row in self.fx.dev.tables["solutioncomponents"]
            if not (row["objectid"] == self.sales_tbl and row["_solutionid_value"] == self.ids["Sales"])
        ]
        self.hold("Sales", self.sales_tbl, 1, behavior=1)
        shell = self.hold("ProjB", self.sales_tbl, 1, behavior=1)
        self.hold("ProjB", column, 2, root=shell)
        self.release().run()
        self.assertFalse(any(r["required_name"] == "sales_tbl.b_other" and r["dependent_type"] == "Solutions" for r in self.rows()))

    def test_a_column_added_to_another_targets_whole_table_is_in_both(self):
        column = self.add_column("core_tbl", "sales_extra")
        shell = self.hold("Sales", self.core_tbl, 1, behavior=1)
        self.hold("Sales", column, 2, root=shell)
        audit = self.release()
        audit.run()
        self.assertEqual([item["name"] for item in audit.cross_target], ["core_tbl.sales_extra"])
        self.assertEqual(audit.cross_target[0]["solutions"], ["Core", "Sales"])

    def test_shared_rows_do_not_ask_about_the_dry_run(self):
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        self.hold("ProjB", self.sales_form, 60)
        self.fx.dev.missing["Sales"].append(dep(self.core_form, 60, self.sales_form, 60))
        self.release(stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")]).run()
        self.assertEqual(self.row("Sales form", "Sales and ProjB")["reconcile_note"], "")

    def test_a_component_shared_with_an_in_flight_solution_is_reported_per_target(self):
        self.hold("ProjB", self.core_form, 60)
        self.hold("ProjB", self.sales_form, 60)
        self.release().run()
        self.assertEqual(self.row("Core form", "Core and ProjB")["target_solutions"], "Core")
        self.assertEqual(self.row("Sales form", "Sales and ProjB")["target_solutions"], "Sales")
        self.assertIn("exporting Sales also exports ProjB's edits", self.row("Sales form", "Sales and ProjB")["production_detail"])


class MultiSolutionScanTests(MultiSolutionHarness):
    def folder(self, name, text):
        root = self.tmp / name
        root.mkdir(exist_ok=True)
        (root / "a.js").write_text(text)
        return root

    def test_names_held_by_another_target_are_not_flagged(self):
        root = self.folder("Sales", 'use("core_tbl", "core_col", "sales_col");\n')
        self.release(scan_dirs=[root]).run()
        self.assertFalse([r for r in self.rows() if r["dependent_type"] == "File"])

    def test_a_third_solution_is_still_in_flight(self):
        self.hold("Portal", self.fx.dev.entity("portal_tbl", columns=("portal_col",)), 1, behavior=0)
        root = self.folder("Sales", 'use("portal_col");\n')
        self.release(scan_dirs=[root]).run()
        self.assertEqual(self.row("portal_tbl.portal_col")["status"], sda.IN_FLIGHT)

    def test_a_folder_named_after_a_target_is_tied_to_it(self):
        root = self.folder("sales", 'use("b_hidden");\n')
        self.release(scan_dirs=[root]).run()
        self.assertEqual(self.row("b_tbl.b_hidden")["target_solutions"], "Sales")

    def test_a_repository_folder_is_not_tied_to_a_target(self):
        root = self.folder("plugins-repo", 'use("b_hidden");\n')
        self.release(scan_dirs=[root]).run()
        self.assertEqual(self.row("b_tbl.b_hidden")["target_solutions"], "")
        self.assertIn('["Repository code"]', self.map_text())

    def test_a_target_without_a_folder_blocks_the_gate(self):
        root = self.folder("Core", "nothing relevant\n")
        audit = self.release(scan_dirs=[root], stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")])
        self.assertEqual(audit.run(), 3)
        self.assertTrue(any("No scanned folder is named after Sales" in blocker for blocker in audit.gate_blockers()))

    def test_every_folder_inside_an_unpacked_solution_is_searched(self):
        root = self.folder("Sales", "nothing\n")
        (root / "Other").mkdir()
        (root / "Other" / "Solution.xml").write_text("<ImportExportXml/>")
        for name in ("dist", "bin", "obj", "packages"):
            (root / "WebResources" / name).mkdir(parents=True)
            (root / "WebResources" / name / "bundle.js").write_text('use("b_hidden");\n')
        self.release(scan_dirs=[root]).run()
        rows = [r for r in self.rows() if r["required_name"] == "b_tbl.b_hidden" and r["dependent_type"] == "File"]
        self.assertEqual(len(rows), 4)

    def test_build_folders_in_a_repository_are_listed_as_not_searched(self):
        root = self.folder("plugins-repo", "nothing\n")
        (root / "src" / "bin").mkdir(parents=True)
        (root / "src" / "bin" / "out.js").write_text('use("b_hidden");\n')
        audit = self.release(scan_dirs=[root])
        audit.run()
        self.assertFalse(any(r["required_name"] == "b_tbl.b_hidden" for r in self.rows()))
        self.assertEqual(audit.coverage["build_folders_not_searched"], ["plugins-repo/src/bin"])
        self.assertIn("Build folders not searched in repositories: plugins-repo/src/bin", self.map_text())

    def test_a_folder_that_cannot_be_read_becomes_a_row(self):
        root = self.folder("plugins-repo", "nothing\n")
        locked = root / "locked"
        real_walk = sda.os.walk

        def walk(top, onerror=None):
            error = PermissionError(13, "Permission denied")
            error.filename = str(locked)
            onerror(error)
            yield from real_walk(top)

        with mock.patch.object(sda.os, "walk", walk):
            self.release(scan_dirs=[root]).run()
        row = self.row("Not searched: plugins-repo/locked")
        self.assertEqual(row["status"], sda.UNKNOWN)

    def test_skipped_submodules_become_a_row(self):
        root = self.folder("Sales", "nothing\n")
        source = {"name": "plugins", "url": "https://dev.azure.com/c/p/_git/plugins", "submodules_skipped": True}
        self.release(scan_dirs=[root], sources=[source]).run()
        row = self.row("Not searched: submodules of plugins")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("submodules", row["production_detail"])

    def test_a_failed_table_listing_blocks_the_gate(self):
        self.fx.dev.fail["EntityDefinitions?"] = 500
        audit = self.release(
            scan_dirs=self.quiet_folders(),
            stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")],
        )
        self.assertEqual(audit.run(), 3)
        self.assertTrue(any("unmanaged custom tables" in blocker for blocker in audit.gate_blockers()))

    def test_one_target_owns_every_folder(self):
        root = self.folder("plugins-repo", 'use("b_hidden");\n')
        self.audit(scan_dirs=[root], skip_impact=True).run()
        self.assertEqual(self.row("b_tbl.b_hidden")["target_solutions"], "Target")

    def add_assembly(self, holders):
        assembly = g("assembly")
        self.fx.dev.table("pluginassemblies", "pluginassemblyid", [{"pluginassemblyid": assembly, "name": "Contoso.Plugins"}])
        for name in holders:
            self.hold(name, assembly, 91)
        self.fx.dev.table("plugintypes", "plugintypeid", [
            {"plugintypeid": g("type-new"), "typename": "Contoso.Plugins.New", "_pluginassemblyid_value": assembly},
        ])
        self.fx.prod.table("pluginassemblies", "pluginassemblyid", [])
        self.fx.prod.table("plugintypes", "plugintypeid", [])

    def test_plug_ins_in_any_target_are_checked_once(self):
        self.add_assembly(["Core", "Sales"])
        root = self.folder("plugins-repo", "// no classes here\n")
        (root / "New.cs").write_text("public class New : IPlugin {}\n")
        self.release(scan_dirs=[root]).run()
        rows = [r for r in self.rows() if r["required_name"] == "Contoso.Plugins.New"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_solutions"], "Core; Sales")


class MultiSolutionDryRunTests(MultiSolutionHarness):
    def missing_table(self):
        return {"RequiredComponentId": self.core_tbl, "RequiredComponentSchemaName": "core_tbl", "RequiredComponentType": "Entity"}

    def test_every_target_needs_a_dry_run(self):
        audit = self.release(scan_dirs=self.quiet_folders(), stage_jsons=[self.stage("Core", "1.0.0.0")])
        self.assertEqual(audit.run(), 3)
        self.assertIn("There's no dry run for Sales.", audit.gate_blockers())

    def test_no_dry_runs_at_all_reads_the_same_as_before(self):
        audit = self.release()
        audit.run()
        self.assertIn("The dry run isn't included.", audit.gate_blockers())

    def test_a_dry_run_of_a_solution_outside_the_release_blocks(self):
        audit = self.release(
            scan_dirs=self.quiet_folders(),
            stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0"), self.stage("Portal", "3.0.0.0")],
        )
        self.assertEqual(audit.run(), 3)
        self.assertTrue(any("tested 'Portal'" in blocker for blocker in audit.gate_blockers()))

    def test_two_dry_runs_of_one_target_block(self):
        audit = self.release(
            stage_jsons=[self.stage("Core", "1.0.0.0"), self.stage("Core", "1.0.0.0"), self.stage("Sales", "2.0.0.0")],
        )
        audit.run()
        self.assertTrue(any("two dry runs for Core" in blocker for blocker in audit.gate_blockers()))

    def test_versions_are_checked_for_each_target(self):
        audit = self.release(stage_jsons=[self.stage("Core", "1.0.0.0"), self.stage("Sales", "1.0.0.0")])
        audit.run()
        self.assertTrue(any("dry run of Sales tested version 1.0.0.0, but DEV has 2.0.0.0" in blocker for blocker in audit.gate_blockers()))
        self.assertFalse(any("dry run of Core tested" in blocker for blocker in audit.gate_blockers()))

    def test_a_failed_dry_run_names_its_target(self):
        audit = self.release(stage_jsons=[self.stage("Core", "1.0.0.0", status="Failed"), self.stage("Sales", "2.0.0.0")])
        audit.run()
        self.assertTrue(any("dry run of Core didn't pass" in blocker for blocker in audit.gate_blockers()))

    def test_needing_another_target_is_expected_in_a_dry_run(self):
        audit = self.release(
            stage_jsons=[self.stage("Sales", "2.0.0.0", [self.missing_table()], status="Failed"), self.stage("Core", "1.0.0.0")],
        )
        audit.run()
        row = self.row("core_tbl")
        self.assertEqual(row["status"], sda.IN_TARGET)
        self.assertEqual(row["reconcile_note"], "")

    def test_a_target_missing_its_own_component_is_a_conflict(self):
        self.fx.dev.missing["Core"] = [dep(self.core_form, 60, self.core_tbl, 1)]
        audit = self.release(stage_jsons=[self.stage("Core", "1.0.0.0", [self.missing_table()]), self.stage("Sales", "2.0.0.0")])
        audit.run()
        conflicted = [r for r in self.rows() if r["required_name"] == "core_tbl" and r["status"] == sda.UNKNOWN]
        self.assertTrue(conflicted)
        self.assertIn("graph says In target", conflicted[0]["reconcile_note"])

    def test_a_dry_run_can_reveal_the_release_order(self):
        self.fx.dev.missing["Sales"] = []
        audit = self.release(
            stage_jsons=[self.stage("Sales", "2.0.0.0", [self.missing_table()], status="Failed"), self.stage("Core", "1.0.0.0")],
        )
        audit.run()
        self.assertEqual(audit.release_order, ["Core", "Sales"])
        self.assertEqual(self.row("core_tbl")["status"], sda.IN_TARGET)
        self.assertEqual(self.row("core_tbl")["sources"], "stage")

    def test_an_id_is_never_matched_by_name_to_another_component(self):
        twin = g("projb-core-form")
        self.fx.dev.tables["systemforms"].append({"formid": twin, "name": "Core form"})
        self.hold("ProjB", twin, 60)
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.core_form, 60)]
        items = [
            {"RequiredComponentId": self.core_form, "RequiredComponentSchemaName": "Core form", "RequiredComponentType": "SystemForm"},
            {"RequiredComponentId": twin, "RequiredComponentSchemaName": "Core form", "RequiredComponentType": "SystemForm",
             "DependentComponentSchemaName": "Sales form", "DependentComponentType": "SystemForm"},
        ]
        audit = self.release(scan_dirs=self.quiet_folders(), stage_jsons=[self.stage("Sales", "2.0.0.0", items, status="Failed"), self.stage("Core", "1.0.0.0")])
        self.assertEqual(audit.run(), 3)
        statuses = {r["status"] for r in self.rows() if r["required_id"] == twin}
        self.assertEqual(statuses, {sda.IN_FLIGHT})

    def test_a_null_missing_list_reads_as_empty(self):
        path = self.tmp / "null.json"
        path.write_text(json.dumps({"StageSolutionResults": {
            "StageSolutionStatus": "Passed", "MissingDependencies": None,
            "SolutionDetails": {"SolutionUniqueName": "Core", "SolutionVersion": "1.0.0.0"},
        }}))
        audit = self.release(stage_jsons=[path])
        audit.run()
        self.assertEqual(audit.dry_runs["Core"], "Passed")

    def test_a_stage_only_row_naming_a_target_needs_no_id(self):
        self.fx.dev.missing["Sales"] = []
        item = {"RequiredComponentSchemaName": "core_thing", "RequiredComponentType": "Entity", "RequiredSolutionName": "core"}
        audit = self.release(stage_jsons=[self.stage("Sales", "2.0.0.0", [item], status="Failed"), self.stage("Core", "1.0.0.0")])
        audit.run()
        self.assertEqual(self.row("core_thing")["status"], sda.IN_TARGET)
        self.assertEqual(audit.release_order, ["Core", "Sales"])

    def test_a_stage_only_row_in_the_target_itself_is_unknown(self):
        self.fx.dev.missing["Sales"] = []
        item = {"RequiredComponentId": self.sales_tbl, "RequiredComponentSchemaName": "sales_tbl", "RequiredComponentType": "Entity"}
        audit = self.release(stage_jsons=[self.stage("Sales", "2.0.0.0", [item], status="Failed"), self.stage("Core", "1.0.0.0")])
        audit.run()
        row = self.row("sales_tbl")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("Sales holds it", row["production_detail"])

    def test_the_same_in_flight_component_in_two_dry_runs_names_both_targets(self):
        item = {"RequiredComponentId": self.fx.child_flow, "RequiredComponentSchemaName": "Child flow", "RequiredComponentType": "Workflow",
                "DependentComponentSchemaName": "A dependent", "DependentComponentType": "Workflow"}
        other = dict(item, DependentComponentSchemaName="Another dependent")
        self.release(stage_jsons=[self.stage("Core", "1.0.0.0", [item]), self.stage("Sales", "2.0.0.0", [other])]).run()
        self.assertEqual(self.row("Child flow", "A dependent")["target_solutions"], "Core")
        self.assertEqual(self.row("Child flow", "Another dependent")["target_solutions"], "Sales")
        self.assertEqual(self.row("Child flow", "A dependent")["status"], sda.IN_FLIGHT)

    def test_a_graph_row_is_only_compared_with_its_own_targets_dry_run(self):
        self.fx.dev.missing["Sales"].append(dep(self.sales_form, 60, self.fx.b_tbl, 1))
        self.release(stage_jsons=[self.stage("Core", "1.0.0.0")]).run()
        self.assertEqual(self.row("b_tbl", "Sales form")["reconcile_note"], "")
        self.release(stage_jsons=[self.stage("Sales", "2.0.0.0"), self.stage("Core", "1.0.0.0")]).run()
        self.assertIn("not by the dry run", self.row("b_tbl", "Sales form")["reconcile_note"])

    def test_the_map_shows_each_dry_run(self):
        self.release(stage_jsons=[self.stage("Core", "1.0.0.0")]).run()
        self.assertIn("- Dry run: Sales not included, Core Passed", self.map_text())

    def test_validation_messages_name_their_target(self):
        path = self.stage("Core", "1.0.0.0", SolutionValidationResults=[{"Message": "Check this", "SolutionValidationResultType": "Warning"}])
        self.release(stage_jsons=[path]).run()
        self.assertIn("Core: Check this", self.map_text())


class ClosingRowsTests(MultiSolutionHarness):
    def explain(self, audit, required_name, text):
        for row in audit.rows.values():
            if row.required_name == required_name:
                row.explanation = text

    def test_a_false_positive_cannot_close_a_real_dependency(self):
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        audit = self.release()
        audit.run()
        self.explain(audit, "b_tbl", "False positive: looks fine.")
        self.assertTrue(any(row.required_name == "b_tbl" for row in audit._open_rows()))
        self.explain(audit, "b_tbl", "Accepted: looks fine.")
        self.assertTrue(any(row.required_name == "b_tbl" for row in audit._open_rows()))

    def test_a_false_positive_cannot_close_a_failed_check(self):
        self.fx.prod.fail["b_tbl"] = 500
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        audit = self.release()
        audit.run()
        self.explain(audit, "b_tbl", "False positive: looks fine.")
        self.assertTrue(any(row.required_name == "b_tbl" and row.status == sda.UNKNOWN for row in audit._open_rows()))

    def test_accepted_closes_an_unknown_a_person_checked(self):
        self.fx.prod.fail["b_tbl"] = 500
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        audit = self.release()
        audit.run()
        self.explain(audit, "b_tbl", "Accepted: checked production by hand.")
        self.assertFalse(any(row.required_name == "b_tbl" for row in audit._open_rows()))
        self.assertEqual(audit.counts()["Accepted"], 1)

    def test_accepted_closes_a_new_plug_in_type(self):
        row = sda.Row("", "Contoso.Plugins", "Plug-in assembly", None, "", "Contoso.Plugins.New", "Plug-in type", sda.IN_FLIGHT, "", "")
        row.explanation = "Accepted: it belongs in this release."
        self.assertEqual(sda.Audit._closed_as(row), "Accepted")

    def test_explanations_that_cannot_close_a_row_are_flagged(self):
        self.fx.dev.missing["Sales"] = [dep(self.sales_form, 60, self.fx.b_tbl, 1)]
        self.release().run()
        rows = self.rows()
        for row in rows:
            row["explanation"] = "False positive: no." if row["required_name"] == "b_tbl" else ""
        with (self.out / "dependencies.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=sda.CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        audit = self.release()
        audit.run()
        self.assertTrue(any("can't close them" in warning for warning in audit.warnings))


class FailedLookupTests(Harness):
    def test_a_failed_owner_lookup_cannot_hide_an_old_version(self):
        self.fx.prod.tables["workflows"].append({"workflowid": self.fx.ms_flow, "name": "Microsoft flow"})
        self.fx.dev.missing["Target"] = [dep(self.fx.flow, 29, self.fx.ms_flow, 29, base=self.fx.sol["MsBase"], introduced=2.0)]
        self.audit(skip_impact=True).run()
        self.assertEqual(self.row("Microsoft flow")["status"], sda.VERSION_TOO_OLD)
        self.fx.dev.fail[f"solutions({self.fx.sol['MsBase']})"] = 500
        self.audit(skip_impact=True).run()
        row = self.row("Microsoft flow")
        self.assertEqual(row["status"], sda.UNKNOWN)
        self.assertIn("owner check failed", row["production_detail"])

    def test_a_read_timeout_is_retried_and_reported(self):
        client = self.client(self.dev_url, "DEV", retries=1)
        client._opener = mock.Mock()
        client._opener.open.side_effect = socket.timeout("timed out")
        with self.assertRaisesRegex(sda.ApiError, "could not reach DEV"):
            client.get("WhoAmI")
        self.assertEqual(client._opener.open.call_count, 2)

    def test_same_organization_can_be_confirmed(self):
        twin_server, twin_url = start_server(self.fx.dev)
        self.addCleanup(twin_server.server_close)
        self.addCleanup(twin_server.shutdown)
        audit = sda.Audit(
            sda.Config(targets=["Target"], out=self.out, skip_impact=True, allow_same_org_id=True), self.client(self.dev_url, "DEV"),
            self.client(twin_url, "PROD"), log=lambda _: None,
        )
        audit.run()
        self.assertTrue(any("same organization ID" in warning for warning in audit.warnings))


class GeneratedCodeTests(Harness):
    def scan(self, name, text):
        root = self.tmp / "repo"
        root.mkdir(exist_ok=True)
        (root / name).write_text(text)
        self.audit(scan_dirs=[root], skip_impact=True).run()
        return self.row("b_tbl.b_hidden")

    def test_a_script_that_mentions_the_words_is_not_generated(self):
        row = self.scan("app.js", '// generated by a tool, then edited\nuse("b_hidden");\n')
        self.assertEqual(row["explanation"], "")

    def test_the_automatic_note_is_decided_again_on_each_run(self):
        self.assertTrue(self.scan("Model.cs", '// <auto-generated>\nclass b_hidden {}\n')["explanation"].startswith("False positive"))
        self.assertEqual(self.scan("Model.cs", 'class b_hidden {}\n')["explanation"], "")


class CommandLineTests(Harness):
    def test_bad_arguments_exit_with_two(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sda.main(["--target", "bad name", "--dev", "https://a", "--prod", "https://b"]), 2)
            self.assertEqual(sda.main(["--target", "ok", "--dev", "http://a", "--prod", "https://b"]), 2)
            self.assertEqual(sda.main(["--target", "ok", "--dev", "https://a", "--prod", "https://b", "--stage-json", "missing.json"]), 2)

    def test_windows_paths_survive_in_a_command(self):
        self.assertEqual(sda.split_command(r"C:\tools\get-token.cmd --scope x", windows=True), [r"C:\tools\get-token.cmd", "--scope", "x"])
        self.assertEqual(sda.split_command(r'"C:\Program Files\t.cmd" a', windows=True), [r"C:\Program Files\t.cmd", "a"])

    def test_posix_commands_keep_their_quoting(self):
        self.assertEqual(sda.split_command('az get --query "a b"', windows=False), ["az", "get", "--query", "a b"])

    def test_an_unbalanced_quote_is_explained(self):
        with self.assertRaises(sda.AuditError):
            sda.split_command('az "get', windows=False)


class MultiSolutionArgumentTests(unittest.TestCase):
    BASE = ["--dev", "https://dev.example", "--prod", "https://prod.example"]

    def test_targets_can_repeat_or_use_commas(self):
        args = sda.parse_args(["--target", "Core,Sales", "--target", "Portal, core"] + self.BASE)
        self.assertEqual(sda.split_names(args.target), ["Core", "Sales", "Portal"])
        sda.validate(args)

    def test_every_target_name_is_validated(self):
        args = sda.parse_args(["--target", "Core,bad name"] + self.BASE)
        with self.assertRaisesRegex(sda.AuditError, "bad name"):
            sda.validate(args)

    def test_an_empty_target_is_refused(self):
        args = sda.parse_args(["--target", " , "] + self.BASE)
        with self.assertRaisesRegex(sda.AuditError, "at least one"):
            sda.validate(args)

    def test_a_target_cannot_also_be_in_flight(self):
        args = sda.parse_args(["--target", "Core,Sales", "--in-flight", "ProjB,core"] + self.BASE)
        with self.assertRaisesRegex(sda.AuditError, "both a target and in-flight"):
            sda.validate(args)

    def test_stage_files_repeat_and_must_exist(self):
        args = sda.parse_args(["--target", "Core", "--stage-json", "a.json", "--stage-json", "b.json"] + self.BASE)
        self.assertEqual([path.name for path in args.stage_json], ["a.json", "b.json"])
        with self.assertRaisesRegex(sda.AuditError, "wasn't found"):
            sda.validate(args)


class CheckCommandTests(MultiSolutionHarness):
    def test_check_lists_every_target_and_changes_nothing(self):
        out = io.StringIO()
        argv = ["--target", "Core,Sales", "--dev", self.dev_url, "--prod", self.prod_url, "--out", str(self.out), "--check"]
        with mock.patch.object(sda, "load_auth", return_value=(lambda _: TOKEN, None)), mock.patch.object(sda, "validate"), contextlib.redirect_stdout(out):
            self.assertEqual(sda.main(argv), 0)
        self.assertIn("Target:     Core 1.0.0.0", out.getvalue())
        self.assertIn("Target:     Sales 2.0.0.0", out.getvalue())
        self.assertFalse(self.out.exists())


class SignInTests(unittest.TestCase):
    def test_falls_back_to_the_azure_cli(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(tmp)
        done = mock.Mock(returncode=0, stdout="cli-token\n", stderr="")
        with mock.patch.object(sda.shutil, "which", return_value="/usr/bin/az"), mock.patch.object(sda.subprocess, "run", return_value=done) as run, mock.patch.object(sda, "__file__", str(tmp / "x.py")):
            provider, headers = sda.load_auth(None)
            self.assertEqual(provider("https://dev.crm.dynamics.com/"), "cli-token")
        self.assertIsNone(headers)
        command = run.call_args[0][0]
        self.assertEqual(command[:3], ["/usr/bin/az", "account", "get-access-token"])
        self.assertIn("https://dev.crm.dynamics.com", command)

    def test_a_failed_cli_sign_in_is_explained_and_shows_no_token(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="Please run az login")
        with mock.patch.object(sda.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(sda.AuditError, "az login --tenant"):
                sda.az_token("https://dev.crm.dynamics.com")

    def test_no_way_to_sign_in_is_explained(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(tmp)
        with mock.patch.object(sda.shutil, "which", return_value=None), mock.patch.object(sda, "__file__", str(tmp / "x.py")):
            with self.assertRaisesRegex(sda.AuditError, "no way to sign in"):
                sda.load_auth(None)


class HelperTests(unittest.TestCase):
    def test_introduced_version_compare(self):
        self.assertTrue(sda._older_than_introduced("9.0.0.1", 9.1))
        self.assertFalse(sda._older_than_introduced("9.1.0.0", 9.1))
        self.assertFalse(sda._older_than_introduced("1.0.0.0", 1.0))
        self.assertTrue(sda._older_than_introduced("1.5.0.0", 2.0))
        self.assertFalse(sda._older_than_introduced("1.0.0.0", None))
        self.assertFalse(sda._older_than_introduced("1.0.0.0", 0))

    def test_guid_normalizing(self):
        self.assertEqual(sda.norm_guid("{ABC}"), "abc")
        self.assertEqual(sda.norm_guid(sda.EMPTY_GUID), "")
        self.assertEqual(sda.norm_guid(None), "")

    def test_token_filter(self):
        self.assertTrue(sda.usable_token("abc_col"))
        self.assertTrue(sda.usable_token("helpers.js"))
        self.assertFalse(sda.usable_token("account"))
        self.assertFalse(sda.usable_token("a_b"))

    def test_odata_quotes(self):
        self.assertEqual(sda.odata_text("O'Neil"), "O''Neil")


if __name__ == "__main__":
    unittest.main()
