#!/usr/bin/env python3
"""Dry-run the import of a managed solution on a sandbox, and save the result for the audit.

This is the one script in this repository that isn't read-only. It sends the file to
StageSolution, which validates it and reports what the environment is missing. Staging
doesn't make the solution available to anyone, but it does upload the file, so use a
throwaway sandbox made from a copy of production.

The script refuses to run on DEV or production, including through another URL for the
same organization. It also refuses a solution file that isn't managed, and it refuses to
run if DEV and production are the same, because then it can't tell what production is.

Needs only the Python standard library, and solution_dependency_audit.py in the same
folder. It signs in with the Azure CLI, or with auth.py from the Dataverse plugin.

Exit codes
  0  the dry run passed
  1  the dry run could not run, and any earlier result file was removed
  2  bad arguments
  3  the dry run ran and didn't pass. The result file is written, so give it to the audit
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from solution_dependency_audit import (
    API_ROOT, ApiError, AuditError, ReadOnlyClient, _NoRedirect, _error_message, _retry_delay,
    load_auth, norm_guid,
)

__version__ = "1.0.0"

STAGE_TIMEOUT_SECONDS = 3600
RETRIES = 3


@dataclass
class SolutionFile:
    path: Path
    unique_name: str
    version: str


def read_solution_file(path: Path) -> SolutionFile:
    """Read the name and version from the zip, and refuse anything but a managed solution."""
    try:
        with zipfile.ZipFile(path) as archive:
            entry = next((name for name in archive.namelist() if name.lower() == "solution.xml"), None)
            if entry is None:
                raise AuditError(f"{path} has no solution.xml, so it isn't a solution file")
            manifest = ElementTree.fromstring(archive.read(entry)).find("SolutionManifest")
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise AuditError(f"Could not read {path}: {exc}") from None
    if manifest is None:
        raise AuditError(f"{path} has no solution manifest, so it isn't a solution file")
    name = (manifest.findtext("UniqueName") or "").strip()
    version = (manifest.findtext("Version") or "").strip()
    if not name:
        raise AuditError(f"{path} doesn't name its solution")
    if (manifest.findtext("Managed") or "").strip() != "1":
        raise AuditError(f"{path} isn't a managed solution. Export it with --managed true, because production gets the managed file.")
    return SolutionFile(path, name, version)


def organization_of(client: ReadOnlyClient) -> Dict[str, str]:
    try:
        organization_id = norm_guid(client.get("WhoAmI").get("OrganizationId"))
        name = client.get(f"organizations({organization_id})", {"$select": "name"}).get("name")
    except ApiError as exc:
        raise AuditError(f"Could not read {client.label} ({client.base_url}): {exc}") from None
    return {"id": organization_id, "name": str(name or "")}


def confirm_sandbox(sandbox: ReadOnlyClient, dev: ReadOnlyClient, prod: ReadOnlyClient, allow_same_org_id: bool = False) -> Dict[str, str]:
    """Stop unless the sandbox, DEV and production are three different environments."""
    if dev.base_url.lower() == prod.base_url.lower():
        raise AuditError("--dev and --prod are the same environment, so the script can't tell what production is")
    for other in (dev, prod):
        if sandbox.base_url.lower() == other.base_url.lower():
            raise AuditError(f"--sandbox and --{other.label.lower()} are the same environment")
    organization = organization_of(sandbox)
    dev_organization = organization_of(dev)
    prod_organization = organization_of(prod)
    if allow_same_org_id:
        return organization
    hint = "If the admin center shows they're separate environments, add --allow-same-org-id."
    if dev_organization["id"] == prod_organization["id"]:
        raise AuditError(f"--dev and --prod point to the same organization. {hint}")
    for other, other_organization in ((dev, dev_organization), (prod, prod_organization)):
        if other_organization["id"] == organization["id"]:
            raise AuditError(f"--sandbox and --{other.label.lower()} point to the same organization. {hint}")
    return organization


def stage(
    base_url: str, solution: SolutionFile, headers: Dict[str, str], sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Send the one POST this repository makes. Redirects are refused so the token can't travel."""
    url = base_url.rstrip("/") + API_ROOT + "StageSolution"
    body = json.dumps({"CustomizationFile": base64.b64encode(solution.path.read_bytes()).decode("ascii")}).encode("utf-8")
    opener = urllib.request.build_opener(_NoRedirect)
    request_headers = dict(headers)
    request_headers.update({
        "Accept": "application/json", "Content-Type": "application/json",
        "OData-MaxVersion": "4.0", "OData-Version": "4.0",
    })
    for attempt in range(RETRIES + 1):
        request = urllib.request.Request(url, data=body, method="POST", headers=request_headers)
        try:
            with opener.open(request, timeout=STAGE_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < RETRIES:
                sleep(_retry_delay(exc.headers.get("Retry-After"), attempt))
                continue
            raise AuditError(f"StageSolution failed with HTTP {exc.code}: {_error_message(exc.read())}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            raise AuditError(f"Could not reach the sandbox: {exc}. Run the script again.") from None
        except ValueError as exc:
            raise AuditError(f"StageSolution's response wasn't JSON: {exc}") from None
    raise AuditError("StageSolution didn't complete")


def save_result(result: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise AuditError(f"The dry run finished, but its result couldn't be saved to {path}: {exc}") from None


def remove_earlier_result(path: Path) -> None:
    """A failed run must not leave an older passing result for the audit to read."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AuditError(f"Could not remove the earlier result {path}: {exc}") from None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run the import of a managed solution on a sandbox.")
    parser.add_argument("--zip", type=Path, required=True, help="The managed solution file")
    parser.add_argument("--sandbox", required=True, help="URL of a throwaway sandbox, copied from production")
    parser.add_argument("--dev", required=True, help="DEV environment URL. The script refuses to run on it")
    parser.add_argument("--prod", required=True, help="Production environment URL. The script refuses to run on it")
    parser.add_argument("--out", type=Path, help="Result file (default: audit/stage-<solution>.json)")
    parser.add_argument("--auth-dir", help="Folder holding auth.py from the Dataverse plugin (default: ./scripts if it's there, or else the Azure CLI)")
    parser.add_argument("--check", action="store_true", help="Show the solution and the sandbox, then stop without sending anything")
    parser.add_argument(
        "--allow-same-org-id", action="store_true",
        help="Run even if the sandbox reports the same organization ID as DEV or production. Only use it once the admin center shows they're separate environments",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def validate(args: argparse.Namespace) -> None:
    for name, url in (("--sandbox", args.sandbox), ("--dev", args.dev), ("--prod", args.prod)):
        if not url.lower().startswith("https://"):
            raise AuditError(f"{name} must be an https:// URL")
    if not args.zip.is_file():
        raise AuditError(f"--zip file wasn't found: {args.zip}")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        validate(args)
    except AuditError as exc:
        print(f"Bad arguments: {exc}", file=sys.stderr)
        return 2
    try:
        solution = read_solution_file(args.zip)
        os.environ.setdefault("DATAVERSE_URL", args.sandbox)
        token_provider, header_builder = load_auth(args.auth_dir)
        sandbox = ReadOnlyClient(args.sandbox, token_provider, "SANDBOX", header_builder)
        dev = ReadOnlyClient(args.dev, token_provider, "DEV", header_builder)
        prod = ReadOnlyClient(args.prod, token_provider, "PROD", header_builder)
        organization = confirm_sandbox(sandbox, dev, prod, args.allow_same_org_id)
        print(f"Solution: {solution.unique_name} {solution.version} (managed)")
        print(f"Sandbox:  {sandbox.base_url} ({organization['name']})")
        if args.check:
            return 0
        out = args.out or Path("audit") / f"stage-{solution.unique_name}.json"
        remove_earlier_result(out)
        token = token_provider(sandbox.base_url)
        headers = dict(header_builder(token)) if header_builder else {"Authorization": f"Bearer {token}"}
        result = stage(sandbox.base_url, solution, headers)
        results = result.get("StageSolutionResults") if isinstance(result, dict) else None
        if not isinstance(results, dict):
            raise AuditError("StageSolution's response had no StageSolutionResults")
        save_result(result, out)
        status = results.get("StageSolutionStatus")
        missing = results.get("MissingDependencies") or []
        print(f"Status:   {status}. Missing dependencies: {len(missing)}")
        print(f"Saved:    {out}")
        return 0 if status in ("Passed", 1) else 3
    except AuditError as exc:
        print(f"Dry run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
