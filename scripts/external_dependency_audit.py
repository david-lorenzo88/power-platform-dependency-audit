#!/usr/bin/env python3
"""Read-only audit of what a release needs from SharePoint and Azure Functions.

Dataverse doesn't track these dependencies, so a solution can import cleanly and still
fail when it runs. This script:

  1. Scans source folders for SharePoint sites, lists and columns, and for Azure Functions
     URLs, including values held in environment variables
  2. Uses a map file to pair each DEV site and function app with its production one
  3. Reads both environments through Microsoft Graph and Azure Resource Manager
  4. Reads the Azure Functions defined in each chosen source branch
  5. Reports what production lacks, what is hardcoded, and what deploying each branch would
     add to or remove from production

Every call is an HTTP GET. Function keys found in source are reported by location only;
their values are never stored. If a call fails, the row is Unknown; nothing is inferred.

Needs solution_dependency_audit.py in the same folder.

Exit codes
  0  nothing needs action
  1  the audit could not run
  2  bad arguments
  3  the audit ran and rows still need action (use this to fail a pipeline)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from solution_dependency_audit import (
    ApiError, AuditError, ReadOnlyClient, _md, _mermaid_text, _safe, _sources_markdown, _unsafe,
    classify_code, iter_text_files, load_sources_lock, read_explanations, split_command,
)

__version__ = "1.0.0"

IN_PRODUCTION = "In production"
NOT_IN_PRODUCTION = "Not in production"
DEV_ONLY = "DEV only"
NEW_IN_BRANCH = "New in branch"
REMOVED_BY_BRANCH = "Removed by branch"
UNKNOWN = "Unknown"
OPEN_STATUSES = {NOT_IN_PRODUCTION, DEV_ONLY, NEW_IN_BRANCH, REMOVED_BY_BRANCH, UNKNOWN}
STATUS_ORDER = {UNKNOWN: 0, REMOVED_BY_BRANCH: 1, NOT_IN_PRODUCTION: 2, NEW_IN_BRANCH: 3, DEV_ONLY: 4, IN_PRODUCTION: 5}

GRAPH_URL = "https://graph.microsoft.com"
ARM_URL = "https://management.azure.com"
ARM_API_VERSION = "2026-07-15"
DEFAULT_GRAPH_TOKEN = "az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv"
DEFAULT_ARM_TOKEN = "az account get-access-token --resource https://management.azure.com --query accessToken -o tsv"

URL_END = r"""[^\s"'<>?#\\@,;&)\]]"""
SITE_RE = re.compile(rf"https://([a-z0-9-]+\.sharepoint\.(?:com|us|de|cn))((?:/(?:sites|teams)/{URL_END}+?)?)(?=[/\s\"'<>?#\\@,;&)\]]|$)", re.I)
LIST_URL_RE = re.compile(r"""/lists/([^/\s"'?#<>&]+)""", re.I)
FUNCTION_KEY_RE = re.compile(r"(?:\?|&|&amp;|%(?:25)*3F|%(?:25)*26)code(?:=|%(?:25)*3D)", re.I)
HEADER_KEY_RE = re.compile(r"""x-functions-key["'\s:=,\]]+["']?[A-Za-z0-9_\-/+=]{20,}""", re.I)
QUERY_START_RE = re.compile(r"\?|&|%(?:25)*3F|%(?:25)*26", re.I)
GET_BY_TITLE_RE = re.compile(r"""lists/getbytitle\(\s*(?:'|%27|")([^'"%]+)""", re.I)
LIST_BY_GUID_RE = re.compile(r"""lists\(\s*(?:guid)?(?:'|%27)?\{?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})""", re.I)
ODATA_FILTER_COLUMN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z0-9_]+)?\s+(?:eq|ne|gt|ge|lt|le)\b")
FUNCTION_HOST_ENDINGS = (".azurewebsites.net", ".azurewebsites.us", ".chinacloudsites.cn")
FUNCTION_RE = re.compile(rf"https://((?:[a-z0-9-]+\.)+(?:azurewebsites\.(?:net|us)|chinacloudsites\.cn))(/{URL_END}*)?(\?[^\s\"'<>]*)?", re.I)
GUID_RE = re.compile(r"^\{?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}?$", re.I)
PARAMETER_RE = re.compile(r"parameters\('[^']*?\(([A-Za-z0-9_]+)\)'\)")
SITE_KEYS = ("dataset", "DatasetName", "siteUrl", "SiteUrl")
LIST_KEYS = ("table", "TableName", "listId", "ListId", "listName", "ListName")
# Local settings stay on a developer's machine and often hold keys, so they aren't read.
SKIPPED_FILES = {"local.settings.json", ".env", "secrets.json", "appsettings.development.json"}
SKIPPED_SUFFIXES = {".md"}
CSV_COLUMNS = [
    "id", "status", "system", "kind", "name", "parent", "hardcoded", "detail", "evidence",
    "reference_count", "explanation",
]


@dataclass
class SiteReference:
    site: str
    hardcoded: bool
    evidence: str
    lists: Set[str] = field(default_factory=set)
    columns: Dict[str, Set[str]] = field(default_factory=dict)
    carried_value: str = ""


@dataclass
class FunctionReference:
    host: str
    route: str
    hardcoded: bool
    evidence: str
    partial: bool = False


@dataclass
class Finding:
    system: str
    kind: str
    name: str
    status: str
    parent: str = ""
    hardcoded: str = ""
    detail: str = ""
    evidence: str = ""
    count: int = 1
    explanation: str = ""

    @property
    def stable_id(self) -> str:
        key = "|".join((self.system, self.kind, self.parent.lower(), self.name.lower()))
        return f"X-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"

    @property
    def closed_as(self) -> str:
        text = self.explanation.lower()
        if text.startswith("false positive"):
            return "False positive"
        # A hardcoded address needs a fix, whatever else a person has confirmed.
        if text.startswith("accepted") and self.hardcoded != "Yes":
            return "Accepted"
        return ""

    @property
    def is_open(self) -> bool:
        return not self.closed_as and (self.status in OPEN_STATUSES or self.hardcoded == "Yes")


def normalize_site(host: str, path: str) -> str:
    return f"https://{host.lower()}{(path or '').rstrip('/').lower()}"


def is_guid(value: str) -> bool:
    return bool(GUID_RE.match(value.strip()))


