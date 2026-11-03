#!/usr/bin/env python3
"""Check that everything the audit needs is ready, before it runs.

It checks the tools, the sign-ins and the project folder, and validates audit-config.json
if you pass one. For anything missing, it prints the exact fix. It shows which account and
tenant are signed in, so you can confirm they're the customer's.

It changes nothing, and it never asks for or prints a token. It can't prove your access to
an environment: the --check option of each audit does that.

Needs only the Python standard library, and solution_dependency_audit.py in the same folder.

Exit codes
  0  everything is ready
  2  bad arguments
  3  something needs fixing
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from solution_dependency_audit import SOLUTION_NAME_RE, make_output_safe, version_tuple

__version__ = "1.0.0"

MIN_PYTHON = (3, 9)
MIN_GIT = (2, 31)
OK = "OK"
FIX = "FIX"
INFO = "INFO"

CONFIG_KEYS = {"targets", "dev", "prod", "sandbox", "release", "azure_devops", "includes", "power_pages_site_id"}
INCLUDE_KEYS = ("power_pages", "sharepoint", "azure_functions", "plugins")
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def run(command: Sequence[str], cwd: Optional[Path] = None) -> Optional[Tuple[int, str]]:
    """Run a tool and return its exit code and output, or None if it couldn't run."""
    executable = shutil.which(command[0])
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, *command[1:]], capture_output=True, encoding="utf-8", errors="replace", timeout=120, cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode, result.stdout or ""


# Tools --------------------------------------------------------------------------------
def check_python(version: Sequence[int] = tuple(sys.version_info[:3])) -> Check:
    shown = ".".join(str(part) for part in version)
    if tuple(version[:2]) < MIN_PYTHON:
        return Check("Python", FIX, f"Version {shown} is too old.", "Install Python 3.9 or later from https://www.python.org/downloads/")
    return Check("Python", OK, f"Version {shown}")


def check_git() -> Check:
    result = run(["git", "--version"])
    if result is None or result[0] != 0:
        return Check("Git", FIX, "It isn't installed.", "Install Git 2.31 or later from https://git-scm.com/downloads")
    found = version_tuple(result[1])
    shown = ".".join(str(part) for part in found[:3])
    if found[:2] < MIN_GIT:
        return Check("Git", FIX, f"Version {shown} is too old.", "Install Git 2.31 or later from https://git-scm.com/downloads")
    return Check("Git", OK, f"Version {shown}")


def check_azure_cli(project: Path) -> Check:
    has_plugin_sign_in = (project / "scripts" / "auth.py").is_file()
    if shutil.which("az") is None:
        if has_plugin_sign_in:
            return Check("Azure CLI", INFO, "It isn't installed. Dataverse sign-in uses scripts/auth.py, but the Azure DevOps and external audits need the Azure CLI, or their own token commands.")
        return Check("Azure CLI", FIX, "It isn't installed.", "Install it from https://learn.microsoft.com/cli/azure/install-azure-cli")
    result = run(["az", "account", "show", "--query", "{user: user.name, tenant: tenantId}", "-o", "json"])
    if result is None or result[0] != 0:
        return Check("Azure CLI sign-in", FIX, "You aren't signed in.", "Run: az login --tenant <tenant-id>")
    try:
        account = json.loads(result[1])
        return Check("Azure CLI sign-in", OK, f"{account.get('user')}, tenant {account.get('tenant')}. Confirm this is the customer's tenant.")
    except (ValueError, AttributeError):
        return Check("Azure CLI sign-in", FIX, "The Azure CLI's answer couldn't be read.", "Run: az account show")


def check_pac() -> Check:
    if shutil.which("pac") is None:
        return Check("Power Platform CLI", FIX, "It isn't installed.", "Install it from https://learn.microsoft.com/power-platform/developer/cli/introduction")
    result = run(["pac", "auth", "list"])
    fix = "Run: pac auth create --environment <DEV url>"
    if result is None or result[0] != 0 or "no profiles" in result[1].lower():
        return Check("Power Platform CLI sign-in", FIX, "There's no sign-in profile.", fix)
    active = next((" ".join(line.split()) for line in result[1].splitlines() if "*" in line), "")
    if not active:
        return Check("Power Platform CLI sign-in", FIX, "No profile is active.", "Run: pac auth select --index <number>")
    return Check("Power Platform CLI sign-in", OK, f"Active profile: {active}. Confirm it's the customer's.")


# Project folder -----------------------------------------------------------------------
def check_folder(project: Path) -> List[Check]:
    manifest = project / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            is_tool = json.loads(manifest.read_text(encoding="utf-8-sig")).get("name") == "dependency-audit"
        except (OSError, ValueError, AttributeError):
            is_tool = False
        if is_tool:
            return [Check(
                "Project folder", FIX, "This is the audit tool's own public repository, so results could be published.",
                "Move to the customer's private project folder, and run this again.",
            )]
    probe = run(["git", "rev-parse", "--is-inside-work-tree"], project)
    if probe is None or probe[0] != 0 or probe[1].strip() != "true":
        return [Check("Project folder", INFO, f"{project.name} isn't in a Git repository. Keep the results in the customer's private repository.")]
    unprotected = [folder for folder in ("build", "sources") if _not_ignored(project, folder)]
    if unprotected:
        names = " and ".join(f"{folder}/" for folder in unprotected)
        return [Check("Project folder", FIX, f"Git would commit {names}.", f"Add {names} to .gitignore in the private repository.")]
    return [Check("Project folder", OK, f"{project.name}, with build/ and sources/ ignored by Git. Confirm the repository is private.")]


