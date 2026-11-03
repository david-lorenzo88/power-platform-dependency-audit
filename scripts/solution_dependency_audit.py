#!/usr/bin/env python3
"""Read-only dependency audit for a release made of one or more Dataverse solutions.

Answers one question: what do the target solutions need that production doesn't already
have? A component held by another target isn't a problem, but it does fix the order they
must be imported in, so the audit also works out a release order and reports any circular
dependency. It merges these sources into one list:

  graph  RetrieveMissingDependencies for each target, then RetrieveRequiredComponents to walk each chain
  text   A scan of source folders (the unpacked target, a downloaded Power Pages site,
         and the Azure DevOps branches holding plug-ins and Azure Functions) for names that
         exist only in other unmanaged solutions
  code   For plug-in assemblies in the target, a comparison of the types registered in
         DEV and production with the classes in the scanned branches
  stage  Saved StageSolution responses (--stage-json, one per target), produced separately on a sandbox

Every Dataverse call is an HTTP GET. The client has no method for any other verb, so
this script can't change an environment. If a call fails, the row is reported as
Unknown; the script never infers a result.

Needs only the Python standard library. It signs in with the Azure CLI, or with auth.py
from the Dataverse plugin when that file is available.

Exit codes
  0  no In-flight, Version too old or Unknown rows
  1  the audit could not run
  2  bad arguments
  3  the audit ran and rows still need action (use this to fail a pipeline)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import http.client
import importlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

__version__ = "1.0.0"

API_ROOT = "/api/data/v9.2/"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
IGNORED_SOLUTIONS = {"default", "active", "basic"}

FALSE_POSITIVE = "False positive"
ACCEPTED = "Accepted"
# Rows the audit can't decide alone, so a person can close them. A dependency needs a fix.
JUDGED_ROW_TYPES = {"File", "Solutions", "Plug-in assembly"}
CYCLE_ROW_TYPE = "Solution"

IN_TARGET = "In target"
IN_PRODUCTION = "In production"
IN_FLIGHT = "In-flight"
VERSION_TOO_OLD = "Version too old"
UNKNOWN = "Unknown"
ACTIONABLE = {IN_FLIGHT, VERSION_TOO_OLD, UNKNOWN}
STATUS_ORDER = {UNKNOWN: 0, IN_FLIGHT: 1, VERSION_TOO_OLD: 2, IN_PRODUCTION: 3, IN_TARGET: 4}

DEPENDENCY_TYPES = {0: "None", 1: "Internal", 2: "Published", 4: "Unpublished"}

COMPONENT_LABELS = {
    1: "Table", 2: "Column", 3: "Relationship", 4: "Choice value", 9: "Global choice",
    10: "Relationship", 14: "Alternate key", 20: "Security role", 24: "Form",
    26: "View", 29: "Process or flow", 31: "Report", 36: "Email template",
    48: "Ribbon command", 50: "Ribbon customization", 55: "Ribbon diff", 59: "Chart",
    60: "Form", 61: "Web resource", 62: "Site map", 63: "Connection role",
    65: "Hierarchy rule", 66: "Custom control", 70: "Field security profile",
    71: "Field permission", 80: "Model-driven app", 90: "Plug-in type",
    91: "Plug-in assembly", 92: "Plug-in step", 93: "Plug-in step image",
    95: "Service endpoint", 161: "Mobile offline profile", 300: "Canvas app",
    371: "Connector", 372: "Connector", 380: "Environment variable",
    381: "Environment variable value", 400: "AI project type", 401: "AI project",
    402: "AI configuration",
}


@dataclass(frozen=True)
class RecordType:
    entity_set: str
    key: str
    name_columns: Tuple[str, ...]
    natural: str = ""


RECORD_TYPES = {
    # Roles and service endpoints match on GUID: their names aren't unique.
    20: RecordType("roles", "roleid", ("name",)),
    24: RecordType("systemforms", "formid", ("name",)),
    26: RecordType("savedqueries", "savedqueryid", ("name",)),
    29: RecordType("workflows", "workflowid", ("name",)),
    31: RecordType("reports", "reportid", ("name",)),
    36: RecordType("templates", "templateid", ("title",)),
    59: RecordType("savedqueryvisualizations", "savedqueryvisualizationid", ("name",)),
    60: RecordType("systemforms", "formid", ("name",)),
    61: RecordType("webresourceset", "webresourceid", ("name",), "name"),
    62: RecordType("sitemaps", "sitemapid", ("sitemapnameunique", "sitemapname"), "sitemapnameunique"),
    63: RecordType("connectionroles", "connectionroleid", ("name",)),
    66: RecordType("customcontrols", "customcontrolid", ("name",), "name"),
    70: RecordType("fieldsecurityprofiles", "fieldsecurityprofileid", ("name",)),
    80: RecordType("appmodules", "appmoduleid", ("uniquename", "name"), "uniquename"),
    90: RecordType("plugintypes", "plugintypeid", ("typename",), "typename"),
    91: RecordType("pluginassemblies", "pluginassemblyid", ("name",), "name"),
    92: RecordType("sdkmessageprocessingsteps", "sdkmessageprocessingstepid", ("name",)),
    93: RecordType("sdkmessageprocessingstepimages", "sdkmessageprocessingstepimageid", ("name",)),
    95: RecordType("serviceendpoints", "serviceendpointid", ("name",)),
    300: RecordType("canvasapps", "canvasappid", ("name",), "name"),
    371: RecordType("connectors", "connectorid", ("name",), "name"),
    372: RecordType("connectors", "connectorid", ("name",), "name"),
    380: RecordType("environmentvariabledefinitions", "environmentvariabledefinitionid", ("schemaname",), "schemaname"),
    381: RecordType("environmentvariablevalues", "environmentvariablevalueid", ("schemaname",)),
}

# Component types above 10000 point to a table whose type number varies by environment.
# These tables have a stable name that is safer to match on than a GUID.
DYNAMIC_NATURAL_KEYS = {
    "connectionreference": "connectionreferencelogicalname",
    "customapi": "uniquename",
    "environmentvariabledefinition": "schemaname",
}

SKIPPED_DIRECTORIES = {".git", "node_modules", "bin", "obj", ".vs", "packages", "dist"}
BINARY_SUFFIXES = {
    ".dll", ".exe", ".pdb", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".nupkg", ".snk", ".pfx", ".xap",
}
MAX_SCANNED_BYTES = 50 * 1024 * 1024
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[./\\-][A-Za-z0-9_]+)*")
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SOLUTION_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

CSV_COLUMNS = [
    "id", "status", "target_solutions", "dependent_name", "dependent_type", "required_name", "required_type",
    "dependency_type", "depth", "owning_solutions", "production_detail", "sources",
    "evidence", "used_by_count", "reconcile_note", "explanation", "dependent_id", "required_id",
]


class AuditError(Exception):
    """The audit can't produce a trustworthy result."""