class Discovery:
    """Finds every SharePoint and Azure Functions reference in the scanned folders."""

    def __init__(self, aliases: Iterable[str]):
        self.sites: Dict[str, SiteReference] = {}
        self.functions: Dict[Tuple[str, str], FunctionReference] = {}
        self.runtime_values: List[str] = []
        self.key_locations: List[str] = []
        self.skipped: List[Tuple[str, str]] = []
        self.files_scanned = 0
        self.env_values: Dict[str, str] = {}
        self.env_carried: Dict[str, str] = {}
        self.env_files: Set[str] = set()
        self.references: List[Tuple[str, dict, str]] = []
        self.alias_re = None
        names = [re.escape(alias.lower()) for alias in aliases if alias]
        if names:
            self.alias_re = re.compile(rf"https://({'|'.join(names)})(/{URL_END}*)?(\?[^\s\"'<>]*)?", re.I)

    # Reading --------------------------------------------------------------------------
    def scan(self, roots: Iterable[Path]) -> None:
        documents: List[Tuple[str, str]] = []
        for root in roots:
            for label, text in iter_text_files(root, self.skipped):
                file_name = label.rsplit("/", 1)[-1].lower()
                if file_name in SKIPPED_FILES or file_name.startswith(".env.") or Path(file_name).suffix in SKIPPED_SUFFIXES:
                    continue
                self.files_scanned += 1
                documents.append((label, text.replace("\\/", "/")))
                self._read_environment_variable(label, text)
        for label, text in documents:
            self._scan_urls(label, text)
            if label.lower().endswith(".json"):
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                self._read_openapi(label, data)
                self._walk(label, data)

    def _read_environment_variable(self, label: str, text: str) -> None:
        parts = label.split("/")
        lowered = [part.lower() for part in parts]
        if "environmentvariabledefinitions" not in lowered:
            return
        index = lowered.index("environmentvariabledefinitions")
        if index + 2 >= len(parts):
            return
        schema = parts[index + 1]
        self.env_files.add(label)
        file_name = lowered[-1]
        if file_name == "environmentvariabledefinition.xml":
            match = re.search(r"<defaultvalue>(.*?)</defaultvalue>", text, re.S | re.I)
            if match:
                self.env_values.setdefault(schema.lower(), html.unescape(match.group(1).strip()))
        elif file_name == "environmentvariablevalues.json":
            try:
                value = _first_value(json.loads(text))
            except ValueError:
                value = ""
            if value:
                self.env_values[schema.lower()] = value
                self.env_carried[schema.lower()] = value

    def _scan_urls(self, label: str, text: str) -> None:
        literal = label not in self.env_files
        for number, line in enumerate(text.splitlines(), 1):
            if HEADER_KEY_RE.search(line):
                self.key_locations.append(f"{label}:{number}")
            for match in SITE_RE.finditer(line):
                reference = self._add_site(normalize_site(match.group(1), match.group(2)), literal, f"{label}:{number}")
                named_list = LIST_URL_RE.match(line[match.end():])
                if named_list:
                    list_name = urllib.parse.unquote(named_list.group(1)).lower()
                    reference.lists.add(list_name)
                    reference.columns.setdefault(list_name, set())
            for pattern in (FUNCTION_RE, self.alias_re):
                if pattern is None:
                    continue
                for match in pattern.finditer(line):
                    route = (match.group(2) or "").rstrip("/")
                    following = line[match.end():match.end() + 1]
                    if FUNCTION_KEY_RE.search(line[match.start():match.end() + 300]):
                        self.key_locations.append(f"{label}:{number}")
                    self._add_function(match.group(1).lower(), route, literal, f"{label}:{number}", following == "@")

    def _read_openapi(self, label: str, data) -> None:
        if not isinstance(data, dict) or not (data.get("swagger") or data.get("openapi")):
            return
        host = str(data.get("host") or "").lower()
        if not host:
            return
        is_function_host = host.endswith(FUNCTION_HOST_ENDINGS) or (self.alias_re is not None and self.alias_re.match(f"https://{host}"))
        if not is_function_host:
            return
        base = "/" + str(data.get("basePath") or "").strip("/")
        for path in (data.get("paths") or {}):
            route = (base.rstrip("/") + "/" + str(path).strip("/")).rstrip("/")
            if FUNCTION_KEY_RE.search(route):
                self.key_locations.append(f"{label} (custom connector)")
            self._add_function(host, route, True, f"{label} (custom connector)")

    def _walk(self, label: str, node) -> None:
        if isinstance(node, list):
            for item in node:
                self._walk(label, item)
            return
        if not isinstance(node, dict):
            return
        host = node.get("host")
        parameters = node.get("parameters")
        if isinstance(parameters, dict) and isinstance(host, dict) and "sharepointonline" in json.dumps(host).lower():
            action = str(host.get("operationId") or "action")
            self._read_sharepoint_values(f"{label}#{action}", parameters, is_action=True)
        elif any(key in node for key in ("DatasetName", "siteUrl", "SiteUrl")):
            self._read_sharepoint_values(label, node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                self._walk(label, value)

    @staticmethod
    def _list_from_request(values: dict) -> str:
        """Find the list in a Send an HTTP request action, or the library in a file action."""
        uri = next((values[key] for key in ("parameters/uri", "uri") if isinstance(values.get(key), str)), "")
        for pattern in (GET_BY_TITLE_RE, LIST_BY_GUID_RE):
            match = pattern.search(uri)
            if match:
                return urllib.parse.unquote(match.group(1))
        folder = next((values[key] for key in ("folderPath", "path") if isinstance(values.get(key), str)), "")
        if folder.startswith("/") and "@" not in folder:
            parts = [part for part in folder.split("/") if part]
            if parts:
                return urllib.parse.unquote(parts[0])
        return ""

    def _read_sharepoint_values(self, evidence: str, values: dict, is_action: bool = False) -> None:
        site_value = next((values[key] for key in SITE_KEYS if isinstance(values.get(key), str)), "")
        list_value = next((values[key] for key in LIST_KEYS if isinstance(values.get(key), str)), "")
        if not site_value:
            return
        site, site_literal = self._value(site_value)
        if not site:
            self.runtime_values.append(f"{evidence}: the site is set at runtime ({site_value[:60]})")
            return
        match = SITE_RE.match(site)
        if not match:
            self.runtime_values.append(f"{evidence}: the site value isn't a SharePoint URL")
            return
        site_url = normalize_site(match.group(1), match.group(2))
        reference = self._add_site(site_url, site_literal, evidence)
        schema = _schema_of(site_value)
        if schema and schema in self.env_carried:
            reference.carried_value = schema
        list_value = list_value or self._list_from_request(values)
        if not list_value:
            if is_action:
                self.runtime_values.append(f"{evidence}: the list or library in this action couldn't be read")
            return
        list_ref, _ = self._value(list_value)
        if not list_ref:
            self.runtime_values.append(f"{evidence}: the list is set at runtime ({list_value[:60]})")
            return
        list_ref = list_ref.strip("{}").lower()
        reference.lists.add(list_ref)
        columns = reference.columns.setdefault(list_ref, set())
        for key, value in values.items():
            if key.startswith("item/"):
                columns.add(key[5:].split("/", 1)[0])
            elif key in ("$select", "$orderby") and isinstance(value, str) and not value.startswith("@"):
                columns.update(part.split()[0].split("/", 1)[0] for part in value.split(",") if part.strip())
            elif key == "$filter" and isinstance(value, str):
                columns.update(ODATA_FILTER_COLUMN_RE.findall(re.sub(r"@\{.*?\}", "''", value)))

    def _value(self, raw: str) -> Tuple[str, bool]:
        """Return (value, is it literal). An expression with no known value returns an empty value."""
        raw = raw.strip()
        schema = _schema_of(raw)
        if schema:
            return self.env_values.get(schema, ""), False
        if raw.startswith("@") or "@{" in raw:
            return "", False
        return raw, True

    # Collecting -----------------------------------------------------------------------
    def _add_site(self, site: str, hardcoded: bool, evidence: str) -> SiteReference:
        reference = self.sites.get(site)
        if reference is None:
            reference = self.sites[site] = SiteReference(site, hardcoded, evidence)
        elif hardcoded and not reference.hardcoded:
            reference.hardcoded = True
            reference.evidence = evidence
        return reference

    def _add_function(self, host: str, route: str, hardcoded: bool, evidence: str, partial: bool = False) -> None:
        # Keep nothing after a query starts, so a function key can't reach the output.
        route = QUERY_START_RE.split(route, 1)[0].rstrip("/")
        key = (host, route.lower())
        existing = self.functions.get(key)
        if existing is None:
            self.functions[key] = FunctionReference(host, route, hardcoded, evidence, partial)
        elif hardcoded and not existing.hardcoded:
            existing.hardcoded = True
            existing.evidence = evidence


def _schema_of(raw: str) -> str:
    match = PARAMETER_RE.search(raw)
    return match.group(1).lower() if match else ""


def _first_value(node) -> str:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "value" and isinstance(value, str):
                return value
        for value in node.values():
            found = _first_value(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _first_value(item)
            if found:
                return found
    return ""


def route_segments(route: str) -> List[str]:
    return [segment for segment in route.lower().split("/") if segment]


def route_matches(reference: str, template: str) -> bool:
    actual, expected = route_segments(reference), route_segments(template)
    if len(actual) != len(expected):
        return False
    return all(a == b or a.startswith("{") or b.startswith("{") for a, b in zip(actual, expected))


# Reading the environments -------------------------------------------------------------
class SharePointReader:
    def __init__(self, client: ReadOnlyClient):
        self.client = client
        self._sites: Dict[str, Optional[dict]] = {}
        self._lists: Dict[str, List[dict]] = {}
        self._columns: Dict[Tuple[str, str], List[dict]] = {}

    def site(self, url: str) -> Optional[dict]:
        """Return the site, or None if it doesn't exist. Other failures raise ApiError."""
        if url not in self._sites:
            parts = urllib.parse.urlsplit(url)
            path = f"sites/{parts.netloc}:{parts.path}" if parts.path else f"sites/{parts.netloc}"
            try:
                self._sites[url] = self.client.get(path, {"$select": "id,displayName,webUrl"})
            except ApiError as exc:
                if exc.status != 404:
                    raise
                self._sites[url] = None
        return self._sites[url]

    def lists(self, site_id: str) -> List[dict]:
        if site_id not in self._lists:
            self._lists[site_id] = self.client.get_all(f"sites/{site_id}/lists", {"$select": "id,name,displayName,system"})
        return self._lists[site_id]

    def columns(self, site_id: str, list_id: str) -> List[dict]:
        key = (site_id, list_id)
        if key not in self._columns:
            self._columns[key] = self.client.get_all(
                f"sites/{site_id}/lists/{list_id}/columns", {"$select": "name,displayName,hidden,readOnly"}
            )
        return self._columns[key]

    def find_list(self, site_id: str, reference: str) -> Optional[dict]:
        wanted = reference.strip("{}").lower()
        for item in self.lists(site_id):
            names = (str(item.get("id", "")).lower(), str(item.get("displayName", "")).lower(), str(item.get("name", "")).lower())
            if wanted in names:
                return item
        return None


class FunctionReader:
    def __init__(self, client: ReadOnlyClient):
        self.client = client
        self._functions: Dict[str, Optional[List[dict]]] = {}

    def functions(self, app: dict) -> Optional[List[dict]]:
        """Return the app's functions, or None if the app doesn't exist."""
        key = f"{app['subscription']}/{app['resource_group']}/{app['name']}".lower()
        if key not in self._functions:
            path = (
                f"subscriptions/{app['subscription']}/resourceGroups/{app['resource_group']}"
                f"/providers/Microsoft.Web/sites/{app['name']}/functions?api-version={ARM_API_VERSION}"
            )
            try:
                self._functions[key] = self.client.get_all(path)
            except ApiError as exc:
                if exc.status != 404:
                    raise
                self._functions[key] = None
        return self._functions[key]


# Function definitions in source --------------------------------------------------------
FUNCTION_SOURCE_SUFFIXES = {".cs", ".fs", ".vb", ".java", ".py", ".js", ".mjs", ".ts"}
CSHARP_FUNCTION_RE = re.compile(
    r'''\[\s*(?:[\w.]+\.)?(?:Function|FunctionName)(?:Attribute)?\s*\(\s*(?:"([^"]+)"|nameof\(\s*(?:\w+\.)*(\w+)\s*\)|([\w.]+))'''
)
JAVA_FUNCTION_RE = re.compile(r'''@FunctionName\(\s*"([^"]+)"''')
PYTHON_NAME_RE = re.compile(r'''@\w+\.function_name\(\s*(?:name\s*=\s*)?["\']([^"\']+)''')
PYTHON_ROUTE_RE = re.compile(r'''@\w+\.(?:route|schedule|queue_trigger|blob_trigger|event_hub_message_trigger|service_bus_queue_trigger|service_bus_topic_trigger|cosmos_db_trigger|event_grid_trigger)\([^\n]*\)\s*(?:@[^\n]*\n\s*)*(?:async\s+)?def\s+(\w+)''')
NODE_FUNCTION_RE = re.compile(
    r'''\bapp\.(?:http|timer|storageQueue|storageBlob|serviceBusQueue|serviceBusTopic|eventHub|eventGrid|cosmosDB|get|post|put|patch|deleteRequest|generic)\(\s*["\']([^"\']+)'''
)


def read_function_definitions(folder: Path) -> Tuple[Set[str], List[str]]:
    """Return the function names defined in a folder, and places where a name can't be read."""
    names: Set[str] = set()
    unresolved: List[str] = []
    for label, text in iter_text_files(folder):
        if classify_code(label, text) in ("test", "generated"):
            continue
        parts = label.split("/")
        if parts[-1].lower() == "function.json":
            try:
                if len(parts) >= 2 and isinstance(json.loads(text).get("bindings"), list):
                    names.add(parts[-2].lower())
            except (ValueError, AttributeError):
                unresolved.append(label)
            continue
        suffix = Path(parts[-1]).suffix.lower()
        if suffix not in FUNCTION_SOURCE_SUFFIXES:
            continue
        if suffix in (".cs", ".fs", ".vb"):
            for match in CSHARP_FUNCTION_RE.finditer(text):
                if match.group(3):
                    line = text.count("\n", 0, match.start()) + 1
                    unresolved.append(f"{label}:{line}")
                else:
                    names.add((match.group(1) or match.group(2)).lower())
        elif suffix == ".java":
            names.update(name.lower() for name in JAVA_FUNCTION_RE.findall(text))
        elif suffix == ".py":
            explicit = PYTHON_NAME_RE.findall(text)
            names.update(name.lower() for name in explicit)
            if not explicit:
                names.update(name.lower() for name in PYTHON_ROUTE_RE.findall(text))
        else:
            names.update(name.lower() for name in NODE_FUNCTION_RE.findall(text))
    return names, unresolved


def function_name(item: dict) -> str:
    return str(item.get("name", "")).rsplit("/", 1)[-1]


def function_route(item: dict) -> str:
    template = (item.get("properties") or {}).get("invoke_url_template") or ""
    return urllib.parse.urlsplit(template).path if template else ""


# The audit ----------------------------------------------------------------------------
@dataclass
class Config:
    map_path: Path
    scan_dirs: List[Path]
    out: Path = Path("audit")
    site_pairs: List[dict] = field(default_factory=list)
    app_pairs: List[dict] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)


def load_map(path: Path) -> Tuple[List[dict], List[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AuditError(f"Could not read the map file: {exc}") from None
    sites = data.get("sharepoint_sites") or []
    apps = data.get("function_apps") or []
    for pair in sites:
        for role in ("dev", "prod"):
            if not str(pair.get(role, "")).lower().startswith("https://"):
                raise AuditError(f"sharepoint_sites needs an https:// {role} URL: {pair}")
    seen_sites = [str(pair["dev"]).rstrip("/").lower() for pair in sites] + [str(pair["prod"]).rstrip("/").lower() for pair in sites]
    if any(str(pair["dev"]).rstrip("/").lower() == str(pair["prod"]).rstrip("/").lower() for pair in sites):
        raise AuditError("A SharePoint pair uses the same site for DEV and production")
    if len(seen_sites) != len(set(seen_sites)):
        raise AuditError("A SharePoint site appears more than once in the map")
    for pair in apps:
        for role in ("dev", "prod"):
            app = pair.get(role) or {}
            if not all(app.get(key) for key in ("subscription", "resource_group", "name")):
                raise AuditError(f"function_apps needs subscription, resource_group and name for {role}: {pair}")
        if pair["dev"]["name"].lower() == pair["prod"]["name"].lower() and pair["dev"]["resource_group"].lower() == pair["prod"]["resource_group"].lower():
            raise AuditError(f"A function app pair uses the same app for DEV and production: {pair['dev']['name']}")
    return sites, apps


class ExternalAudit:
    def __init__(
        self, config: Config, sharepoint: ReadOnlyClient, azure: ReadOnlyClient,
        log: Callable[[str], None] = print,
    ):
        self.config = config
        self.sharepoint = SharePointReader(sharepoint)
        self.azure = FunctionReader(azure)
        self.sharepoint_client = sharepoint
        self.azure_client = azure
        self.log = log
        self.findings: Dict[str, Finding] = {}
        self.warnings: List[str] = []
        aliases = [alias for pair in config.app_pairs for alias in pair.get("aliases", [])]
        self.discovery = Discovery(aliases)
        self.referenced_routes: Dict[int, Set[str]] = {}
        self.branch_functions: Dict[int, Set[str]] = {}
        self.incomplete: List[str] = []

    def run(self) -> int:
        self.discovery.scan(self.config.scan_dirs)
        found = self.discovery
        self.log(
            f"Found {len(found.sites)} SharePoint sites and {len(found.functions)} function URLs "
            f"in {found.files_scanned} files"
        )
        self.audit_sharepoint()
        self.audit_functions()
        self.add_scan_findings()
        self.carry_explanations()
        self.write_outputs()
        return self.report()

    def add(self, finding: Finding) -> Finding:
        existing = self.findings.get(finding.stable_id)
        if existing is None:
            self.findings[finding.stable_id] = finding
            return finding
        existing.count += 1
        if finding.hardcoded == "Yes":
            existing.hardcoded = "Yes"
            existing.evidence = finding.evidence
        return existing

    # SharePoint -----------------------------------------------------------------------
    def _pair_for_site(self, site: str) -> Tuple[Optional[dict], str]:
        for pair in self.config.site_pairs:
            if site == str(pair["dev"]).rstrip("/").lower():
                return pair, "dev"
            if site == str(pair["prod"]).rstrip("/").lower():
                return pair, "prod"
        return None, ""

    def audit_sharepoint(self) -> None:
        for reference in self.discovery.sites.values():
            pair, role = self._pair_for_site(reference.site)
            flag = "Yes" if reference.hardcoded else "No"
            if pair is None:
                self.add(Finding(
                    "SharePoint", "Site", reference.site, UNKNOWN, hardcoded=flag, evidence=reference.evidence,
                    detail="This site isn't in the map file, so it can't be compared. Add its DEV and production pair.",
                ))
                continue
            self._audit_site(reference, pair, role, flag)

    @staticmethod
    def _graph_error(exc: ApiError) -> str:
        if exc.status in (401, 403):
            return f"{exc}. Microsoft Graph needs a token with Sites.Read.All or Sites.Selected: see --graph-token-command."
        return str(exc)

    def _audit_site(self, reference: SiteReference, pair: dict, role: str, flag: str) -> None:
        prod_url = str(pair["prod"]).rstrip("/").lower()
        dev_url = str(pair["dev"]).rstrip("/").lower()
        address = "DEV" if role == "dev" else "production"
        note = f"The {address} address is hardcoded. " if reference.hardcoded else ""
        if reference.carried_value:
            note += f"Environment variable {reference.carried_value} exports its current value with the solution. "
        try:
            prod_site = self.sharepoint.site(prod_url)
        except ApiError as exc:
            self.add(Finding("SharePoint", "Site", prod_url, UNKNOWN, hardcoded=flag, evidence=reference.evidence, detail=f"{note}Production check failed: {self._graph_error(exc)}"))
            return
        if prod_site is None:
            self.add(Finding("SharePoint", "Site", prod_url, NOT_IN_PRODUCTION, hardcoded=flag, evidence=reference.evidence, detail=f"{note}The production site wasn't found."))
            return
        self.add(Finding("SharePoint", "Site", prod_url, IN_PRODUCTION, hardcoded=flag, evidence=reference.evidence, detail=f"{note}Production site: {prod_site.get('displayName')}".strip()))
        for list_reference in sorted(reference.lists):
            self._audit_list(reference, list_reference, dev_url, prod_site, role)

    def _audit_list(self, reference: SiteReference, list_reference: str, dev_url: str, prod_site: dict, role: str) -> None:
        columns = reference.columns.get(list_reference, set())
        evidence = reference.evidence
        name = list_reference
        dev_site = None
        dev_list = None
        if role == "dev":
            try:
                dev_site = self.sharepoint.site(dev_url)
                dev_list = self.sharepoint.find_list(dev_site["id"], list_reference) if dev_site else None
            except ApiError as exc:
                self.add(Finding("SharePoint", "List", name, UNKNOWN, parent=dev_url, evidence=evidence, detail=f"DEV check failed: {self._graph_error(exc)}"))
                return
            if dev_list is None:
                self.add(Finding("SharePoint", "List", name, UNKNOWN, parent=dev_url, evidence=evidence, detail="The list wasn't found in the DEV site, so it can't be matched by name."))
                return
            name = str(dev_list.get("displayName") or list_reference)
        try:
            prod_list = self.sharepoint.find_list(prod_site["id"], name)
        except ApiError as exc:
            self.add(Finding("SharePoint", "List", name, UNKNOWN, parent=str(prod_site.get("webUrl", "")), evidence=evidence, detail=f"Production check failed: {exc}"))
            return
        parent = str(prod_site.get("webUrl", ""))
        if prod_list is None:
            self.add(Finding("SharePoint", "List", name, NOT_IN_PRODUCTION, parent=parent, evidence=evidence, detail="No production list has this display name."))
            return
        detail = "Found by display name. List IDs differ between sites, so use an environment variable." if is_guid(list_reference) and role == "dev" else "Found"
        self.add(Finding("SharePoint", "List", name, IN_PRODUCTION, parent=parent, evidence=evidence, detail=detail))
        self._audit_columns(name, columns, dev_site, dev_list, prod_site, prod_list, evidence)

    def _audit_columns(self, list_name, wanted, dev_site, dev_list, prod_site, prod_list, evidence) -> None:
        parent = f"{prod_site.get('webUrl', '')} / {list_name}"
        try:
            prod_columns = {str(c.get("name")).lower(): c for c in self.sharepoint.columns(prod_site["id"], prod_list["id"])}
        except ApiError as exc:
            self.add(Finding("SharePoint", "Column", "All columns", UNKNOWN, parent=parent, evidence=evidence, detail=f"Production check failed: {exc}"))
            return
        for column in sorted(wanted):
            if column.lower() not in prod_columns:
                self.add(Finding("SharePoint", "Column", column, NOT_IN_PRODUCTION, parent=parent, evidence=evidence, detail="Used by the target, but missing from the production list."))
        if not dev_site or not dev_list:
            return
        try:
            dev_columns = self.sharepoint.columns(dev_site["id"], dev_list["id"])
        except ApiError as exc:
            self.add(Finding("SharePoint", "Column", "All columns", UNKNOWN, parent=parent, evidence=evidence, detail=f"DEV check failed: {exc}"))
            return
        lowered = {column.lower() for column in wanted}
        for column in dev_columns:
            if column.get("hidden"):
                continue
            key = str(column.get("name")).lower()
            match = prod_columns.get(key)
            if match is None and key not in lowered:
                self.add(Finding("SharePoint", "Column", str(column.get("name")), DEV_ONLY, parent=parent, evidence=evidence, detail="In the DEV list only, and no scanned source uses it. Confirm which project it belongs to."))
            elif match is not None and str(match.get("displayName")).lower() != str(column.get("displayName")).lower():
                self.add(Finding(
                    "SharePoint", "Column", str(column.get("name")), UNKNOWN, parent=parent, evidence=evidence,
                    detail=f"The display name differs: DEV '{column.get('displayName')}', production '{match.get('displayName')}'. Environment variables need them to match.",
                ))

    # Azure Functions ------------------------------------------------------------------
    def _pair_for_host(self, host: str) -> Tuple[Optional[dict], str]:
        host = host.lower()
        for pair in self.config.app_pairs:
            if host in [str(alias).lower() for alias in pair.get("aliases", [])]:
                return pair, "dev"
            for role in ("dev", "prod"):
                name = str(pair[role]["name"]).lower()
                first_label = host.split(".", 1)[0]
                if first_label == name or (first_label.startswith(f"{name}-") and host.count(".") > 2):
                    return pair, role
        return None, ""

    def read_branches(self) -> None:
        """Read the functions defined in each branch that deploys to a mapped function app."""
        for source in self.config.sources:
            if str(source.get("kind", "")).lower() != "functions":
                continue
            name = str(source.get("name"))
            app = str(source.get("function_app") or "").lower()
            index = next((i for i, pair in enumerate(self.config.app_pairs) if app and app in (str(pair["dev"]["name"]).lower(), str(pair["prod"]["name"]).lower())), None)
            evidence = f"{name} @ {source.get('branch')} ({str(source.get('commit'))[:12]})"
            if index is None:
                detail = "Name the function app this branch deploys to, and add the app to the map file." if not app else f"Function app '{app}' isn't in the map file."
                self.add(Finding("Azure Functions", "Source branch", name, UNKNOWN, evidence=evidence, detail=detail))
                continue
            names, unresolved = read_function_definitions(Path(str(source.get("folder"))))
            for location in unresolved:
                self.add(Finding("Azure Functions", "Function name in code", location, UNKNOWN, parent=name, evidence=location, detail="The name comes from a constant, so it can't be read. Check this function by hand."))
            if not names and not unresolved:
                self.add(Finding("Azure Functions", "Source branch", name, UNKNOWN, evidence=evidence, detail="No function definitions were found in this branch, so it can't be compared with production."))
                continue
            self.branch_functions.setdefault(index, set()).update(names)

    def audit_functions(self) -> None:
        self.read_branches()
        for reference in self.discovery.functions.values():
            pair, role = self._pair_for_host(reference.host)
            flag = "Yes" if reference.hardcoded else "No"
            label = f"{reference.host}{reference.route}"
            if pair is None:
                self.add(Finding(
                    "Azure Functions", "Function", label, UNKNOWN, hardcoded=flag, evidence=reference.evidence,
                    detail="This host isn't in the map file, so it can't be compared. Add its DEV and production pair.",
                ))
                continue
            index = self.config.app_pairs.index(pair)
            self.referenced_routes.setdefault(index, set())
            self._audit_function(reference, pair, role, flag, index)
        for index, pair in enumerate(self.config.app_pairs):
            if index in self.branch_functions:
                self._audit_branch_contents(pair, self.branch_functions[index], self.referenced_routes.get(index, set()))
            else:
                self._audit_app_contents(pair, self.referenced_routes.get(index, set()))

    def _audit_branch_contents(self, pair: dict, branch: Set[str], referenced: Set[str]) -> None:
        app_name = str(pair["prod"]["name"])
        try:
            prod_functions = self.azure.functions(pair["prod"])
        except ApiError as exc:
            self.add(Finding("Azure Functions", "Function app", app_name, UNKNOWN, evidence=f"Azure: {app_name}", detail=f"Could not compare the branch with production: {exc}"))
            return
        if prod_functions is None:
            self.add(Finding("Azure Functions", "Function app", app_name, NOT_IN_PRODUCTION, evidence=f"Azure: {app_name}", detail="The production function app wasn't found."))
            return
        if not prod_functions:
            self.add(Finding("Azure Functions", "Function app", app_name, UNKNOWN, evidence=f"Azure: {app_name}", detail="The production app returned no functions. It may be stopped, so the branch can't be compared with it."))
            return
        production = {function_name(item).lower() for item in prod_functions}
        for name in sorted(production - branch):
            self.add(Finding(
                "Azure Functions", "Function", name, REMOVED_BY_BRANCH, parent=app_name, evidence=f"Azure: {app_name}",
                detail="Production has this function, but the chosen branch doesn't define it. Deploying the branch would remove it.",
            ))
        for name in sorted(branch - production - referenced):
            self.add(Finding(
                "Azure Functions", "Function", name, NEW_IN_BRANCH, parent=app_name, evidence="Source branch",
                detail="The chosen branch adds this function, and no scanned source calls it. A function app deploys as a whole, so confirm it belongs in this release.",
            ))

    def _audit_function(self, reference: FunctionReference, pair: dict, role: str, flag: str, index: int) -> None:
        app_name = str(pair["prod"]["name"])
        address = "DEV" if role == "dev" else "production"
        note = f"The {address} address is hardcoded. " if reference.hardcoded else ""
        try:
            prod_functions = self.azure.functions(pair["prod"])
            dev_functions = self.azure.functions(pair["dev"])
        except ApiError as exc:
            self.add(Finding("Azure Functions", "Function", reference.route or app_name, UNKNOWN, parent=app_name, hardcoded=flag, evidence=reference.evidence, detail=f"{note}Azure check failed: {exc}"))
            return
        if prod_functions is None:
            self.add(Finding("Azure Functions", "Function app", app_name, NOT_IN_PRODUCTION, hardcoded=flag, evidence=reference.evidence, detail=f"{note}The production function app wasn't found."))
            return
        if not reference.route:
            self.add(Finding("Azure Functions", "Function app", app_name, IN_PRODUCTION, hardcoded=flag, evidence=reference.evidence, detail=note.strip() or "Found"))
            return
        if reference.partial:
            self.add(Finding(
                "Azure Functions", "Function", f"{reference.route}/...", UNKNOWN, parent=app_name, hardcoded=flag, evidence=reference.evidence,
                detail=f"{note}Part of this address is set at runtime, so it can't be matched. Check by hand which functions it calls.",
            ))
            return
        for item in dev_functions or []:
            if function_route(item) and route_matches(reference.route, function_route(item)):
                self.referenced_routes[index].add(function_name(item).lower())
        match = next((item for item in prod_functions if function_route(item) and route_matches(reference.route, function_route(item))), None)
        if match is not None:
            self.referenced_routes[index].add(function_name(match).lower())
            disabled = bool((match.get("properties") or {}).get("isDisabled"))
            status = NOT_IN_PRODUCTION if disabled else IN_PRODUCTION
            detail = f"{note}Function {function_name(match)}" + (" is disabled in production." if disabled else "")
            self.add(Finding("Azure Functions", "Function", reference.route, status, parent=app_name, hardcoded=flag, evidence=reference.evidence, detail=detail))
            return
        in_dev = any(function_route(item) and route_matches(reference.route, function_route(item)) for item in dev_functions or [])
        if in_dev:
            dev_match = next(item for item in dev_functions or [] if function_route(item) and route_matches(reference.route, function_route(item)))
            branch = self.branch_functions.get(index)
            missing = " The chosen branch doesn't define it either." if branch is not None and function_name(dev_match).lower() not in branch else ""
            self.add(Finding("Azure Functions", "Function", reference.route, NOT_IN_PRODUCTION, parent=app_name, hardcoded=flag, evidence=reference.evidence, detail=f"{note}Deployed in DEV, but not in production.{missing}"))
        else:
            self.add(Finding("Azure Functions", "Function", reference.route, UNKNOWN, parent=app_name, hardcoded=flag, evidence=reference.evidence, detail=f"{note}This route wasn't found in DEV or production. Check for a custom route or a proxy."))

    def _audit_app_contents(self, pair: dict, referenced: Set[str]) -> None:
        app_name = str(pair["prod"]["name"])
        try:
            prod_functions = self.azure.functions(pair["prod"])
            dev_functions = self.azure.functions(pair["dev"])
        except ApiError as exc:
            self.add(Finding("Azure Functions", "Function app", app_name, UNKNOWN, evidence=f"Azure: {app_name}", detail=f"Could not compare DEV with production: {exc}"))
            return
        if prod_functions is None:
            self.add(Finding("Azure Functions", "Function app", app_name, NOT_IN_PRODUCTION, evidence=f"Azure: {app_name}", detail="The production function app wasn't found."))
            return
        if not dev_functions:
            self.add(Finding(
                "Azure Functions", "Function app", str(pair["dev"]["name"]), UNKNOWN, evidence=f"Azure: {pair['dev']['name']}",
                detail="The DEV app wasn't found or returned no functions, so it can't be compared with production. Check that it's running.",
            ))
            return
        in_production = {function_name(item).lower() for item in prod_functions}
        for item in dev_functions:
            name = function_name(item)
            if name.lower() in in_production or name.lower() in referenced:
                continue
            self.add(Finding(
                "Azure Functions", "Function", name, DEV_ONLY, parent=app_name, evidence=f"Azure: {pair['dev']['name']}",
                detail="In DEV only, and no scanned source calls it. A function app deploys as a whole, so this ships if DEV's code is deployed.",
            ))

    # Scan findings --------------------------------------------------------------------
    def add_scan_findings(self) -> None:
        for location in self.discovery.key_locations:
            self.add(Finding(
                "Azure Functions", "Function key in source", location, UNKNOWN, hardcoded="Yes", evidence=location,
                detail="A URL here includes a function key. The value wasn't stored. Move it to a secure setting and rotate the key.",
            ))
        for label, reason in self.discovery.skipped:
            self.add(Finding("Scan", "File not searched", label, UNKNOWN, evidence=label, detail=reason))
        for item in self.config.sources:
            if item.get("submodules_skipped"):
                self.add(Finding(
                    "Scan", "Submodules not searched", str(item.get("name")), UNKNOWN, evidence=str(item.get("url")),
                    detail="This repository uses Git submodules, which weren't downloaded. Scan them too, or confirm they hold nothing that ships.",
                ))
        for message in self.discovery.runtime_values:
            location, _, detail = message.partition(": ")
            self.add(Finding("SharePoint", "Runtime value", location, UNKNOWN, evidence=location, detail=f"{detail[:1].upper()}{detail[1:]}. Check by hand which site and list it uses in production."))

    # Output ---------------------------------------------------------------------------
    def carry_explanations(self) -> None:
        previous = read_explanations(self.config.out / "external-dependencies.csv", self.warnings)
        for finding in self.findings.values():
            finding.explanation = _unsafe(previous.get(finding.stable_id, "") or "")

    def ordered(self) -> List[Finding]:
        return sorted(
            self.findings.values(),
            key=lambda f: (STATUS_ORDER.get(f.status, 9), f.hardcoded != "Yes", f.system, f.parent.lower(), f.name.lower()),
        )

    def counts(self) -> Counter:
        counter: Counter = Counter()
        for finding in self.findings.values():
            counter[finding.closed_as or finding.status] += 1
        return counter

    def gate_blockers(self) -> List[str]:
        blockers = list(self.incomplete)
        if not self.discovery.files_scanned:
            blockers.append("The scan read no files.")
        unknown = sum(1 for f in self.findings.values() if f.status == UNKNOWN and not f.explanation)
        if unknown:
            blockers.append(f"{unknown} Unknown rows have no explanation.")
        return blockers

    def write_outputs(self) -> None:
        self.config.out.mkdir(parents=True, exist_ok=True)
        with (self.config.out / "external-dependencies.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for finding in self.ordered():
                writer.writerow({key: _safe(value) for key, value in {
                    "id": finding.stable_id, "status": finding.status, "system": finding.system, "kind": finding.kind,
                    "name": finding.name, "parent": finding.parent, "hardcoded": finding.hardcoded,
                    "detail": finding.detail, "evidence": finding.evidence, "reference_count": finding.count,
                    "explanation": finding.explanation,
                }.items()})
        (self.config.out / "external-map.md").write_text(self._markdown(), encoding="utf-8")

    def _markdown(self) -> str:
        counts = self.counts()
        open_rows = [finding for finding in self.ordered() if finding.is_open]
        hardcoded = sum(1 for finding in open_rows if finding.hardcoded == "Yes")
        lines = [
            "# External dependencies: SharePoint and Azure Functions", "",
            f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC by external_dependency_audit {__version__}.",
            f"Scanned {self.discovery.files_scanned} files.", "",
            "## Status", "", "| Status | Rows |", "| --- | --- |",
        ]
        for status in (UNKNOWN, REMOVED_BY_BRANCH, NOT_IN_PRODUCTION, NEW_IN_BRANCH, DEV_ONLY, "False positive", "Accepted", IN_PRODUCTION):
            lines.append(f"| {status} | {counts.get(status, 0)} |")
        lines.append(f"| Hardcoded addresses still open | {hardcoded} |")
        blockers = self.gate_blockers()
        lines += ["", "## Gate 2", ""] + (["Ready."] if not blockers else [f"- {_md(item)}" for item in blockers])
        lines += _sources_markdown(self.config.sources)
        lines += ["", "## Rows that need action", ""]
        if open_rows:
            lines += ["| Status | System | Kind | Name | Parent | Hardcoded | Detail |", "| --- | --- | --- | --- | --- | --- | --- |"]
            lines += [
                f"| {f.status} | {f.system} | {f.kind} | {_md(f.name)} | {_md(f.parent)} | {f.hardcoded} | {_md(f.detail)} |"
                for f in open_rows
            ]
        else:
            lines.append("None.")
        lines += ["", "## Structure", ""] + self._mermaid(open_rows)
        if self.warnings:
            lines += ["", "## Warnings", ""] + [f"- {_md(item)}" for item in self.warnings]
        return "\n".join(lines) + "\n"

    def _mermaid(self, rows: List[Finding]) -> List[str]:
        if not rows:
            return ["No open rows to draw."]
        boxes: Dict[str, List[Tuple[str, Finding]]] = {}
        for index, finding in enumerate(rows[:150], 1):
            box = finding.parent or finding.system
            boxes.setdefault(box, []).append((f"n{index}", finding))
        lines = ["```mermaid", "flowchart LR", '  source["Scanned source"]']
        for index, (box, items) in enumerate(sorted(boxes.items()), 1):
            lines.append(f'  subgraph b{index}["{_mermaid_text(box)}"]')
            for node, finding in items:
                style = {NOT_IN_PRODUCTION: ":::missing", REMOVED_BY_BRANCH: ":::missing", DEV_ONLY: ":::devonly", NEW_IN_BRANCH: ":::devonly", UNKNOWN: ":::unknown"}.get(finding.status, ":::hardcoded")
                lines.append(f'    {node}["{_mermaid_text(finding.kind + ": " + finding.name)}"]{style}')
            lines.append("  end")
        for items in boxes.values():
            for node, finding in items:
                if finding.status not in (DEV_ONLY, NEW_IN_BRANCH, REMOVED_BY_BRANCH):
                    lines.append(f"  source --> {node}")
        lines += [
            "  classDef missing fill:#fde2e1,stroke:#b42318,color:#111",
            "  classDef devonly fill:#fff1cc,stroke:#b54708,color:#111",
            "  classDef unknown fill:#e8e8e8,stroke:#555,color:#111",
            "  classDef hardcoded fill:#dbeafe,stroke:#1d4ed8,color:#111",
            "```", "",
            "Red is missing or would be removed, amber is DEV only or new in the branch, grey is Unknown and blue is in production but hardcoded. Items with no arrow aren't used by any scanned source.",
        ]
        if len(rows) > 150:
            lines.append(f"The diagram shows the first 150 of {len(rows)} open rows.")
        return lines

    def report(self) -> int:
        counts = self.counts()
        self.log("Rows: " + (", ".join(f"{status} {count}" for status, count in sorted(counts.items())) or "none"))
        if self.discovery.key_locations:
            self.log(f"Security: {len(self.discovery.key_locations)} function keys found in source. See the CSV for locations.")
        for warning in self.warnings:
            self.log(f"Warning: {warning}")
        blockers = self.gate_blockers()
        self.log("Gate 2: ready" if not blockers else "Gate 2: not ready. " + " ".join(blockers))
        self.log(f"Output: {self.config.out}")
        return 3 if blockers or any(finding.is_open for finding in self.findings.values()) else 0


# Command line -------------------------------------------------------------------------
def token_from_command(command: str) -> Callable[[str], str]:
    def provider(_base_url: str) -> str:
        parts = split_command(command)
        if not parts:
            raise AuditError("The token command is empty")
        executable = shutil.which(parts[0]) or parts[0]
        try:
            result = subprocess.run([executable] + parts[1:], capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AuditError(f"Could not run the token command '{parts[0]}': {exc}") from None
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            raise AuditError(f"The token command '{parts[0]}' failed: {result.stderr.strip()[:200]}")
        return token

    return provider


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit of SharePoint and Azure Functions references.")
    parser.add_argument("--map", type=Path, required=True, help="JSON file pairing DEV and production sites and apps")
    parser.add_argument("--scan-dir", type=Path, action="append", default=[], help="Folder to search. Repeat as needed")
    parser.add_argument("--sources-lock", type=Path, help="sources-lock.json from fetch_audit_sources.py. Adds each branch to the scan")
    parser.add_argument("--out", type=Path, default=Path("audit"), help="Output folder (default: audit)")
    parser.add_argument("--graph-token-command", default=DEFAULT_GRAPH_TOKEN, help="Command that prints a Microsoft Graph token")
    parser.add_argument("--azure-token-command", default=DEFAULT_ARM_TOKEN, help="Command that prints an Azure Resource Manager token")
    parser.add_argument("--graph-url", default=GRAPH_URL, help="Microsoft Graph address, for sovereign clouds")
    parser.add_argument("--azure-url", default=ARM_URL, help="Azure Resource Manager address, for sovereign clouds")
    parser.add_argument("--check", action="store_true", help="Show the sites and apps that will be read, then stop")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def check(config: Config, sharepoint: SharePointReader, azure: FunctionReader) -> int:
    failed = False
    for pair in config.site_pairs:
        for role in ("dev", "prod"):
            url = str(pair[role]).rstrip("/").lower()
            try:
                site = sharepoint.site(url)
                print(f"SharePoint {role}: {url} ({site.get('displayName') if site else 'NOT FOUND'})")
            except ApiError as exc:
                failed = True
                print(f"SharePoint {role}: {url} (FAILED: {exc})")
    for pair in config.app_pairs:
        for role in ("dev", "prod"):
            name = pair[role]["name"]
            try:
                functions = azure.functions(pair[role])
                print(f"Function app {role}: {name} ({'NOT FOUND' if functions is None else str(len(functions)) + ' functions'})")
            except ApiError as exc:
                failed = True
                print(f"Function app {role}: {name} (FAILED: {exc})")
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        for name, url in (("--graph-url", args.graph_url), ("--azure-url", args.azure_url)):
            if not url.lower().startswith("https://"):
                raise AuditError(f"{name} must be an https:// URL")
        for path in args.scan_dir:
            if not path.is_dir():
                raise AuditError(f"--scan-dir folder wasn't found: {path}")
        if not args.scan_dir and not args.sources_lock and not args.check:
            raise AuditError("Pass at least one --scan-dir or --sources-lock")
    except AuditError as exc:
        print(f"Bad arguments: {exc}", file=sys.stderr)
        return 2
    try:
        sources = load_sources_lock(args.sources_lock) if args.sources_lock else []
        scan_dirs = args.scan_dir + [Path(item["folder"]) for item in sources]
        site_pairs, app_pairs = load_map(args.map)
        sharepoint = ReadOnlyClient(args.graph_url, token_from_command(args.graph_token_command), "Microsoft Graph", api_root="/v1.0/")
        azure = ReadOnlyClient(args.azure_url, token_from_command(args.azure_token_command), "Azure", api_root="/")
        config = Config(args.map, scan_dirs, args.out, site_pairs, app_pairs, sources)
        if args.check:
            return check(config, SharePointReader(sharepoint), FunctionReader(azure))
        return ExternalAudit(config, sharepoint, azure).run()
    except AuditError as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