def _not_ignored(project: Path, folder: str) -> bool:
    result = run(["git", "check-ignore", "-q", f"{folder}/probe"], project)
    return result is None or result[0] != 0


# Config -------------------------------------------------------------------------------
def _url_problem(key: str, value) -> Optional[str]:
    """Describe a bad address without repeating it, because it could hold a credential."""
    if not isinstance(value, str):
        return f"{key} must be a URL."
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() != "https" or not parts.hostname:
        return f"{key} must be an https:// URL."
    if parts.username or parts.password or parts.query or parts.fragment:
        return f"{key} must be a plain address, with no credentials or query."
    return None


def validate_config(data) -> List[str]:
    """Return what's wrong with a config. Unknown keys are refused, so secrets can't be stored in it."""
    if not isinstance(data, dict):
        return ["The file must hold one JSON object."]
    problems = [f"'{key}' isn't a setting this file can hold. Remove it, and never store a secret here." for key in sorted(set(data) - CONFIG_KEYS, key=str)]
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        problems.append("targets must list at least one solution unique name.")
    elif not all(isinstance(name, str) and SOLUTION_NAME_RE.fullmatch(name) for name in targets):
        problems.append("targets must hold solution unique names: letters, digits and underscores.")
    addresses = {}
    for key, required in (("dev", True), ("prod", True), ("sandbox", False), ("release", False)):
        if key not in data:
            if required:
                problems.append(f"{key} is missing.")
            continue
        problem = _url_problem(key, data[key])
        if problem:
            problems.append(problem)
        else:
            addresses[key] = data[key].rstrip("/").lower()
    for first, second in (("dev", "prod"), ("sandbox", "dev"), ("sandbox", "prod"), ("release", "prod")):
        if first in addresses and addresses.get(first) == addresses.get(second):
            problems.append(f"{first} and {second} can't be the same environment.")
    includes = data.get("includes")
    if not isinstance(includes, dict) or not all(isinstance(includes.get(key), bool) for key in INCLUDE_KEYS):
        problems.append(f"includes must say true or false for each of: {', '.join(INCLUDE_KEYS)}.")
    elif set(includes) - set(INCLUDE_KEYS):
        problems.append(f"includes can only hold: {', '.join(INCLUDE_KEYS)}.")
    projects = data.get("azure_devops", [])
    if not isinstance(projects, list) or not all(_is_project(item) for item in projects):
        problems.append("azure_devops must be a list of plain organization and project names.")
    site_id = data.get("power_pages_site_id")
    if site_id is not None and not (isinstance(site_id, str) and GUID_RE.fullmatch(site_id)):
        problems.append("power_pages_site_id must be a GUID.")
    return problems


def _is_project(item) -> bool:
    if not isinstance(item, dict) or set(item) != {"organization", "project"}:
        return False
    return all(isinstance(value, str) and value.strip() and not re.search(r"[/?#%@:]", value) for value in item.values())


def check_config(path: Path) -> List[Check]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [Check("Config", FIX, f"{path} wasn't found.", "Create it from examples/audit-config.example.json.")]
    except (OSError, ValueError) as exc:
        return [Check("Config", FIX, f"{path} couldn't be read: {type(exc).__name__}.", "Fix the file, or create it again from examples/audit-config.example.json.")]
    problems = validate_config(data)
    if problems:
        return [Check("Config", FIX, problem, f"Fix this in {path.name}.") for problem in problems]
    includes = ", ".join(key for key in INCLUDE_KEYS if data["includes"][key]) or "only Dataverse solutions"
    lines = [
        f"Solutions {', '.join(data['targets'])}.", f"DEV {data['dev']}.", f"Production {data['prod']}.",
        f"Sandbox {data.get('sandbox', 'not set')}.", f"Includes {includes}.",
    ]
    return [Check("Config", OK, " ".join(lines))]


# Output -------------------------------------------------------------------------------
def collect(project: Path, config: Optional[Path]) -> List[Check]:
    checks = [check_python(), check_git(), check_azure_cli(project), check_pac()]
    checks += check_folder(project)
    if config is not None:
        checks += check_config(config)
    return checks


def show(checks: List[Check]) -> int:
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}".rstrip())
        if check.fix:
            print(f"       {check.fix}")
    needing_fixes = sum(1 for check in checks if check.status == FIX)
    print("Ready." if not needing_fixes else f"Things to fix: {needing_fixes}. Fix them, then run this again.")
    return 3 if needing_fixes else 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check that everything the audit needs is ready, before it runs.")
    parser.add_argument("--config", type=Path, help="Also validate this audit-config.json")
    parser.add_argument("--project", type=Path, default=Path("."), help="The customer's private project folder (default: this folder)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    make_output_safe()
    args = parse_args(argv)
    if not args.project.is_dir():
        print(f"Bad arguments: --project folder wasn't found: {args.project}", file=sys.stderr)
        return 2
    return show(collect(args.project.resolve(), args.config))


if __name__ == "__main__":
    sys.exit(main())