class ApiError(Exception):
    def __init__(self, status: Optional[int], message: str, path: str = ""):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.path = path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the bearer token to another address, so refuse it."""

    def redirect_request(self, *args, **kwargs):
        return None


class ReadOnlyClient:
    """Dataverse Web API client that can only send GET requests."""

    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[str], str],
        label: str,
        header_builder: Optional[Callable[[str], Dict[str, str]]] = None,
        retries: int = 5,
        timeout: int = 120,
        sleep: Callable[[float], None] = time.sleep,
        api_root: str = API_ROOT,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_root = api_root
        self.label = label
        self.request_count = 0
        self._origin = self._origin_of(self.base_url)
        self._token_provider = token_provider
        self._header_builder = header_builder
        self._token: Optional[str] = None
        self._retries = retries
        self._timeout = timeout
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_NoRedirect)

    @staticmethod
    def _origin_of(url: str) -> Tuple[str, str]:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme.lower(), parts.netloc.lower()

    def _headers(self) -> Dict[str, str]:
        if self._token is None:
            self._token = self._token_provider(self.base_url)
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._header_builder:
            headers = dict(self._header_builder(self._token))
        headers.update({"Accept": "application/json", "OData-MaxVersion": "4.0", "OData-Version": "4.0"})
        return headers

    def _url(self, path: str, params: Optional[Dict[str, str]]) -> str:
        url = self.base_url + self.api_root + urllib.parse.quote(path, safe="/()=',:@?&$-_.~")
        if params:
            query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="$,()")
            url += ("&" if "?" in url else "?") + query
        return url

    def get(self, path: str, params: Optional[Dict[str, str]] = None) -> dict:
        return self._fetch(self._url(path, params), path)

    @staticmethod
    def _rows_of(page, path: str) -> List[dict]:
        rows = page.get("value") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            raise ApiError(None, "the response had no list of rows", path)
        return rows

    def get_all(self, path: str, params: Optional[Dict[str, str]] = None) -> List[dict]:
        page = self.get(path, params)
        rows = list(self._rows_of(page, path))
        next_link = page.get("@odata.nextLink") or page.get("nextLink")
        while next_link:
            if self._origin_of(next_link) != self._origin:
                raise ApiError(None, "the next-page link points to another host, so it was not followed", path)
            page = self._fetch(next_link, path)
            rows.extend(self._rows_of(page, path))
            next_link = page.get("@odata.nextLink") or page.get("nextLink")
        return rows

    def _fetch(self, url: str, path: str) -> dict:
        token_refreshed = False
        last_error: Optional[ApiError] = None
        for attempt in range(self._retries + 1):
            request = urllib.request.Request(url, method="GET", headers=self._headers())
            self.request_count += 1
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8-sig")) if raw else {}
            except urllib.error.HTTPError as exc:
                body = exc.read()
                last_error = ApiError(exc.code, _error_message(body), path)
                if exc.code == 401 and not token_refreshed:
                    self._token = None
                    token_refreshed = True
                    continue
                if exc.code not in (429, 502, 503, 504) or attempt == self._retries:
                    raise last_error from None
                self._sleep(_retry_delay(exc.headers.get("Retry-After"), attempt))
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                last_error = ApiError(None, f"could not reach {self.label}: {exc}", path)
                if attempt == self._retries:
                    raise last_error from None
                self._sleep(_retry_delay(None, attempt))
            except json.JSONDecodeError as exc:
                raise ApiError(None, f"the response was not JSON: {exc}", path) from None
        raise last_error or ApiError(None, "the request did not complete", path)


def _retry_delay(retry_after: Optional[str], attempt: int) -> float:
    try:
        return min(float(retry_after), 300.0) if retry_after is not None else min(2.0 ** attempt, 60.0)
    except ValueError:
        return min(2.0 ** attempt, 60.0)


def _error_message(body: bytes) -> str:
    try:
        return str(json.loads(body.decode("utf-8-sig"))["error"]["message"])[:300]
    except (ValueError, KeyError, TypeError):
        return body[:200].decode("utf-8", "replace")


def norm_guid(value) -> str:
    text = str(value or "").strip().strip("{}").lower()
    return "" if text == EMPTY_GUID else text


def odata_text(value: str) -> str:
    return value.replace("'", "''")


def label_of(component_type) -> str:
    try:
        return COMPONENT_LABELS.get(int(component_type)) or f"Type {int(component_type)}"
    except (TypeError, ValueError):
        return str(component_type)


def version_tuple(version) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(version or "")))


def usable_token(token: str) -> bool:
    """Skip short or unprefixed names: they match ordinary words and system columns."""
    token = token.lower()
    return len(token) >= 5 and " " not in token and ("_" in token.strip("_") or "/" in token or "." in token.strip("."))


@dataclass
class Component:
    id: str
    type: int
    name: str
    resolved: bool = True
    error: str = ""
    parent_id: str = ""
    metadata_path: str = ""
    entity_set: str = ""
    key_column: str = ""
    natural_column: str = ""
    natural_value: str = ""
    tokens: Tuple[str, ...] = ()
    type_label: str = ""

    @property
    def key(self) -> Tuple[str, int]:
        return self.id, self.type

    @property
    def label(self) -> str:
        return self.type_label or label_of(self.type)


@dataclass
class Classification:
    status: str
    owners: str = ""
    detail: str = ""
    targets: Tuple[str, ...] = ()


@dataclass
class Row:
    dependent_id: str
    dependent_name: str
    dependent_type: str
    required: Optional[Component]
    required_id: str
    required_name: str
    required_type: str
    status: str
    owners: str
    detail: str
    depth: int = 1
    dependency_types: Set[str] = field(default_factory=set)
    evidence: str = ""
    note: str = ""
    used_by: str = ""
    explanation: str = ""
    target_solutions: Set[str] = field(default_factory=set)
    holding_targets: Tuple[str, ...] = ()

    @property
    def merge_key(self) -> Tuple[str, str]:
        return (self.dependent_id or self.dependent_name.lower(), self.required_id or self.required_name.lower())

    @property
    def stable_id(self) -> str:
        digest = hashlib.sha1("|".join(self.merge_key).encode("utf-8")).hexdigest()
        return f"D-{digest[:8]}"

    @property
    def component_key(self) -> str:
        return self.required_id or self.required_name.lower()


class Resolver:
    """Names components in DEV, then checks whether production has them."""

    def __init__(self, dev: ReadOnlyClient, prod: ReadOnlyClient, target_ids: Dict[str, str]):
        self.dev = dev
        self.prod = prod
        self.target_ids = target_ids
        self._components: Dict[Tuple[str, int], Component] = {}
        self._entities: Dict[str, dict] = {}
        self._columns: Dict[str, Tuple[str, str]] = {}
        self._definitions: Dict[int, str] = {}
        self._tables: Dict[str, dict] = {}
        self._solutions: Dict[str, dict] = {}
        self._prod_solutions: Dict[str, Optional[dict]] = {}
        self._production: Dict[Tuple[str, int], Tuple[Optional[bool], str]] = {}
        self._classifications: Dict[Tuple[str, int], Classification] = {}

    # Naming ---------------------------------------------------------------------------
    def resolve(self, object_id, component_type, parent_hint=None) -> Component:
        cid = norm_guid(object_id)
        ctype = int(component_type)
        key = (cid, ctype)
        parent = norm_guid(parent_hint)
        if key not in self._components:
            try:
                self._components[key] = self._resolve(cid, ctype, parent)
            except ApiError as exc:
                self._components[key] = Component(cid, ctype, f"{label_of(ctype)} {cid}", False, str(exc))
        component = self._components[key]
        # A table added with all objects holds its forms and views too, not only its columns.
        if parent and not component.parent_id:
            component.parent_id = parent
        return component

    def _resolve(self, cid: str, ctype: int, parent: str) -> Component:
        if ctype == 1:
            return self._table(cid)
        if ctype == 2:
            return self._column(cid, parent)
        if ctype in (3, 10):
            name = self.dev.get(f"RelationshipDefinitions({cid})", {"$select": "SchemaName"})["SchemaName"]
            return Component(
                cid, ctype, name,
                metadata_path=f"RelationshipDefinitions(SchemaName='{odata_text(name)}')?$select=MetadataId",
                tokens=(name,),
            )
        if ctype == 9:
            name = self.dev.get(f"GlobalOptionSetDefinitions({cid})", {"$select": "Name"})["Name"]
            return Component(
                cid, ctype, name,
                metadata_path=f"GlobalOptionSetDefinitions(Name='{odata_text(name)}')?$select=Name",
                tokens=(name,),
            )
        if ctype == 14:
            return self._alternate_key(cid, parent)
        if ctype in RECORD_TYPES:
            return self._record(cid, ctype, RECORD_TYPES[ctype])
        if ctype >= 10000:
            return self._dynamic(cid, ctype)
        raise ApiError(None, f"this script can't name component type {ctype}", "")

    def _entity(self, cid: str) -> dict:
        if cid not in self._entities:
            self._entities[cid] = self.dev.get(
                f"EntityDefinitions({cid})", {"$select": "LogicalName,EntitySetName"}
            )
        return self._entities[cid]

    def _table(self, cid: str) -> Component:
        entity = self._entity(cid)
        logical = entity["LogicalName"]
        return Component(
            cid, 1, logical,
            metadata_path=f"EntityDefinitions(LogicalName='{odata_text(logical)}')?$select=MetadataId",
            tokens=(logical, entity.get("EntitySetName") or logical),
        )

    def _column(self, cid: str, parent: str) -> Component:
        if cid in self._columns:
            table, column = self._columns[cid]
        else:
            parent = parent or self._parent_from_solution_components(cid)
            if not parent:
                raise ApiError(None, "could not find the column's table", "")
            table = self._entity(parent)["LogicalName"]
            column = self.dev.get(
                f"EntityDefinitions({parent})/Attributes({cid})", {"$select": "LogicalName"}
            )["LogicalName"]
        path = (
            f"EntityDefinitions(LogicalName='{odata_text(table)}')"
            f"/Attributes(LogicalName='{odata_text(column)}')?$select=MetadataId"
        )
        return Component(cid, 2, f"{table}.{column}", parent_id=parent, metadata_path=path, tokens=(column,))

    def _alternate_key(self, cid: str, parent: str) -> Component:
        parent = parent or self._parent_from_solution_components(cid)
        if not parent:
            raise ApiError(None, "could not find the alternate key's table", "")
        table = self._entity(parent)["LogicalName"]
        key_name = self.dev.get(f"EntityDefinitions({parent})/Keys({cid})", {"$select": "LogicalName"})["LogicalName"]
        path = (
            f"EntityDefinitions(LogicalName='{odata_text(table)}')"
            f"/Keys(LogicalName='{odata_text(key_name)}')?$select=MetadataId"
        )
        return Component(cid, 14, f"{table}.{key_name}", parent_id=parent, metadata_path=path, tokens=(key_name,))

    def _parent_from_solution_components(self, cid: str) -> str:
        rows = self.dev.get_all(
            "solutioncomponents",
            {"$filter": f"objectid eq {cid}", "$select": "rootsolutioncomponentid,solutioncomponentid"},
        )
        for row in rows:
            root = norm_guid(row.get("rootsolutioncomponentid"))
            if root:
                parent = self.dev.get(f"solutioncomponents({root})", {"$select": "objectid,componenttype"})
                if int(parent.get("componenttype", 0)) == 1:
                    return norm_guid(parent.get("objectid"))
        return ""

    def _record(self, cid: str, ctype: int, spec: RecordType) -> Component:
        columns = list(dict.fromkeys(spec.name_columns + ((spec.natural,) if spec.natural else ())))
        row = self.dev.get(f"{spec.entity_set}({cid})", {"$select": ",".join(columns)})
        name = next((row.get(column) for column in spec.name_columns if row.get(column)), "") or f"{label_of(ctype)} {cid}"
        natural = str(row.get(spec.natural) or "") if spec.natural else ""
        tokens = [cid]
        if natural:
            tokens.append(natural)
        if ctype == 61 and "/" in natural and "." in natural.rsplit("/", 1)[-1]:
            # Web resources often link to each other by relative path, which Dataverse doesn't track.
            tokens.append(natural.rsplit("/", 1)[-1])
        return Component(
            cid, ctype, str(name), entity_set=spec.entity_set, key_column=spec.key,
            natural_column=spec.natural if natural else "", natural_value=natural, tokens=tuple(tokens),
        )

    def _dynamic(self, cid: str, ctype: int) -> Component:
        table = self._definition(ctype)
        meta = self._table_meta(table)
        natural_column = DYNAMIC_NATURAL_KEYS.get(table, "")
        primary_name = meta.get("PrimaryNameAttribute") or ""
        columns = [column for column in dict.fromkeys((primary_name, natural_column)) if column]
        params = {"$select": ",".join(columns)} if columns else None
        row = self.dev.get(f"{meta['EntitySetName']}({cid})", params)
        natural = str(row.get(natural_column) or "") if natural_column else ""
        name = natural or str(row.get(primary_name) or "") or f"{table} {cid}"
        tokens = (cid, natural) if natural else (cid,)
        return Component(
            cid, ctype, name, entity_set=meta["EntitySetName"], key_column=meta["PrimaryIdAttribute"],
            natural_column=natural_column if natural else "", natural_value=natural, tokens=tokens,
            type_label=table,
        )

    def _definition(self, ctype: int) -> str:
        if ctype not in self._definitions:
            rows = self.dev.get_all(
                "solutioncomponentdefinitions",
                {"$filter": f"solutioncomponenttype eq {ctype}", "$select": "primaryentityname,name"},
            )
            if not rows or not rows[0].get("primaryentityname"):
                raise ApiError(None, f"component type {ctype} has no definition in DEV", "")
            self._definitions[ctype] = rows[0]["primaryentityname"]
        return self._definitions[ctype]

    def _table_meta(self, table: str) -> dict:
        if table not in self._tables:
            self._tables[table] = self.dev.get(
                f"EntityDefinitions(LogicalName='{odata_text(table)}')",
                {"$select": "EntitySetName,PrimaryIdAttribute,PrimaryNameAttribute"},
            )
        return self._tables[table]

    def index_table_columns(self, table_id: str) -> List[Tuple[str, str, str]]:
        """Return every column on a table, and remember them for later naming."""
        data = self.dev.get(
            f"EntityDefinitions({table_id})",
            {"$select": "LogicalName,EntitySetName", "$expand": "Attributes($select=MetadataId,LogicalName)"},
        )
        table = data["LogicalName"]
        self._entities.setdefault(table_id, {"LogicalName": table, "EntitySetName": data.get("EntitySetName")})
        columns = []
        for attribute in data.get("Attributes", []):
            column_id = norm_guid(attribute.get("MetadataId"))
            self._columns[column_id] = (table, attribute["LogicalName"])
            columns.append((column_id, table, attribute["LogicalName"]))
        return columns

    def searchable_by_guid_only(self, ctype: int) -> bool:
        """True when a component has no stable name, so its GUID is all a scan can look for."""
        if ctype in (1, 2, 3, 9, 10, 14):
            return False
        if ctype in RECORD_TYPES:
            return not RECORD_TYPES[ctype].natural
        if ctype >= 10000:
            try:
                return self._definition(ctype) not in DYNAMIC_NATURAL_KEYS
            except ApiError:
                return True
        return True

    # Ownership ------------------------------------------------------------------------
    def solution(self, solution_id: str) -> dict:
        if solution_id not in self._solutions:
            self._solutions[solution_id] = self.dev.get(
                f"solutions({solution_id})", {"$select": "uniquename,ismanaged,version,isvisible"}
            )
        return self._solutions[solution_id]

    def _is_ignored(self, info: dict) -> bool:
        return str(info.get("uniquename", "")).lower() in IGNORED_SOLUTIONS or info.get("isvisible") is False

    def owners(self, component: Component) -> Tuple[List[Tuple[dict, bool]], List[str]]:
        """Return (solution, reached through its table) pairs, and the target solutions among them."""
        found: Dict[str, Tuple[dict, bool]] = {}
        held: Set[str] = set()
        rows = self.dev.get_all(
            "solutioncomponents", {"$filter": f"objectid eq {component.id}", "$select": "_solutionid_value"}
        )
        for row in rows:
            solution_id = norm_guid(row.get("_solutionid_value"))
            if solution_id in self.target_ids:
                held.add(self.target_ids[solution_id])
            info = self.solution(solution_id)
            if not self._is_ignored(info):
                found[solution_id] = (info, False)
        if component.parent_id:
            # A table added with all objects owns its columns without a row for each one.
            rows = self.dev.get_all(
                "solutioncomponents",
                {"$filter": f"objectid eq {component.parent_id}", "$select": "_solutionid_value,rootcomponentbehavior"},
            )
            for row in rows:
                if row.get("rootcomponentbehavior") != 0:
                    continue
                solution_id = norm_guid(row.get("_solutionid_value"))
                if solution_id in self.target_ids:
                    held.add(self.target_ids[solution_id])
                info = self.solution(solution_id)
                if not self._is_ignored(info):
                    found.setdefault(solution_id, (info, True))
        return list(found.values()), sorted(held, key=str.lower)

    # Production -----------------------------------------------------------------------
    def in_production(self, component: Component) -> Tuple[Optional[bool], str]:
        if not component.resolved:
            return None, component.error or "the component could not be named"
        if component.key not in self._production:
            self._production[component.key] = self._check_production(component)
        return self._production[component.key]

    def _check_production(self, component: Component) -> Tuple[Optional[bool], str]:
        try:
            if component.metadata_path:
                self.prod.get(component.metadata_path)
                return True, ""
            if component.natural_column:
                rows = self.prod.get_all(
                    component.entity_set,
                    {
                        "$filter": f"{component.natural_column} eq '{odata_text(component.natural_value)}'",
                        "$select": component.key_column,
                        "$top": "1",
                    },
                )
                if rows:
                    return True, ""
            rows = self.prod.get_all(
                component.entity_set,
                {"$filter": f"{component.key_column} eq {component.id}", "$select": component.key_column, "$top": "1"},
            )
            return bool(rows), ""
        except ApiError as exc:
            if exc.status == 404:
                return False, ""
            return None, str(exc)

    def production_solution(self, unique_name: str) -> Optional[dict]:
        if unique_name not in self._prod_solutions:
            rows = self.prod.get_all(
                "solutions",
                {"$filter": f"uniquename eq '{odata_text(unique_name)}'", "$select": "version,ismanaged", "$top": "1"},
            )
            self._prod_solutions[unique_name] = rows[0] if rows else None
        return self._prod_solutions[unique_name]

    # Status ---------------------------------------------------------------------------
    def classify(self, component: Component, base_solution_id="", introduced=None) -> Classification:
        if component.key not in self._classifications:
            self._classifications[component.key] = self._classify(component, norm_guid(base_solution_id), introduced)
        return self._classifications[component.key]

    def _classify(self, component: Component, base_id: str, introduced) -> Classification:
        owners: List[Tuple[dict, bool]] = []
        owner_error = ""
        held: List[str] = []
        if component.id:
            try:
                owners, held = self.owners(component)
            except ApiError as exc:
                owner_error = str(exc)
        owner_text = "; ".join(
            f"{info.get('uniquename')}{' (via table)' if via_table else ''}" for info, via_table in owners
        )
        if held:
            return Classification(IN_TARGET, owner_text, "", tuple(held))

        found, production_error = self.in_production(component)
        managed, base_error = self._managed_base(owners, base_id)
        if managed is not None:
            return self._classify_managed(found, production_error, managed, introduced, owner_text)
        if found is None:
            return Classification(UNKNOWN, owner_text, f"Production check failed: {production_error}")
        # Without the owner, a managed component that's too old would pass as In production.
        lookup_error = owner_error or base_error
        if lookup_error:
            where = "In production" if found else "Not in production"
            return Classification(UNKNOWN, owner_text, f"{where}; owner check failed: {lookup_error}")
        if found:
            return Classification(IN_PRODUCTION, owner_text, "Found in production")
        unmanaged = [info for info, _ in owners if not info.get("ismanaged")]
        detail = "Not in production" if unmanaged else "Not in production and not in any solution (Default only)"
        return Classification(IN_FLIGHT, owner_text or "None", detail)

    def _managed_base(self, owners: List[Tuple[dict, bool]], base_id: str) -> Tuple[Optional[dict], str]:
        error = ""
        if base_id:
            try:
                info = self.solution(base_id)
                if info.get("ismanaged") and not self._is_ignored(info):
                    return info, ""
            except ApiError as exc:
                error = str(exc)
        if owners and all(info.get("ismanaged") for info, _ in owners):
            return owners[0][0], ""
        return None, error

    def _classify_managed(self, found, production_error, managed, introduced, owner_text) -> Classification:
        name = str(managed.get("uniquename"))
        dev_version = str(managed.get("version") or "")
        if found is None:
            return Classification(UNKNOWN, owner_text, f"Production check failed: {production_error}")
        try:
            installed = self.production_solution(name)
        except ApiError as exc:
            return Classification(UNKNOWN, owner_text, f"Could not read {name} in production: {exc}")
        prod_version = str(installed.get("version") or "") if installed else ""
        if found:
            if prod_version and _older_than_introduced(prod_version, introduced):
                return Classification(
                    VERSION_TOO_OLD, owner_text,
                    f"Production has {name} {prod_version}; the component needs {introduced}",
                )
            detail = f"Found in production ({name} {prod_version})" if prod_version else "Found in production"
            return Classification(IN_PRODUCTION, owner_text, detail)
        if not installed:
            return Classification(VERSION_TOO_OLD, owner_text, f"Managed solution {name} isn't installed in production")
        if version_tuple(prod_version) < version_tuple(dev_version):
            return Classification(VERSION_TOO_OLD, owner_text, f"Production has {name} {prod_version}; DEV has {dev_version}")
        return Classification(
            UNKNOWN, owner_text,
            f"Production has {name} {prod_version}, the same as or newer than DEV, but the component wasn't found",
        )


def _older_than_introduced(prod_version: str, introduced) -> bool:
    """The introduced version is stored with two decimals, so compare major.minor only."""
    try:
        value = float(introduced)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    return version_tuple(prod_version)[:2] < version_tuple(f"{value:g}")[:2]


# Text scan ----------------------------------------------------------------------------
def _decode(raw: bytes) -> Optional[str]:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "ignore")
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8-sig", "ignore")


ARCHIVE_SUFFIXES = {".zip", ".nupkg"}


def iter_text_files(
    root: Path, skipped: Optional[List[Tuple[str, str]]] = None, build_folders: Optional[List[str]] = None,
) -> Iterator[Tuple[str, str]]:
    """Yield (relative path, text), including inside canvas apps.

    Files and folders that can't be read go to skipped. Build folders go to build_folders.
    """
    skipped = skipped if skipped is not None else []
    root_name = root.resolve().name
    # Everything in an unpacked solution ships, even from a folder called dist or bin.
    ignored = {".git"} if (root / "Other" / "Solution.xml").is_file() else SKIPPED_DIRECTORIES

    def unreadable(error: OSError) -> None:
        try:
            relative = Path(str(error.filename)).relative_to(root).as_posix()
        except ValueError:
            relative = str(error.filename)
        skipped.append((f"{root_name}/{relative}", "This folder can't be read, so it wasn't searched."))

    for folder, directories, files in os.walk(root, onerror=unreadable):
        current = Path(folder)
        if build_folders is not None:
            build_folders.extend(
                f"{root_name}/{(current / name).relative_to(root).as_posix()}"
                for name in directories if name in ignored and name != ".git"
            )
        directories[:] = sorted(name for name in directories if name not in ignored and not (current / name).is_symlink())
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root)
            if path.is_symlink() or not path.is_file():
                continue
            # Solution.xml lists missing dependencies itself, so scanning it would echo the graph.
            if relative.parts[-2:] == ("Other", "Solution.xml"):
                continue
            suffix = path.suffix.lower()
            label = f"{root_name}/{relative.as_posix()}"
            if suffix in ARCHIVE_SUFFIXES:
                skipped.append((label, "This archive can't be searched. Unpack it, or confirm it holds nothing that ships."))
                continue
            if suffix in BINARY_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_SCANNED_BYTES:
                    skipped.append((label, "This file is larger than 50 MB, so it wasn't searched."))
                    continue
                if suffix == ".msapp":
                    yield from _iter_msapp(path, label, skipped)
                    continue
                decoded = _decode(path.read_bytes())
            except OSError as exc:
                skipped.append((label, f"This file can't be read, so it wasn't searched: {exc.strerror}"))
                continue
            if decoded is not None:
                yield label, decoded


def _iter_msapp(path: Path, label: str, skipped: List[Tuple[str, str]]) -> Iterator[Tuple[str, str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                if entry.file_size > MAX_SCANNED_BYTES:
                    skipped.append((f"{label}!/{entry.filename}", "This file is larger than 50 MB, so it wasn't searched."))
                    continue
                if Path(entry.filename).suffix.lower() in BINARY_SUFFIXES:
                    continue
                text = _decode(archive.read(entry))
                if text is not None:
                    yield f"{label}!/{entry.filename}", text
    except zipfile.BadZipFile:
        skipped.append((label, "This canvas app package couldn't be opened, so it wasn't searched."))


def read_explanations(path: Path, warnings: List[str]) -> Dict[str, str]:
    """Read the explanations in an earlier CSV, including one re-saved by a spreadsheet."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not {"id", "explanation"} <= set(reader.fieldnames or []):
        warnings.append(f"{path.name} has no id and explanation columns, so earlier explanations weren't kept.")
        return {}
    return {row["id"]: row.get("explanation") or "" for row in reader if row.get("id")}


