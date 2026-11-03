#!/usr/bin/env python3
"""List Azure DevOps repositories and branches, then download chosen branches read-only.

The audit skills use this after asking the person running the audit which repositories
and branches to analyse. It records the exact commit of each branch in sources-lock.json,
so an audit result can always be traced back to the code it read.

Commands
  list-repos     --organization ORG --project PROJECT
  list-branches  --url REPOSITORY_URL
  fetch          --manifest audit-sources.json [--dest sources] [--out audit]

It only reads from Azure DevOps: it never pushes, and it doesn't fetch submodules or large
files. An access token reaches Git through environment variables, never through the command
line or .git/config, and only for the Azure DevOps host.

Needs Git 2.31 or later and solution_dependency_audit.py in the same folder.

Exit codes: 0 success, 1 failure, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from solution_dependency_audit import ApiError, AuditError, ReadOnlyClient, split_command

__version__ = "1.0.0"

AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
DEFAULT_TOKEN_COMMAND = f"az account get-access-token --resource {AZURE_DEVOPS_RESOURCE} --query accessToken -o tsv"
API_VERSION = "7.1"
MARKER_FILE = "solution-audit-source"
KINDS = {"plugins", "functions", "other"}
AUTH_HEADER_PREFIX = "Authorization: Bearer "
SAFE_GIT_CONFIG = ("-c", "core.symlinks=false", "-c", "protocol.file.allow=never")
GIT_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class RepoUrl:
    clean: str
    origin: str
    name: str


def parse_repo_url(url: str) -> RepoUrl:
    """Accept only Azure DevOps HTTPS URLs, so a token can't be sent anywhere else."""
    parts = urllib.parse.urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https":
        raise AuditError("The repository URL must use https")
    if parts.password:
        raise AuditError("The repository URL contains a password or token. Remove it and use a clean URL.")
    if host != "dev.azure.com" and not host.endswith(".visualstudio.com"):
        raise AuditError(f"'{host}' isn't an Azure DevOps host (dev.azure.com or *.visualstudio.com)")
    if parts.query or parts.fragment:
        raise AuditError("The repository URL can't include a query or fragment")
    segments = [segment for segment in parts.path.split("/") if segment]
    if ".." in segments or "_git" not in segments or segments.index("_git") != len(segments) - 2:
        raise AuditError("The repository URL must end in /_git/<repository>")
    origin = f"https://{host}"
    clean = origin + "/" + "/".join(segments)
    return RepoUrl(clean, origin, urllib.parse.unquote(segments[-1]))


def validate_branch(branch: str) -> str:
    name = str(branch or "").strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    invalid = (
        not name or name.startswith(("-", "/")) or name.endswith(("/", ".lock", "."))
        or ".." in name or "@{" in name or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name)
    )
    if invalid:
        raise AuditError(f"'{branch}' isn't a valid branch name")
    return name


def safe_folder_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not cleaned:
        raise AuditError(f"'{name}' can't be used as a folder name")
    return cleaned


# Git ----------------------------------------------------------------------------------
def git_environment(origin: str, token: Optional[str]) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"})
    if token:
        # Add to any existing entries, such as a corporate proxy setting, rather than replace them.
        try:
            index = max(int(env.get("GIT_CONFIG_COUNT", "0")), 0)
        except ValueError:
            index = 0
        # Scoping the header to the host means a redirect can't carry the token elsewhere.
        env.update({
            "GIT_CONFIG_COUNT": str(index + 1),
            f"GIT_CONFIG_KEY_{index}": f"http.{origin}/.extraHeader",
            f"GIT_CONFIG_VALUE_{index}": f"{AUTH_HEADER_PREFIX}{token}",
        })
    return env


def run_git(args: Sequence[str], env: Dict[str, str], extra_config: Sequence[str] = ()) -> str:
    command = ["git", *SAFE_GIT_CONFIG, *extra_config, *args]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise AuditError("Git wasn't found. Install Git 2.31 or later.") from None
    except subprocess.TimeoutExpired:
        raise AuditError(f"Git timed out after {GIT_TIMEOUT_SECONDS} seconds") from None
    if result.returncode != 0:
        raise AuditError(f"Git failed: {_clean_error(result.stderr, env)}")
    return result.stdout


def _clean_error(message: str, env: Dict[str, str]) -> str:
    for key, value in env.items():
        if key.startswith("GIT_CONFIG_VALUE_") and value.startswith(AUTH_HEADER_PREFIX):
            message = message.replace(value[len(AUTH_HEADER_PREFIX):], "[removed]")
    return re.sub(r"https://[^@\s/]+@", "https://", message).strip()[:400]


def list_branches(repo: RepoUrl, env: Dict[str, str], extra_config: Sequence[str] = ()) -> List[str]:
    output = run_git(["ls-remote", "--heads", "--", repo.clean], env, extra_config)
    return sorted(
        line.split("refs/heads/", 1)[1].strip()
        for line in output.splitlines() if "refs/heads/" in line
    )


