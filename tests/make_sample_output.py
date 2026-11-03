"""Rebuild examples/sample-output from the simulated environments the tests use.

Run from the repository root: python tests/make_sample_output.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_external_dependency_audit as external  # noqa: E402
import test_solution_dependency_audit as solution  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "examples" / "sample-output"
KEEP = {"dependencies.csv", "dependency-map.md", "external-dependencies.csv", "external-map.md"}
dep = solution.dep


def clean(urls):
    for path in OUT.iterdir():
        if path.name not in KEEP:
            path.unlink()
            continue
        text = path.read_text(encoding="utf-8-sig")
        for url, name in urls.items():
            text = text.replace(url, name)
        text = re.sub(r"Generated \d{4}-\d\d-\d\d \d\d:\d\d UTC", "Generated from simulated environments", text)
        path.write_text(text, encoding="utf-8")


class SolutionSample(solution.MultiSolutionHarness):
    def runTest(self):
        fx = self.fx
        fx.dev.missing["Core"] = [dep(self.core_form, 60, fx.b_col, 2, parent=fx.account)]
        fx.dev.missing["Sales"] = [
            dep(self.sales_form, 60, self.core_tbl, 1),
            dep(self.sales_form, 60, fx.b_col, 2, parent=fx.account),
            dep(self.sales_form, 60, fx.b_tbl, 1),
            dep(self.sales_form, 60, fx.shared_col, 2, parent=fx.account),
        ]
        self.hold("Sales", self.core_form, 60)
        code = self.tmp / "plugins-repo"
        code.mkdir()
        (code / "Handler.cs").write_text('var c = entity["b_hidden"];\n')
        self.out = OUT
        missing_column = {
            "RequiredComponentId": fx.b_col, "RequiredComponentSchemaName": "b_col",
            "RequiredComponentParentSchemaName": "account", "RequiredComponentType": "Attribute",
        }
        stage_core = self.stage("Core", "1.0.0.0", [missing_column], status="Failed")
        stage_sales = self.stage("Sales", "2.0.0.0", [
            {"RequiredComponentId": self.core_tbl, "RequiredComponentSchemaName": "core_tbl", "RequiredComponentType": "Entity"},
            missing_column,
            {"RequiredComponentId": fx.b_tbl, "RequiredComponentSchemaName": "b_tbl", "RequiredComponentType": "Entity"},
        ], status="Failed")
        self.release(scan_dirs=self.quiet_folders() + [code], stage_jsons=[stage_core, stage_sales]).run()
        self.urls = {self.dev_url: "https://contoso-dev.crm.dynamics.com", self.prod_url: "https://contoso.crm.dynamics.com"}
        clean(self.urls)


class ExternalSample(external.Harness):
    def runTest(self):
        self.out = OUT
        self.audit().run()
        clean({})


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(unittest.TestSuite([SolutionSample(), ExternalSample()]))
    sys.exit(0 if result.wasSuccessful() else 1)