GENERATED_MARKERS = ("<auto-generated", "generatedcode(", "this code was generated by a tool")
CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)", re.I)
GENERATED_NOTE = "False positive (automatic): found only in generated code, and no other scanned code uses it."
TEST_NOTE = "Found in test code, which doesn't ship. Confirm the release branch doesn't contain this feature."


def classify_code(label: str, text: str) -> str:
    """Return 'generated', 'test' or an empty string for ordinary source."""
    lowered = label.lower()
    if lowered.endswith((".g.cs", ".generated.cs", ".designer.cs")):
        return "generated"
    # Only compiled code has these headers. A script could just mention the words.
    if lowered.endswith((".cs", ".vb")) and any(marker in text[:3000].lower() for marker in GENERATED_MARKERS):
        return "generated"
    if re.search(r"(^|/)([^/]*[._-])?(unit|integration)?tests?([._-][^/]*)?/", lowered):
        return "test"
    if re.search(r"Tests?\.(cs|vb|py|ts|js)$", label) or re.search(r"\.(test|spec)\.[a-z]+$", lowered) or re.search(r"(^|/)test_[^/]*\.py$", lowered):
        return "test"
    return ""


def load_sources_lock(path: Path) -> List[dict]:
    """Read sources-lock.json and confirm each folder still holds the recorded commit."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        repositories = data["repositories"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuditError(f"Could not read the sources lock file: {exc}") from None
    for entry in repositories:
        folder = Path(str(entry.get("folder", "")))
        if not folder.is_dir():
            raise AuditError(f"Source folder '{folder}' wasn't found. Download the branches again.")
        if _git_output(folder, "rev-parse", "HEAD") != entry.get("commit"):
            raise AuditError(f"'{folder}' isn't at the recorded commit. Download the branches again.")
        if _git_output(folder, "status", "--porcelain"):
            raise AuditError(f"'{folder}' has changes since it was downloaded, so it isn't the recorded commit. Download the branches again.")
    return repositories


def _git_output(folder: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditError(f"Could not check '{folder}' with Git: {exc}") from None
    if result.returncode != 0:
        raise AuditError(f"Git can't read '{folder}', so it can't be matched to its recorded commit. Download the branches again.")
    return result.stdout.strip()


def split_command(command: str, windows: bool = os.name == "nt") -> List[str]:
    """Split a command line. On Windows, the backslashes in a path must survive."""
    try:
        parts = shlex.split(command, posix=not windows)
    except ValueError as exc:
        raise AuditError(f"Could not read the token command: {exc}") from None
    if windows:
        parts = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part for part in parts]
    return parts


def _token_candidates(token: str) -> Iterator[str]:
    lowered = token.lower().replace("\\", "/")
    yield lowered
    segments = lowered.split("/")
    for index in range(1, len(segments)):
        yield "/".join(segments[index:])
    for piece in re.split(r"[./-]", lowered):
        yield piece
        match = re.fullmatch(r"_(.+)_value", piece)
        if match:
            yield match.group(1)


def scan_text(text: str, tokens: Dict[str, List[Component]]) -> Dict[Tuple[str, int], Tuple[int, int, str]]:
    """Return {component: (first line, number of hits, the name that matched)}."""
    hits: Dict[Tuple[str, int], Tuple[int, int, str]] = {}

    def record(token: str, line_number: int) -> None:
        for component in tokens.get(token, ()):
            first, count, name = hits.get(component.key, (line_number, 0, token))
            hits[component.key] = (first, count + 1, name)

    for line_number, line in enumerate(text.splitlines(), 1):
        for guid in GUID_RE.findall(line):
            record(guid.lower(), line_number)
        for match in TOKEN_RE.finditer(line):
            matched: Set[str] = set()
            for candidate in _token_candidates(match.group(0)):
                if candidate in tokens and candidate not in matched:
                    matched.add(candidate)
                    record(candidate, line_number)
    return hits


def add_tokens(tokens: Dict[str, List[Component]], component: Component, unsearchable: Optional[Set[str]] = None) -> None:
    for token in component.tokens:
        token = token.lower()
        if GUID_RE.fullmatch(token) or usable_token(token):
            bucket = tokens.setdefault(token, [])
            if all(item.key != component.key for item in bucket):
                bucket.append(component)
        elif unsearchable is not None and component.type not in (1, 2):
            unsearchable.add(token)


def solution_tokens(
    resolver: Resolver, solution_id: str, errors: List[str], unsearchable: Optional[Set[str]] = None,
) -> Dict[str, List[Component]]:
    """Return the searchable names of everything in one solution."""
    tokens: Dict[str, List[Component]] = {}

    def add(component: Component) -> None:
        add_tokens(tokens, component, unsearchable)

    rows = resolver.dev.get_all(
        "solutioncomponents",
        {"$filter": f"_solutionid_value eq {solution_id}", "$select": "objectid,componenttype,rootcomponentbehavior"},
    )
    tables = [row for row in rows if int(row.get("componenttype", 0)) == 1]
    for row in tables:
        table_id = norm_guid(row.get("objectid"))
        component = resolver.resolve(table_id, 1)
        if not component.resolved:
            errors.append(component.name)
            continue
        add(component)
        try:
            columns = resolver.index_table_columns(table_id)
        except ApiError as exc:
            errors.append(f"columns of {component.name}: {exc}")
            continue
        if row.get("rootcomponentbehavior") == 0:
            for column_id, _table, _column in columns:
                add(resolver.resolve(column_id, 2, table_id))
    for row in rows:
        ctype = int(row.get("componenttype", 0))
        if ctype == 1:
            continue
        if resolver.searchable_by_guid_only(ctype):
            # A placeholder saves one request per component. A hit resolves the real name later.
            object_id = norm_guid(row.get("objectid"))
            tokens.setdefault(object_id, []).append(Component(object_id, ctype, f"{label_of(ctype)} {object_id}", False))
            continue
        component = resolver.resolve(row.get("objectid"), ctype)
        if component.resolved:
            add(component)
        else:
            errors.append(component.name)
    return tokens


def is_table_shell(row: dict) -> bool:
    """A table added as a shell carries nothing another project could have changed."""
    return int(row.get("componenttype", 0)) == 1 and row.get("rootcomponentbehavior") != 0


def _root_table(row: dict, roots: Dict[str, dict]) -> str:
    """Return the table a subcomponent row hangs from, if its root row is a table."""
    root = roots.get(norm_guid(row.get("rootsolutioncomponentid")))
    return norm_guid(root.get("objectid")) if root and int(root.get("componenttype", 0)) == 1 else ""


def plan_release(targets: List[str], edges: Set[Tuple[str, str]]) -> Tuple[List[str], List[str], List[str]]:
    """Order targets so each comes after what it needs. Edges are (needs, needed) pairs.

    Returns (import order, solutions that need each other, solutions waiting for those).
    """
    needs = {name: {needed for source, needed in edges if source == name and needed != name} for name in targets}
    order: List[str] = []
    remaining = set(targets)
    while True:
        ready = sorted((name for name in remaining if not needs[name] & remaining), key=str.lower)
        if not ready:
            break
        order.append(ready[0])
        remaining.discard(ready[0])


    def reaches_itself(start: str) -> bool:
        seen: Set[str] = set()
        stack = list(needs[start] & remaining)
        while stack:
            item = stack.pop()
            if item == start:
                return True
            if item not in seen:
                seen.add(item)
                stack.extend(needs[item] & remaining)
        return False

    # What's left is in a cycle or waiting for one.
    cycle = {name for name in remaining if reaches_itself(name)}
    return order, sorted(cycle, key=str.lower), sorted(remaining - cycle, key=str.lower)


def _sample(names: Set[str], limit: int = 3) -> str:
    ordered = sorted(names, key=str.lower)
    extra = len(ordered) - limit
    return ", ".join(ordered[:limit]) + (f" and {extra} more" if extra > 0 else "")


# The audit ----------------------------------------------------------------------------
@dataclass
class Config:
    targets: List[str]
    out: Path
    scan_dirs: List[Path] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)
    stage_jsons: List[Path] = field(default_factory=list)
    in_flight: Optional[List[str]] = None
    max_depth: int = 10
    skip_impact: bool = False
    map_limit: int = 150
    allow_same_org_id: bool = False


class Audit:
    def __init__(self, config: Config, dev: ReadOnlyClient, prod: ReadOnlyClient, log: Callable[[str], None] = print):
        self.config = config
        self.dev = dev
        self.prod = prod
        self.log = log
        self.rows: Dict[Tuple[str, str], Row] = {}
        self.sources: Dict[str, Set[str]] = defaultdict(set)
        self.warnings: List[str] = []
        self.impact: List[dict] = []
        self.incomplete: List[str] = []
        self.own_names: Set[str] = set()
        self.coverage = {
            "files_scanned": 0, "files_not_read": 0, "names_searched": 0, "names_not_searchable": 0,
            "unnamed_components": 0, "text_scan": False, "dry_run": False,
        }
        self.stage_validation: List[str] = []
        self.organizations: Dict[str, str] = {}
        self.source_classes: Set[str] = set()
        self.target_names: List[str] = []
        self.target_ids: Dict[str, str] = {}
        self.target_versions: Dict[str, str] = {}
        self.target_edges: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        self.scanned_targets: Set[str] = set()
        self.release_order: List[str] = []
        self.release_cycle: List[str] = []
        self.release_waiting: List[str] = []
        self.cross_target: List[dict] = []
        self.dry_runs: Dict[str, str] = {}
        self.stage_hits: Dict[str, Set[str]] = defaultdict(set)
        self.chain_rows: Dict[Tuple[str, int], Optional[List[dict]]] = {}
        self.resolver: Optional[Resolver] = None

    # Pre-flight -----------------------------------------------------------------------
    def preflight(self) -> None:
        if self.dev.base_url.lower() == self.prod.base_url.lower():
            raise AuditError("--dev and --prod are the same environment")
        ids = {}
        for client in (self.dev, self.prod):
            try:
                who = client.get("WhoAmI")
                organization_id = norm_guid(who.get("OrganizationId"))
                organization = client.get(f"organizations({organization_id})", {"$select": "name"})
            except ApiError as exc:
                raise AuditError(f"Could not read {client.label} ({client.base_url}): {exc}") from None
            ids[client.label] = organization_id
            self.organizations[client.label] = str(organization.get("name") or "")
        if ids["DEV"] == ids["PROD"]:
            if not self.config.allow_same_org_id:
                raise AuditError(
                    "--dev and --prod point to the same organization. If the admin center shows they're "
                    "separate environments, add --allow-same-org-id."
                )
            self.warnings.append("--dev and --prod report the same organization ID. You confirmed they're separate environments.")

    def find_targets(self) -> List[dict]:
        found: List[dict] = []
        missing: List[str] = []
        for name in self.config.targets:
            try:
                rows = self.dev.get_all(
                    "solutions",
                    {
                        "$filter": f"uniquename eq '{odata_text(name)}'",
                        "$select": "solutionid,uniquename,version,ismanaged",
                    },
                )
            except ApiError as exc:
                raise AuditError(f"Could not look up the target solution {name}: {exc}") from None
            if rows:
                found.append(rows[0])
            else:
                missing.append(name)
        if len(missing) == 1:
            raise AuditError(f"Solution '{missing[0]}' wasn't found in DEV")
        if missing:
            raise AuditError(f"Solutions {', '.join(missing)} weren't found in DEV")
        self.target_names, self.target_ids, self.target_versions = [], {}, {}
        for row in found:
            name = str(row.get("uniquename"))
            self.target_names.append(name)
            self.target_ids[norm_guid(row.get("solutionid"))] = name
            self.target_versions[name] = str(row.get("version") or "")
            warning = f"{name} is managed in DEV. Audit the unmanaged source of the solution instead."
            if row.get("ismanaged") and warning not in self.warnings:
                self.warnings.append(warning)
        return found

    def run(self) -> int:
        self.preflight()
        targets = self.find_targets()
        summary = ", ".join(f"{target.get('uniquename')} {target.get('version')}" for target in targets)
        self.log(f"Auditing {summary}: DEV {self.organizations['DEV']}, production {self.organizations['PROD']}")
        self.resolver = Resolver(self.dev, self.prod, self.target_ids)
        self.walk_graph()
        self.check_shared_components()
        self.scan_sources()
        self.check_plugin_types()
        self.merge_stage()
        self.order_targets()
        self.reconcile()
        self.compute_impact()
        self.carry_explanations()
        self.write_outputs()
        return self.report()

    # Source 1: the dependency graph ---------------------------------------------------
    def walk_graph(self) -> None:
        for target in self.target_names:
            path = f"RetrieveMissingDependencies(SolutionUniqueName='{odata_text(target)}')"
            try:
                missing = self.dev.get_all(path)
            except ApiError as exc:
                raise AuditError(f"RetrieveMissingDependencies failed for {target}: {exc}") from None
            self.log(f"Graph: missing dependency rows for {target}: {len(missing)}")
            queue = deque()
            for raw in missing:
                row = self._add_graph_row(raw, 1, target)
                if row is not None and row.status == IN_FLIGHT and row.required is not None:
                    queue.append((row.required, 1))
            self._walk_chains(queue, target)

    def _add_target_edge(self, target: str, holders: Tuple[str, ...], component: str) -> None:
        """Record that a target needs something another target holds, which fixes their order."""
        if target in holders:
            return
        for holder in holders:
            self.target_edges[(target, holder)].add(component)

    def _note_target_dependency(self, target: str, holders: Tuple[str, ...], component: Component) -> None:
        """Needing another target sets the import order, unless production already has the component."""
        assert self.resolver is not None
        if target in holders:
            return
        found, error = self.resolver.in_production(component)
        if found is True:
            return
        if found is None:
            message = f"Could not check whether production has {component.name}, so the release order may be wrong: {error}"
            if message not in self.incomplete:
                self.incomplete.append(message)
        self._add_target_edge(target, holders, component.name)

    def _add_graph_row(self, raw: dict, depth: int, target: str) -> Optional[Row]:
        assert self.resolver is not None
        required_id = raw.get("requiredcomponentobjectid")
        required_type = raw.get("requiredcomponenttype")
        if not required_id or required_type is None:
            self.incomplete.append(f"A dependency row had no required component: {sorted(raw)[:6]}")
            return None
        required = self.resolver.resolve(required_id, required_type, raw.get("requiredcomponentparentid"))
        dependent_id = raw.get("dependentcomponentobjectid")
        dependent_type = raw.get("dependentcomponenttype")
        dependent_name, dependent_label, dependent_key = "Unknown component", "Unknown", ""
        if dependent_id and dependent_type is not None:
            dependent = self.resolver.resolve(dependent_id, dependent_type, raw.get("dependentcomponentparentid"))
            dependent_name, dependent_label, dependent_key = dependent.name, dependent.label, dependent.id
        verdict = self.resolver.classify(
            required, raw.get("requiredcomponentbasesolutionid"), raw.get("requiredcomponentintroducedversion")
        )
        row = Row(
            dependent_id=dependent_key, dependent_name=dependent_name, dependent_type=dependent_label,
            required=required, required_id=required.id, required_name=required.name,
            required_type=required.label, status=verdict.status, owners=verdict.owners,
            detail=verdict.detail, depth=depth, evidence=f"Web API dependency row (depth {depth})",
            target_solutions={target}, holding_targets=verdict.targets,
        )
        row = self._store(row)
        # Deeper rows pass through an in-flight component, and fixing that changes what's needed.
        if depth == 1 and verdict.status == IN_TARGET:
            self._note_target_dependency(target, verdict.targets, required)
        kind = raw.get("dependencytype")
        if kind is not None:
            row.dependency_types.add(DEPENDENCY_TYPES.get(kind, str(kind)))
        self.sources[row.component_key].add("graph")
        return row

    def _required_rows(self, component: Component) -> Optional[List[dict]]:
        """Read what a component needs once, even when several targets lead to it."""
        if component.key not in self.chain_rows:
            path = f"RetrieveRequiredComponents(ObjectId=@o,ComponentType=@t)?@o={component.id}&@t={component.type}"
            try:
                self.chain_rows[component.key] = self.dev.get_all(path)
            except ApiError as exc:
                self.chain_rows[component.key] = None
                self.incomplete.append(f"Could not walk the chain from {component.name}: {exc}")
        return self.chain_rows[component.key]

    def _walk_chains(self, queue: deque, target: str) -> None:
        seen: Set[Tuple[str, int]] = set()
        while queue:
            component, depth = queue.popleft()
            if component.key in seen:
                continue
            seen.add(component.key)
            if depth >= self.config.max_depth:
                message = f"Stopped walking at depth {depth}: {component.name}"
                if message not in self.incomplete:
                    self.incomplete.append(message)
                continue
            for raw in self._required_rows(component) or []:
                row = self._add_graph_row(raw, depth + 1, target)
                if row is not None and row.status == IN_FLIGHT and row.required is not None:
                    queue.append((row.required, depth + 1))

    def check_shared_components(self) -> None:
        """Flag target components another unmanaged solution also holds: its edits would export too."""
        assert self.resolver is not None
        columns = "solutioncomponentid,objectid,componenttype,rootcomponentbehavior,rootsolutioncomponentid"
        try:
            target_rows = {
                name: self.dev.get_all(
                    "solutioncomponents", {"$filter": f"_solutionid_value eq {solution_id}", "$select": columns}
                )
                for solution_id, name in self.target_ids.items()
            }
            self._find_cross_target_components(target_rows)
            for solution_id in self._in_flight_solutions():
                rows = self.dev.get_all(
                    "solutioncomponents", {"$filter": f"_solutionid_value eq {solution_id}", "$select": columns}
                )
                for name, components in target_rows.items():
                    self._add_shared_rows(name, components, rows, solution_id)
        except ApiError as exc:
            self.incomplete.append(f"Could not compare the targets with other unmanaged solutions: {exc}")

    def _find_cross_target_components(self, target_rows: Dict[str, List[dict]]) -> None:
        """Two targets can hold one component, but the last one imported decides what it looks like."""
        assert self.resolver is not None
        holders: Dict[Tuple[str, int], Set[str]] = defaultdict(set)
        parents: Dict[Tuple[str, int], str] = {}
        all_objects = {
            name: {norm_guid(row.get("objectid")) for row in rows if int(row.get("componenttype", 0)) == 1 and row.get("rootcomponentbehavior") == 0}
            for name, rows in target_rows.items()
        }
        for name, rows in target_rows.items():
            roots = {norm_guid(row.get("solutioncomponentid")): row for row in rows}
            for row in rows:
                if is_table_shell(row):
                    continue
                key = (norm_guid(row.get("objectid")), int(row.get("componenttype", 0)))
                holders[key].add(name)
                table = _root_table(row, roots)
                for other, tables in all_objects.items():
                    if other != name and table in tables:
                        holders[key].add(other)
                        parents[key] = table
        for (object_id, ctype), names in holders.items():
            if len(names) > 1:
                component = self.resolver.resolve(object_id, ctype, parents.get((object_id, ctype)))
                self.cross_target.append({
                    "name": component.name, "type": component.label,
                    "solutions": sorted(names, key=str.lower),
                })
        self.cross_target.sort(key=lambda item: item["name"].lower())
        if self.cross_target:
            self.warnings.append(
                f"Components in more than one target solution: {len(self.cross_target)}. "
                "The solution imported last decides what they look like."
            )

    def _add_shared_rows(self, target_name: str, target_rows: List[dict], other_rows: List[dict], solution_id: str) -> None:
        assert self.resolver is not None
        target = {norm_guid(row.get("objectid")): row for row in target_rows if not is_table_shell(row)}
        all_objects = {
            norm_guid(row.get("objectid")) for row in target_rows
            if int(row.get("componenttype", 0)) == 1 and row.get("rootcomponentbehavior") == 0
        }
        roots = {norm_guid(row.get("solutioncomponentid")): row for row in other_rows}
        other_name = str(self.resolver.solution(solution_id).get("uniquename"))
        for row in other_rows:
            object_id = norm_guid(row.get("objectid"))
            table = _root_table(row, roots)
            through_table = table in all_objects
            if is_table_shell(row) or not (object_id in target or through_table):
                continue
            component = self.resolver.resolve(object_id, row.get("componenttype"), table or None)
            detail = (
                f"{target_name} holds this component's table with all objects, so exporting {target_name} also exports what {other_name} added."
                if through_table and object_id not in target else
                f"{target_name} and {other_name} both hold this component, so exporting {target_name} also exports {other_name}'s edits to it."
            )
            stored = self._store(Row(
                dependent_id="", dependent_name=f"{target_name} and {other_name}", dependent_type="Solutions",
                required=None, required_id=component.id, required_name=component.name, required_type=component.label,
                status=IN_FLIGHT, owners=other_name, evidence="Both solutions include this component",
                detail=detail, target_solutions={target_name},
            ))
            self.sources[stored.component_key].add("shared")

    def _store(self, row: Row) -> Row:
        existing = self.rows.get(row.merge_key)
        if existing is None:
            self.rows[row.merge_key] = row
            return row
        existing.depth = min(existing.depth, row.depth)
        existing.target_solutions |= row.target_solutions
        return existing

    # Source 2: the text scan ----------------------------------------------------------
    def scan_sources(self) -> None:
        roots = self.config.scan_dirs
        if not roots:
            return
        assert self.resolver is not None
        tokens = self._in_flight_tokens()
        self.coverage["text_scan"] = True
        self.coverage["names_searched"] = len(tokens)
        self.log(f"Text scan: names to search for: {len(tokens)}")
        real_hits: Set[Tuple[str, int]] = set()
        generated_hits: Dict[Tuple[str, int], Tuple[str, int, int, str]] = {}
        skipped: List[Tuple[str, str]] = []
        build_folders: List[str] = []
        for root in roots:
            for label, text in iter_text_files(root, skipped, build_folders):
                self.coverage["files_scanned"] += 1
                self.scanned_targets |= self._targets_of_file(label)
                kind = classify_code(label, text)
                if label.lower().endswith((".cs", ".vb")) and kind != "test":
                    self.source_classes.update(name.lower() for name in CLASS_RE.findall(text))
                for key, (line, count, token) in scan_text(text, tokens).items():
                    if kind == "generated":
                        generated_hits.setdefault(key, (label, line, count, token))
                        continue
                    real_hits.add(key)
                    self._add_text_row(key, label, line, count, token, note=TEST_NOTE if kind == "test" else "")
        # Generated early-bound classes list every table, so a name found only there isn't a use.
        for key, (label, line, count, token) in generated_hits.items():
            if key not in real_hits:
                self._add_text_row(key, label, line, count, token, explanation=GENERATED_NOTE)
        self.coverage["files_not_read"] = len(skipped)
        self.coverage["build_folders_not_searched"] = sorted(set(build_folders))
        for item in self.config.sources:
            if item.get("submodules_skipped"):
                name = str(item.get("name"))
                skipped.append((f"submodules of {name}", "This repository uses Git submodules, which weren't downloaded. Scan them too, or confirm they hold nothing that ships."))
        for label, reason in skipped:
            row = self._store(Row(
                dependent_id="", dependent_name=label, dependent_type="File", required=None, required_id="",
                required_name=f"Not searched: {label}", required_type="File", status=UNKNOWN, owners="",
                detail=reason, evidence=label, target_solutions=self._targets_of_file(label),
            ))
            self.sources[row.component_key].add("text")

    def _targets_of_file(self, label: str) -> Set[str]:
        """A folder named after a target belongs to it. Repository folders aren't tied to one."""
        if len(self.target_names) == 1:
            return set(self.target_names)
        folder = label.split("/", 1)[0].lower()
        return {name for name in self.target_names if name.lower() == folder}

    def _in_flight_tokens(self) -> Dict[str, List[Component]]:
        assert self.resolver is not None
        try:
            solutions = self._in_flight_solutions()
        except ApiError as exc:
            raise AuditError(f"Could not list the in-flight solutions: {exc}") from None
        errors: List[str] = []
        unsearchable: Set[str] = set()
        tokens: Dict[str, List[Component]] = {}
        for solution_id in solutions:
            try:
                for token, components in solution_tokens(self.resolver, solution_id, errors, unsearchable).items():
                    bucket = tokens.setdefault(token, [])
                    known = {item.key for item in bucket}
                    bucket.extend(item for item in components if item.key not in known)
            except ApiError as exc:
                errors.append(f"solution {solution_id}: {exc}")
        self._add_tables_in_no_solution(tokens)
        own_tokens: Dict[str, List[Component]] = {}
        try:
            for solution_id in self.target_ids:
                for token, components in solution_tokens(self.resolver, solution_id, errors).items():
                    own_tokens.setdefault(token, []).extend(components)
        except ApiError as exc:
            raise AuditError(f"Could not list the target's components: {exc}") from None
        # Drop only the targets' own components: another table can use the same column name.
        own_keys = {item.key for components in own_tokens.values() for item in components}
        self.own_names = set(own_tokens)
        for token in list(tokens):
            tokens[token] = [item for item in tokens[token] if item.key not in own_keys]
            if not tokens[token]:
                del tokens[token]
        self.coverage["unnamed_components"] = len(errors)
        self.coverage["names_not_searchable"] = len(unsearchable)
        if unsearchable:
            self.warnings.append(
                f"Names too short to search, or containing spaces: {len(unsearchable)}. The scan looks for their GUIDs only."
            )
        if errors:
            self.warnings.append(
                f"Components the text scan can't search for, because they couldn't be named: {len(errors)}. "
                f"First: {errors[0]}"
            )
        return tokens

    def _add_tables_in_no_solution(self, tokens: Dict[str, List[Component]]) -> None:
        """Unmanaged custom tables can sit in no solution at all, yet still be in development."""
        assert self.resolver is not None
        try:
            rows = self.dev.get_all(
                "EntityDefinitions",
                {"$select": "MetadataId", "$filter": "IsCustomEntity eq true and IsManaged eq false"},
            )
            for row in rows:
                table_id = norm_guid(row.get("MetadataId"))
                add_tokens(tokens, self.resolver.resolve(table_id, 1))
                for column_id, _table, _column in self.resolver.index_table_columns(table_id):
                    add_tokens(tokens, self.resolver.resolve(column_id, 2, table_id))
        except ApiError as exc:
            self.incomplete.append(f"Could not list unmanaged custom tables, so tables in no solution weren't searched: {exc}")

    def _in_flight_solutions(self) -> List[str]:
        if self.config.in_flight:
            names = self.config.in_flight
            found: List[str] = []
            for name in names:
                rows = self.dev.get_all(
                    "solutions", {"$filter": f"uniquename eq '{odata_text(name)}'", "$select": "solutionid"}
                )
                if not rows:
                    raise AuditError(f"--in-flight solution '{name}' wasn't found in DEV")
                found.append(norm_guid(rows[0].get("solutionid")))
            return [solution_id for solution_id in found if solution_id not in self.target_ids]
        rows = self.dev.get_all(
            "solutions",
            {"$filter": "ismanaged eq false and isvisible eq true", "$select": "solutionid,uniquename"},
        )
        return [
            norm_guid(row.get("solutionid"))
            for row in rows
            if str(row.get("uniquename", "")).lower() not in IGNORED_SOLUTIONS
            and norm_guid(row.get("solutionid")) not in self.target_ids
        ]

    def _add_text_row(
        self, key: Tuple[str, int], label: str, line: int, count: int, token: str,
        note: str = "", explanation: str = "",
    ) -> None:
        assert self.resolver is not None
        component = self.resolver.resolve(*key)
        verdict = self.resolver.classify(component)
        if verdict.status == IN_TARGET:
            return
        if not note and token in self.own_names:
            note = f"The name {token} also exists in a target solution, so this hit may be the target's own component. Confirm."
        row = Row(
            dependent_id="", dependent_name=label, dependent_type="File",
            required=component, required_id=component.id, required_name=component.name,
            required_type=component.label, status=verdict.status, owners=verdict.owners,
            detail=verdict.detail, evidence=f"{label}:{line} matched {token} ({count} hits)",
            note=note, explanation=explanation, target_solutions=self._targets_of_file(label),
        )
        self._store(row)
        self.sources[row.component_key].add("text")

    # Plug-in assemblies ---------------------------------------------------------------
    def check_plugin_types(self) -> None:
        """Compare the plug-in types registered for the targets with the classes in the branches."""
        if not self.config.scan_dirs:
            return
        assemblies: Dict[str, Set[str]] = defaultdict(set)
        for solution_id, name in self.target_ids.items():
            try:
                rows = self.dev.get_all(
                    "solutioncomponents",
                    {"$filter": f"_solutionid_value eq {solution_id} and componenttype eq 91", "$select": "objectid"},
                )
            except ApiError as exc:
                self._add_code_row(name, "Plug-in assemblies", UNKNOWN, f"Could not list them: {exc}", {name})
                continue
            for row in rows:
                assemblies[norm_guid(row.get("objectid"))].add(name)
        for assembly_id, holders in assemblies.items():
            self._check_assembly(assembly_id, holders)

    def _plugin_types(self, client: ReadOnlyClient, assembly_id: str) -> List[str]:
        rows = client.get_all(
            "plugintypes", {"$filter": f"_pluginassemblyid_value eq {assembly_id}", "$select": "typename"}
        )
        return [str(row.get("typename")) for row in rows if row.get("typename")]

    def _check_assembly(self, assembly_id: str, holders: Set[str]) -> None:
        name = f"Assembly {assembly_id}"
        try:
            name = str(self.dev.get(f"pluginassemblies({assembly_id})", {"$select": "name"}).get("name") or name)
            dev_types = self._plugin_types(self.dev, assembly_id)
            matches = self.prod.get_all(
                "pluginassemblies",
                {"$filter": f"name eq '{odata_text(name)}'", "$select": "pluginassemblyid", "$top": "1"},
            )
            prod_types = self._plugin_types(self.prod, norm_guid(matches[0].get("pluginassemblyid"))) if matches else []
        except ApiError as exc:
            self._add_code_row(name, "All plug-in types", UNKNOWN, f"Could not read the registered types: {exc}", holders)
            return

        def defined(type_name: str) -> bool:
            return type_name.rsplit(".", 1)[-1].lower() in self.source_classes

        registered = set(dev_types) | set(prod_types)
        if registered and not any(defined(type_name) for type_name in registered):
            self._add_code_row(
                name, "All plug-in types", UNKNOWN,
                f"No scanned branch defines any of this assembly's {len(registered)} registered types. Add the repository that builds it.",
                holders,
            )
            return
        for type_name in sorted(prod_types):
            if not defined(type_name):
                self._add_code_row(
                    name, type_name, UNKNOWN,
                    "Production registers this type, but no scanned branch defines it. Updating an assembly can fail while a registered type is missing.",
                    holders,
                )
        for type_name in sorted(set(dev_types) - set(prod_types)):
            if defined(type_name):
                self._add_code_row(
                    name, type_name, IN_FLIGHT,
                    "This branch contains a type production doesn't have. An assembly ships whole, so confirm it belongs in this release.",
                    holders,
                )

    def _add_code_row(self, assembly: str, required: str, status: str, detail: str, targets: Set[str]) -> None:
        row = Row(
            dependent_id="", dependent_name=assembly, dependent_type="Plug-in assembly", required=None,
            required_id="", required_name=required, required_type="Plug-in type", status=status,
            owners="Source branch", detail=detail, evidence="Registered types compared with scanned classes",
            target_solutions=set(targets),
        )
        row = self._store(row)
        self.sources[row.component_key].add("code")

    # Source 3: the dry run ------------------------------------------------------------
    def merge_stage(self) -> None:
        assert self.resolver is not None
        for path in self.config.stage_jsons:
            self._merge_stage_file(path)
        self.coverage["dry_run"] = all(name in self.dry_runs for name in self.target_names)

    def _merge_stage_file(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise AuditError(f"Could not read the dry run in {path}: {exc}") from None
        results = data.get("StageSolutionResults", data) if isinstance(data, dict) else None
        missing = results.get("MissingDependencies") if isinstance(results, dict) else None
        if missing is None and isinstance(results, dict) and "MissingDependencies" in results:
            missing = []
        if not isinstance(missing, list):
            raise AuditError(f"{path} has no MissingDependencies list, so it isn't a StageSolution response")
        target = self._stage_target(results, path)
        if target is None:
            return
        self._check_stage_result(target, results, missing)
        self.log(f"Dry run of {target}: missing dependencies: {len(missing)}")
        by_name = {row.required_name.lower(): row.component_key for row in self.rows.values()}
        for item in missing:
            self._add_stage_row(item, by_name, target)

    def _stage_target(self, results: dict, path: Path) -> Optional[str]:
        """Each dry run must belong to one target, and each target gets one dry run."""
        details = results.get("SolutionDetails") or {}
        staged = str(details.get("SolutionUniqueName") or "")
        target = next((name for name in self.target_names if name.lower() == staged.lower()), None)
        if target is None:
            expected = ", ".join(self.target_names)
            self.incomplete.append(f"The dry run in {path.name} tested '{staged or 'an unnamed solution'}', not {expected}.")
            return None
        if target in self.dry_runs:
            self.incomplete.append(f"There are two dry runs for {target}. Keep only the latest one.")
            return None
        return target

    def _check_stage_result(self, target: str, results: dict, missing: List[dict]) -> None:
        """A dry run only counts if it passed, and if it tested the version DEV has now."""
        status = results.get("StageSolutionStatus")
        self.dry_runs[target] = str(status)
        if status not in ("Passed", 1) and not missing:
            self.incomplete.append(f"The dry run of {target} didn't pass (status: {status}), and it lists no missing dependency.")
        details = results.get("SolutionDetails") or {}
        staged_version = str(details.get("SolutionVersion") or "")
        if version_tuple(staged_version) != version_tuple(self.target_versions[target]):
            self.incomplete.append(
                f"The dry run of {target} tested version {staged_version or 'unknown'}, "
                f"but DEV has {self.target_versions[target]}. Run it again."
            )
        for item in results.get("SolutionValidationResults") or []:
            if not isinstance(item, dict):
                self.stage_validation.append(f"{target}: {item}")
                continue
            message = str(item.get("Message") or item.get("AdditionalInfo") or item)
            self.stage_validation.append(f"{target}: {message}")
            if item.get("SolutionValidationResultType") in ("Error", 2):
                self.incomplete.append(f"The dry run of {target} reported an error: {message[:200]}")

    def _add_stage_row(self, item: dict, by_name: Dict[str, str], target: str) -> None:
        assert self.resolver is not None
        required_id = norm_guid(item.get("RequiredComponentId"))
        schema = str(item.get("RequiredComponentSchemaName") or item.get("RequiredComponentDisplayName") or "")
        parent = str(item.get("RequiredComponentParentSchemaName") or "")
        name = f"{parent}.{schema}".lower() if parent and schema else schema.lower()
        component_key = None
        # Two components can share a name, so a name only counts when there's no ID to compare.
        for candidate in ([required_id] if required_id else [name, schema.lower()]):
            if candidate and (candidate in self.sources or candidate in by_name):
                component_key = candidate if candidate in self.sources else by_name[candidate]
                break
        if component_key is not None:
            self.sources[component_key].add("stage")
            self.stage_hits[component_key].add(target)
            attributed = False
            for row in self.rows.values():
                if row.component_key != component_key:
                    continue
                attributed = attributed or target in row.target_solutions
                if row.status == IN_TARGET and row.holding_targets and target not in row.holding_targets:
                    # Expected: another target supplies it, and that target isn't in the sandbox yet.
                    self._add_target_edge(target, row.holding_targets, row.required_name)
                elif row.status in (IN_PRODUCTION, IN_TARGET):
                    row.note = f"The dry run reports this as missing, but the graph says {row.status}."
                    row.status = UNKNOWN
            if attributed:
                return
        self._add_stage_only_row(item, required_id, schema, parent, target)

    def _add_stage_only_row(self, item: dict, required_id: str, schema: str, parent: str, target: str) -> None:
        assert self.resolver is not None
        status, owners, detail = UNKNOWN, "", "Reported only by the dry run"
        name = f"{parent}.{schema}" if parent and schema else (schema or required_id or "Unnamed component")
        reported_by = str(item.get("RequiredSolutionName") or "")
        holders = tuple(other for other in self.target_names if other.lower() == reported_by.lower())
        if required_id:
            try:
                rows = self.dev.get_all(
                    "solutioncomponents",
                    {"$filter": f"objectid eq {required_id}", "$select": "_solutionid_value"},
                )
                ids = [norm_guid(row.get("_solutionid_value")) for row in rows]
                holders = tuple(sorted({self.target_ids[item_id] for item_id in ids if item_id in self.target_ids}, key=str.lower)) or holders
                infos = [self.resolver.solution(item_id) for item_id in ids]
                names = [
                    str(info.get("uniquename")) for info in infos
                    if str(info.get("uniquename", "")).lower() not in IGNORED_SOLUTIONS
                    and info.get("isvisible") is not False and not info.get("ismanaged")
                ]
                if names:
                    status, owners = IN_FLIGHT, "; ".join(names)
            except ApiError as exc:
                detail = f"Reported only by the dry run; owner check failed: {exc}"
        if target in holders:
            status = UNKNOWN
            detail = f"The dry run of {target} reports this as missing, but {target} holds it."
        elif holders:
            status, detail = IN_TARGET, ""
            owners = owners or "; ".join(holders)
            self._add_target_edge(target, holders, name)
        row = Row(
            dependent_id=norm_guid(item.get("DependentComponentId")),
            dependent_name=str(item.get("DependentComponentSchemaName") or item.get("DependentComponentDisplayName") or "Unknown component"),
            dependent_type=str(item.get("DependentComponentType") or "Unknown"),
            required=None, required_id=required_id, required_name=name,
            required_type=str(item.get("RequiredComponentType") or "Unknown"),
            status=status, owners=owners or reported_by, detail=detail,
            evidence="StageSolution MissingDependencies", target_solutions={target}, holding_targets=holders,
        )
        row = self._store(row)
        self.sources[row.component_key].add("stage")
        self.stage_hits[row.component_key].add(target)

    # Release order --------------------------------------------------------------------
    def order_targets(self) -> None:
        self.release_order, self.release_cycle, self.release_waiting = plan_release(
            self.target_names, set(self.target_edges)
        )
        if not self.release_cycle:
            return
        members = set(self.release_cycle)
        links = [
            f"{needs} needs {needed} ({_sample(components)})"
            for (needs, needed), components in sorted(self.target_edges.items())
            if needs in members and needed in members
        ]
        row = self._store(Row(
            dependent_id="", dependent_name=" and ".join(self.release_cycle), dependent_type="Solution",
            required=None, required_id="", required_name="Circular dependency", required_type="Solution",
            status=UNKNOWN, owners="; ".join(self.release_cycle),
            detail="These solutions can't be imported in any order. " + "; ".join(links)
            + ". Merge them, or move what they share into one of them.",
            evidence="Dependencies between target solutions", target_solutions=members,
        ))
        self.sources[row.component_key].add("order")

    # Reconcile ------------------------------------------------------------------------
    def reconcile(self) -> None:
        for row in self.rows.values():
            if row.status not in ACTIONABLE or row.note:
                continue
            sources = self.sources[row.component_key]
            tested = row.target_solutions & set(self.dry_runs)
            found_by_graph = "graph" in sources and row.dependent_type not in JUDGED_ROW_TYPES
            if tested and found_by_graph and not tested & self.stage_hits[row.component_key] and row.depth == 1:
                row.note = "Found by the graph but not by the dry run. Explain why."
            elif sources == {"text"}:
                row.note = "Found only by the text scan. Confirm it's a real reference: the graph doesn't track it."
            elif sources == {"stage"}:
                row.note = "Found only by the dry run. Explain why the graph missed it."

    # Impact ---------------------------------------------------------------------------
    def compute_impact(self) -> None:
        if self.config.skip_impact:
            return
        assert self.resolver is not None
        done: Dict[Tuple[str, int], Optional[int]] = {}
        for row in self.rows.values():
            component = row.required
            if component is None or row.status not in ACTIONABLE or not component.id:
                continue
            if component.key not in done:
                done[component.key] = self._impact_of(component)
            count = done[component.key]
            row.used_by = "" if count is None else str(count)

    def _impact_of(self, component: Component) -> Optional[int]:
        assert self.resolver is not None
        path = f"RetrieveDependentComponents(ObjectId=@o,ComponentType=@t)?@o={component.id}&@t={component.type}"
        try:
            rows = self.dev.get_all(path)
        except ApiError as exc:
            self.warnings.append(f"Could not read who uses {component.name}: {exc}")
            return None
        for raw in rows:
            dependent_id = raw.get("dependentcomponentobjectid")
            dependent_type = raw.get("dependentcomponenttype")
            if not dependent_id or dependent_type is None:
                continue
            dependent = self.resolver.resolve(dependent_id, dependent_type, raw.get("dependentcomponentparentid"))
            try:
                owners, _ = self.resolver.owners(dependent)
                owner_text = "; ".join(str(info.get("uniquename")) for info, _ in owners)
            except ApiError as exc:
                owner_text = f"Unknown: {exc}"
            self.impact.append({
                "required_name": component.name, "required_type": component.label,
                "dependent_name": dependent.name, "dependent_type": dependent.label,
                "dependent_owner_solutions": owner_text, "dependent_id": dependent.id,
            })
        return len(rows)

    # Output ---------------------------------------------------------------------------
    def carry_explanations(self) -> None:
        """Keep the explanations people added on an earlier run."""
        previous = read_explanations(self.config.out / "dependencies.csv", self.warnings)
        for row in self.rows.values():
            carried = _unsafe(previous.get(row.stable_id, "") or "")
            # The automatic note describes the last scan, so this scan decides it again.
            if carried and carried != GENERATED_NOTE:
                row.explanation = carried
        unclosable = sum(
            1 for row in self.rows.values()
            if row.status in ACTIONABLE and not self._closed_as(row)
            and row.explanation.lower().startswith((FALSE_POSITIVE.lower(), ACCEPTED.lower()))
        )
        if unclosable:
            self.warnings.append(
                f"Rows whose explanation can't close them: {unclosable}. In-flight and Version too old dependencies, "
                "and circular dependencies, close only when they're fixed."
            )

    def ordered_rows(self) -> List[Row]:
        return sorted(
            self.rows.values(),
            key=lambda row: (STATUS_ORDER.get(row.status, 9), row.depth, row.required_name.lower(), row.dependent_name.lower()),
        )

    def write_outputs(self) -> None:
        self.config.out.mkdir(parents=True, exist_ok=True)
        self._write_dependencies()
        self._write_impact()
        (self.config.out / "dependency-map.md").write_text(self._map_markdown(), encoding="utf-8")
        (self.config.out / "run-log.json").write_text(json.dumps(self._run_log(), indent=2), encoding="utf-8")

    def _write_dependencies(self) -> None:
        with (self.config.out / "dependencies.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in self.ordered_rows():
                writer.writerow({key: _safe(value) for key, value in {
                    "id": row.stable_id, "status": row.status,
                    "target_solutions": "; ".join(sorted(row.target_solutions, key=str.lower)),
                    "dependent_name": row.dependent_name,
                    "dependent_type": row.dependent_type, "required_name": row.required_name,
                    "required_type": row.required_type,
                    "dependency_type": "; ".join(sorted(row.dependency_types)), "depth": row.depth,
                    "owning_solutions": row.owners, "production_detail": row.detail,
                    "sources": "; ".join(sorted(self.sources[row.component_key])), "evidence": row.evidence,
                    "used_by_count": row.used_by, "reconcile_note": row.note, "explanation": row.explanation,
                    "dependent_id": row.dependent_id, "required_id": row.required_id,
                }.items()})

    def _write_impact(self) -> None:
        columns = ["required_name", "required_type", "dependent_name", "dependent_type", "dependent_owner_solutions", "dependent_id"]
        with (self.config.out / "impact.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for item in self.impact:
                writer.writerow({key: _safe(value) for key, value in item.items()})

    @staticmethod
    def _closed_as(row: Row) -> str:
        """Return how a person closed a row. A real dependency closes only when it's fixed."""
        if row.status not in ACTIONABLE or row.dependent_type == CYCLE_ROW_TYPE:
            return ""
        text = row.explanation.lower()
        judged = row.dependent_type in JUDGED_ROW_TYPES
        if judged and text.startswith(FALSE_POSITIVE.lower()):
            return FALSE_POSITIVE
        if (judged or row.status == UNKNOWN) and text.startswith(ACCEPTED.lower()):
            return ACCEPTED
        return ""

    def _open_rows(self) -> List[Row]:
        return [row for row in self.ordered_rows() if row.status in ACTIONABLE and not self._closed_as(row)]

    def counts(self) -> Counter:
        counter: Counter = Counter()
        for row in self.rows.values():
            counter[self._closed_as(row) or row.status] += 1
        return counter

    def gate_blockers(self) -> List[str]:
        blockers = list(self.incomplete)
        if not self.coverage["text_scan"]:
            blockers.append("The text scan didn't run.")
        else:
            if not self.coverage["files_scanned"]:
                blockers.append("The text scan read no files.")
            if self.coverage["unnamed_components"]:
                blockers.append(f"Components the text scan couldn't search for: {self.coverage['unnamed_components']}.")
            unscanned = [name for name in self.target_names if name not in self.scanned_targets]
            if self.coverage["files_scanned"] and unscanned:
                blockers.append(
                    f"No scanned folder is named after {', '.join(unscanned)}, so its unpacked files weren't searched."
                )
        without_run = [name for name in self.target_names if name not in self.dry_runs]
        if len(without_run) == len(self.target_names):
            blockers.append("The dry run isn't included.")
        elif without_run:
            blockers.append(f"There's no dry run for {', '.join(without_run)}.")
        if self.release_cycle:
            blockers.append(f"These target solutions need each other, so they can't be imported: {', '.join(self.release_cycle)}.")
        open_rows = self._open_rows()
        unknown = sum(1 for row in open_rows if row.status == UNKNOWN and not row.explanation)
        if unknown:
            blockers.append(f"Unknown rows with no explanation: {unknown}.")
        explained = {row.component_key for row in open_rows if row.explanation}
        unexplained = {row.component_key for row in open_rows if row.note} - explained
        if unexplained:
            blockers.append(f"Components found by only some sources, with no explanation: {len(unexplained)}.")
        return blockers

    def _hot_spots(self) -> List[dict]:
        grouped: Dict[str, List[Row]] = defaultdict(list)
        for row in self._open_rows():
            grouped[row.component_key].append(row)
        spots = []
        for rows in grouped.values():
            first = rows[0]
            needed_by = set().union(*(row.target_solutions for row in rows))
            spots.append({
                "name": first.required_name, "type": first.required_type, "status": first.status,
                "owners": first.owners, "rows": len(rows), "used_by": first.used_by,
                "needed_by": ", ".join(sorted(needed_by, key=str.lower)),
            })
        return sorted(spots, key=lambda spot: (-spot["rows"], spot["name"].lower()))

    def _map_markdown(self) -> str:
        counts = self.counts()
        blockers = self.gate_blockers()
        lines = [
            f"# Dependency map: {', '.join(self.target_names)}", "",
            f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC by solution_dependency_audit {__version__}.",
            f"DEV: {self.dev.base_url}. Production: {self.prod.base_url}.", "",
            "## Status", "", "| Status | Rows |", "| --- | --- |",
        ]
        for status in (UNKNOWN, IN_FLIGHT, VERSION_TOO_OLD, FALSE_POSITIVE, ACCEPTED, IN_PRODUCTION, IN_TARGET):
            lines.append(f"| {status} | {counts.get(status, 0)} |")
        lines += ["", "## Gate 2", ""]
        lines += ["Ready."] if not blockers else [f"- {blocker}" for blocker in blockers]
        lines += self._release_markdown()
        lines += [
            "", "## Coverage", "",
            f"- Text scan: files read {self.coverage['files_scanned']}, files not read {self.coverage['files_not_read']}, "
            f"names searched {self.coverage['names_searched']}, components that couldn't be named {self.coverage['unnamed_components']}",
            "- Dry run: " + ", ".join(f"{name} {self.dry_runs.get(name, 'not included')}" for name in self.target_names),
        ]
        build_folders = self.coverage.get("build_folders_not_searched") or []
        if build_folders:
            shown = ", ".join(build_folders[:10]) + (f" and {len(build_folders) - 10} more" if len(build_folders) > 10 else "")
            lines.append(f"- Build folders not searched in repositories: {_md(shown)}")
        if self.stage_validation:
            lines += ["", "## Dry-run validation messages", ""] + [f"- {_md(item)}" for item in self.stage_validation]
        lines += _sources_markdown(self.config.sources)
        if self.cross_target:
            lines += [
                "", "## Components in more than one target solution", "",
                "This doesn't block an import, but the solution imported last decides what these look like. Keep each in one solution.", "",
                "| Component | Type | Target solutions |", "| --- | --- | --- |",
            ]
            lines += [
                f"| {_md(item['name'])} | {_md(item['type'])} | {_md(', '.join(item['solutions']))} |"
                for item in self.cross_target
            ]
        lines += ["", "## Fix these first", "", "Components ranked by how many rows a fix would clear.", ""]
        spots = self._hot_spots()
        if spots:
            lines += [
                "| Rows | Required component | Type | Status | Owning solutions | Needed by | Used by |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            lines += [
                f"| {spot['rows']} | {_md(spot['name'])} | {_md(spot['type'])} | {spot['status']} | {_md(spot['owners'])} | {_md(spot['needed_by'])} | {spot['used_by']} |"
                for spot in spots
            ]
        else:
            lines.append("No open rows.")
        lines += ["", "## Solutions", ""] + self._solution_diagram()
        lines += ["", "## Structure", ""] + self._mermaid()
        if self.warnings:
            lines += ["", "## Warnings", ""] + [f"- {_md(item)}" for item in self.warnings]
        return "\n".join(lines) + "\n"

    def _release_markdown(self) -> List[str]:
        lines = ["", "## Release order", ""]
        if len(self.target_names) == 1:
            return lines + ["There's one target solution, so there's nothing to order."]
        if self.release_order:
            lines += ["Import in this order. Each solution needs only those above it.", ""]
        for index, name in enumerate(self.release_order, 1):
            needs = [
                f"{needed}: {len(components)}" for (source, needed), components in sorted(self.target_edges.items())
                if source == name
            ]
            lines.append(f"{index}. {name}" + (f" (components needed from {', '.join(needs)})" if needs else ""))
        if self.release_cycle:
            lines += ["", f"**Can't be ordered:** {', '.join(self.release_cycle)} need each other. See the Circular dependency row."]
        if self.release_waiting:
            lines += ["", f"Waiting for those: {', '.join(self.release_waiting)}."]
        lines += ["", "Only Dataverse dependencies decide this order. References in code don't, because they don't stop an import."]
        return lines

    def _solution_diagram(self) -> List[str]:
        """One box per solution, with an arrow to whatever it needs. The numbers count components."""
        node_ids: Dict[str, str] = {name: f"t{index}" for index, name in enumerate(self.target_names, 1)}
        counts: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        for (needs, needed), components in self.target_edges.items():
            counts[(needs, needed)] |= components
        others: Dict[str, str] = {}
        for row in self._open_rows():
            if row.status != IN_FLIGHT:
                continue
            needers = row.target_solutions or {"Repository code"}
            for owner in row.owners.split(";"):
                owner = owner.replace(" (via table)", "").strip()
                owner = "Not in a solution" if owner == "None" else owner
                if not owner or owner == "Source branch" or owner in self.target_names:
                    continue
                others.setdefault(owner, f"o{len(others) + 1}")
                for needer in needers:
                    counts[(needer, owner)].add(row.component_key)
        if not counts:
            return ["No target solution needs another solution."]
        if any(needs == "Repository code" for needs, _ in counts):
            node_ids["Repository code"] = "repo"
        node_ids.update(others)
        lines = ["```mermaid", "flowchart LR", '  subgraph targets["In this release"]']
        for name, node in node_ids.items():
            if name not in others:
                suffix = ":::unknown" if name in self.release_cycle else ""
                lines.append(f'    {node}["{_mermaid_text(name)}"]{suffix}')
        lines.append("  end")
        if others:
            lines.append('  subgraph others["Not in this release"]')
            lines += [f'    {node}["{_mermaid_text(name)}"]:::inflight' for name, node in others.items()]
            lines.append("  end")
        for (needs, needed), components in sorted(counts.items()):
            lines.append(f'  {node_ids[needs]} -->|"{len(components)}"| {node_ids[needed]}')
        lines += [
            "  classDef inflight fill:#fde2e1,stroke:#b42318,color:#111",
            "  classDef unknown fill:#e8e8e8,stroke:#555,color:#111",
            "```", "",
            "Each arrow points to what a solution needs, and its number counts the components. Red solutions block the release. Grey solutions are in a circular dependency.",
        ]
        return lines

    def _mermaid(self) -> List[str]:
        open_rows = [row for row in self._open_rows() if row.dependent_type != "Solution"]
        if not open_rows:
            return ["No open rows to draw."]
        shown = open_rows[: self.config.map_limit]
        node_ids: Dict[str, str] = {}
        boxes: Dict[str, Dict[str, str]] = defaultdict(dict)
        edges: List[str] = []

        def node(key: str, label: str, box: str, status: str = "") -> str:
            if key not in node_ids:
                node_ids[key] = f"n{len(node_ids) + 1}"
                suffix = {IN_FLIGHT: ":::inflight", VERSION_TOO_OLD: ":::version", UNKNOWN: ":::unknown"}.get(status, "")
                boxes[box][node_ids[key]] = f'{node_ids[key]}["{_mermaid_text(label)}"]{suffix}'
            return node_ids[key]

        def owning_box(owners: str) -> str:
            first = owners.split(";")[0].strip().replace(" (via table)", "")
            return first if first and first != "None" else "Not in a solution"

        def target_box(row: Row) -> str:
            return " and ".join(sorted(row.target_solutions, key=str.lower)) or "Repository code"

        required_rows = {row.required_id or row.required_name.lower(): row for row in open_rows}
        for row in shown:
            required_node = node(row.component_key, row.required_name, owning_box(row.owners), row.status)
            if row.dependent_type == "File":
                box = target_box(row)
                dependent_node = node(f"files:{box}", "Source files", box)
                edges.append(f"{dependent_node} -.-> {required_node}")
                continue
            dependent_key = row.dependent_id or row.dependent_name.lower()
            upstream = required_rows.get(dependent_key)
            if row.depth > 1 and upstream is not None:
                dependent_node = node(dependent_key, row.dependent_name, owning_box(upstream.owners), upstream.status)
            else:
                dependent_node = node(dependent_key, row.dependent_name, target_box(row))
            edges.append(f"{dependent_node} --> {required_node}")
        lines = ["```mermaid", "flowchart LR"]
        for index, (box, nodes) in enumerate(sorted(boxes.items()), 1):
            lines.append(f'  subgraph s{index}["{_mermaid_text(box)}"]')
            lines += [f"    {definition}" for definition in nodes.values()]
            lines.append("  end")
        lines += [f"  {edge}" for edge in dict.fromkeys(edges)]
        lines += [
            "  classDef inflight fill:#fde2e1,stroke:#b42318,color:#111",
            "  classDef version fill:#fff1cc,stroke:#b54708,color:#111",
            "  classDef unknown fill:#e8e8e8,stroke:#555,color:#111",
            "```", "",
            "Red is In-flight, amber is Version too old and grey is Unknown. Dashed arrows come from the text scan.",
        ]
        if len(open_rows) > len(shown):
            lines.append(f"The diagram shows the first {len(shown)} of {len(open_rows)} open rows. See dependencies.csv for all of them.")
        return lines

    def _run_log(self) -> dict:
        return {
            "version": __version__,
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "targets": [{"name": name, "version": self.target_versions.get(name, "")} for name in self.target_names],
            "release_order": {"order": self.release_order, "cycle": self.release_cycle, "waiting": self.release_waiting},
            "dev": {"url": self.dev.base_url, "organization": self.organizations.get("DEV", ""), "requests": self.dev.request_count},
            "production": {"url": self.prod.base_url, "organization": self.organizations.get("PROD", ""), "requests": self.prod.request_count},
            "counts": dict(self.counts()),
            "coverage": self.coverage,
            "sources": self.config.sources,
            "dry_runs": self.dry_runs,
            "components_in_several_targets": self.cross_target,
            "gate_2_blockers": self.gate_blockers(),
            "warnings": self.warnings,
        }

    def report(self) -> int:
        counts = self.counts()
        self.log("Rows: " + ", ".join(f"{status} {count}" for status, count in sorted(counts.items())))
        for warning in self.warnings:
            self.log(f"Warning: {warning}")
        if len(self.target_names) > 1:
            self.log("Release order: " + (", then ".join(self.release_order) or "none"))
            if self.release_cycle:
                self.log(f"Can't be ordered: {', '.join(self.release_cycle)} need each other")
        blockers = self.gate_blockers()
        self.log("Gate 2: ready" if not blockers else "Gate 2: not ready. " + " ".join(blockers))
        self.log(f"Output: {self.config.out}")
        return 3 if self._open_rows() or blockers else 0


def _sources_markdown(sources: List[dict]) -> List[str]:
    if not sources:
        return []
    lines = ["", "## Source branches", "", "| Repository | Branch | Commit | Kind |", "| --- | --- | --- | --- |"]
    lines += [
        f"| {_md(item.get('name'))} | {_md(item.get('branch'))} | {str(item.get('commit'))[:12]} | {_md(item.get('kind'))} |"
        for item in sources
    ]
    return lines


def _safe(value) -> str:
    """Stop a spreadsheet from running a cell that starts like a formula."""
    text = str(value if value is not None else "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _unsafe(value: str) -> str:
    return value[1:] if value[:2] in ("'=", "'+", "'-", "'@") else value


def _md(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _mermaid_text(value: str) -> str:
    cleaned = re.sub(r'["`<>\[\]{}\n\r]', " ", str(value)).strip()
    return cleaned[:60] + "..." if len(cleaned) > 60 else cleaned


# Command line -------------------------------------------------------------------------
def make_output_safe() -> None:
    """A redirected Windows console can't print every name, and a name mustn't stop a script."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def az_token(base_url: str) -> str:
    """Ask the Azure CLI for a Dataverse token. It stays in memory and is never written down."""
    executable = shutil.which("az") or "az"
    command = [executable, "account", "get-access-token", "--resource", base_url.rstrip("/"), "--query", "accessToken", "-o", "tsv"]
    try:
        result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditError(f"Could not run the Azure CLI: {exc}") from None
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise AuditError(
            f"The Azure CLI has no token for {base_url}. Run 'az login --tenant <tenant-id>'. {result.stderr.strip()[:300]}"
        )
    return token


def load_auth(auth_dir: Optional[str]):
    """Use auth.py from the Dataverse plugin when it's there, or else the Azure CLI.

    Returns a token provider and an optional header builder.
    """
    candidates = [Path(auth_dir)] if auth_dir else [Path.cwd() / "scripts", Path(__file__).resolve().parent]
    for directory in candidates:
        if (directory / "auth.py").is_file():
            sys.path.insert(0, str(directory))
            # Without this, auth.py looks up its version on GitHub. This tool isn't the plugin anyway.
            os.environ.setdefault("DATAVERSE_PLUGIN_VERSION", "unknown")
            os.environ.setdefault("DATAVERSE_SUPPRESS_FALLBACK_HINT", "1")
            auth = importlib.import_module("auth")

            def token_provider(base_url: str) -> str:
                return auth.get_token(f"{base_url.rstrip('/')}/.default")

            def header_builder(token: str) -> Dict[str, str]:
                # "unknown" is the plugin's allowed value for callers that aren't one of its own skills.
                return auth.get_plugin_headers("unknown", token)

            return token_provider, header_builder
    if auth_dir:
        raise AuditError(f"auth.py wasn't found in {auth_dir}. Leave out --auth-dir to sign in with the Azure CLI.")
    if shutil.which("az"):
        return az_token, None
    raise AuditError("There's no way to sign in. Install the Azure CLI and run 'az login', or pass --auth-dir with the folder holding auth.py.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only dependency audit for a release made of one or more Dataverse solutions.")
    parser.add_argument(
        "--target", required=True, action="append",
        help="Unique name of a solution to release. Repeat it, or separate names with commas, when the release has several",
    )
    parser.add_argument("--dev", required=True, help="DEV environment URL")
    parser.add_argument("--prod", required=True, help="Production environment URL, or a fresh copy of production")
    parser.add_argument(
        "--scan-dir", type=Path, action="append", default=[],
        help="Folder to search. Repeat for the unpacked target, the downloaded site, plug-ins, functions and SPFx",
    )
    parser.add_argument("--sources-lock", type=Path, help="sources-lock.json from fetch_audit_sources.py. Adds each branch to the scan")
    parser.add_argument(
        "--stage-json", type=Path, action="append", default=[],
        help="Saved StageSolution response from a sandbox. Repeat it for each target",
    )
    parser.add_argument("--in-flight", help="Comma-separated solutions to treat as in-flight. Default: every other unmanaged solution")
    parser.add_argument("--out", type=Path, default=Path("audit"), help="Output folder (default: audit)")
    parser.add_argument("--max-depth", type=int, default=10, help="How deep to walk each chain (default: 10)")
    parser.add_argument("--map-limit", type=int, default=150, help="Most rows to draw in the map (default: 150)")
    parser.add_argument("--skip-impact", action="store_true", help="Skip RetrieveDependentComponents")
    parser.add_argument(
        "--allow-same-org-id", action="store_true",
        help="Run even if DEV and production report one organization ID. Only use it once the admin center shows they're separate environments",
    )
    parser.add_argument("--auth-dir", help="Folder holding auth.py from the Dataverse plugin (default: ./scripts if it's there, or else the Azure CLI)")
    parser.add_argument("--check", action="store_true", help="Show which environments will be read, then stop")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def split_names(values: List[str]) -> List[str]:
    """Accept repeated flags and comma-separated lists, and drop repeats."""
    names: List[str] = []
    seen: Set[str] = set()
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    return names


def validate(args: argparse.Namespace) -> None:
    targets = split_names(args.target)
    if not targets:
        raise AuditError("--target needs at least one solution unique name")
    for name in targets:
        if not SOLUTION_NAME_RE.fullmatch(name):
            raise AuditError(f"--target '{name}' must be a solution unique name (letters, digits and underscores)")
    in_flight = {name.lower() for name in split_names([args.in_flight])} if args.in_flight else set()
    overlap = [name for name in targets if name.lower() in in_flight]
    if overlap:
        raise AuditError(f"{', '.join(overlap)} can't be both a target and in-flight")
    for name, url in (("--dev", args.dev), ("--prod", args.prod)):
        if not url.lower().startswith("https://"):
            raise AuditError(f"{name} must be an https:// URL")
    for path in args.scan_dir:
        if not path.is_dir():
            raise AuditError(f"--scan-dir folder wasn't found: {path}")
    if args.sources_lock and not args.sources_lock.is_file():
        raise AuditError(f"--sources-lock file wasn't found: {args.sources_lock}")
    for path in args.stage_json:
        if not path.is_file():
            raise AuditError(f"--stage-json file wasn't found: {path}")


def main(argv: Optional[List[str]] = None) -> int:
    make_output_safe()
    args = parse_args(argv)
    try:
        validate(args)
    except AuditError as exc:
        print(f"Bad arguments: {exc}", file=sys.stderr)
        return 2
    try:
        sources = load_sources_lock(args.sources_lock) if args.sources_lock else []
        os.environ.setdefault("DATAVERSE_URL", args.dev)
        token_provider, header_builder = load_auth(args.auth_dir)
        dev = ReadOnlyClient(args.dev, token_provider, "DEV", header_builder)
        prod = ReadOnlyClient(args.prod, token_provider, "PROD", header_builder)
        config = Config(
            targets=split_names(args.target), out=args.out,
            scan_dirs=args.scan_dir + [Path(item["folder"]) for item in sources],
            sources=sources,
            stage_jsons=args.stage_json,
            in_flight=split_names([args.in_flight]) if args.in_flight else None,
            max_depth=args.max_depth, skip_impact=args.skip_impact, map_limit=args.map_limit,
            allow_same_org_id=args.allow_same_org_id,
        )
        audit = Audit(config, dev, prod)
        if args.check:
            audit.preflight()
            targets = audit.find_targets()
            print(f"DEV:        {dev.base_url} ({audit.organizations['DEV']})")
            print(f"Production: {prod.base_url} ({audit.organizations['PROD']})")
            for target in targets:
                print(f"Target:     {target.get('uniquename')} {target.get('version')}")
            return 0
        return audit.run()
    except AuditError as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