def _read_marker(folder: Path) -> Optional[str]:
    try:
        return (folder / ".git" / MARKER_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def download_branch(
    clean_url: str, branch: str, folder: Path, env: Dict[str, str], extra_config: Sequence[str] = (),
) -> str:
    """Download one branch at depth 1, and return its commit."""
    if folder.exists():
        marker = _read_marker(folder)
        if marker is None:
            raise AuditError(f"{folder} already exists and wasn't created by this tool. Choose another --dest.")
        if marker != clean_url:
            raise AuditError(f"{folder} holds another repository. Choose another name or --dest.")
        run_git(["-C", str(folder), "fetch", "--depth", "1", "--no-tags", "--", clean_url, f"refs/heads/{branch}"], env, extra_config)
        run_git(["-C", str(folder), "checkout", "--force", "--detach", "FETCH_HEAD"], env, extra_config)
        run_git(["-C", str(folder), "clean", "-ffdx"], env, extra_config)
    else:
        folder.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            ["clone", "--depth", "1", "--single-branch", "--no-tags", "--branch", branch, "--", clean_url, str(folder)],
            env, extra_config,
        )
        (folder / ".git" / MARKER_FILE).write_text(clean_url, encoding="utf-8")
    return run_git(["-C", str(folder), "rev-parse", "HEAD"], env, extra_config).strip()


# Commands -----------------------------------------------------------------------------
def token_from_command(command: str) -> str:
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
        raise AuditError(f"The token command '{parts[0]}' failed: {result.stderr.strip()[:200]}. Run az login, or use --auth git.")
    return token


def get_token(args: argparse.Namespace) -> Optional[str]:
    return None if args.auth == "git" else token_from_command(args.token_command)


def list_repositories(client: ReadOnlyClient, organization: str, project: str) -> List[dict]:
    if re.search(r"[/?#%]", organization + project):
        raise AuditError("Give the organization and project as plain names, without / ? # or %")
    path = f"{organization}/{project}/_apis/git/repositories?api-version={API_VERSION}"
    try:
        rows = client.get_all(path)
    except ApiError as exc:
        raise AuditError(f"Could not list the repositories: {exc}") from None
    repositories = []
    for row in rows:
        if row.get("isDisabled"):
            continue
        repositories.append({
            "name": row.get("name"), "url": row.get("remoteUrl"),
            "default_branch": str(row.get("defaultBranch") or "").replace("refs/heads/", ""),
        })
    return sorted(repositories, key=lambda item: str(item["name"]).lower())


def load_manifest(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AuditError(f"Could not read the manifest: {exc}") from None
    entries = data.get("repositories") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise AuditError("The manifest needs a non-empty 'repositories' list")
    prepared, names = [], set()
    for entry in entries:
        repo = parse_repo_url(entry.get("url", ""))
        branch = validate_branch(entry.get("branch", ""))
        kind = str(entry.get("kind", "other")).lower()
        if kind not in KINDS:
            raise AuditError(f"kind must be one of {sorted(KINDS)}, not '{kind}'")
        name = safe_folder_name(str(entry.get("name") or repo.name))
        if name.lower() in names:
            raise AuditError(f"Two repositories use the folder name '{name}'. Give one a different 'name'.")
        names.add(name.lower())
        prepared.append({
            "name": name, "repo": repo, "branch": branch, "kind": kind,
            "function_app": str(entry.get("function_app") or ""),
        })
    return prepared


def fetch_all(
    entries: List[dict], dest: Path, out: Path, token: Optional[str],
    log: Callable[[str], None] = print, extra_config: Sequence[str] = (),
) -> dict:
    repositories, warnings = [], []
    for entry in entries:
        repo: RepoUrl = entry["repo"]
        folder = dest / entry["name"]
        log(f"Downloading {entry['name']} ({entry['branch']})")
        commit = download_branch(repo.clean, entry["branch"], folder, git_environment(repo.origin, token), extra_config)
        submodules = (folder / ".gitmodules").is_file()
        if submodules:
            warnings.append(f"{entry['name']} uses submodules, which weren't downloaded or scanned.")
        repositories.append({
            "name": entry["name"], "url": repo.clean, "branch": entry["branch"], "commit": commit,
            "folder": folder.as_posix(), "kind": entry["kind"], "function_app": entry["function_app"],
            "submodules_skipped": submodules,
        })
    lock = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tool_version": __version__, "repositories": repositories, "warnings": warnings,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "sources-lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    for warning in warnings:
        log(f"Warning: {warning}")
    log(f"Recorded {len(repositories)} commits in {out / 'sources-lock.json'}")
    return lock


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List and download Azure DevOps branches for an audit.")
    parser.add_argument("--auth", choices=("az", "git"), default="az", help="az uses a Microsoft Entra token; git uses your Git credentials")
    parser.add_argument("--token-command", default=DEFAULT_TOKEN_COMMAND, help="Command that prints an Azure DevOps token")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    repos = commands.add_parser("list-repos", help="List the repositories in a project")
    repos.add_argument("--organization", required=True)
    repos.add_argument("--project", required=True)
    branches = commands.add_parser("list-branches", help="List the branches in a repository")
    branches.add_argument("--url", required=True)
    fetch = commands.add_parser("fetch", help="Download the branches named in a manifest")
    fetch.add_argument("--manifest", type=Path, required=True)
    fetch.add_argument("--dest", type=Path, default=Path("sources"))
    fetch.add_argument("--out", type=Path, default=Path("audit"))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "list-repos":
            if args.auth == "git":
                raise AuditError("Listing repositories needs a token. Use --auth az, or give the repository URLs directly.")
            client = ReadOnlyClient("https://dev.azure.com", lambda _: token_from_command(args.token_command), "Azure DevOps", api_root="/")
            print(json.dumps(list_repositories(client, args.organization, args.project), indent=2))
        elif args.command == "list-branches":
            repo = parse_repo_url(args.url)
            print(json.dumps(list_branches(repo, git_environment(repo.origin, get_token(args))), indent=2))
        else:
            entries = load_manifest(args.manifest)
            fetch_all(entries, args.dest, args.out, get_token(args))
        return 0
    except AuditError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
