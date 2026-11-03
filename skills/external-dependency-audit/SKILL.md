---
name: external-dependency-audit
description: Audit what a Power Platform release needs from SharePoint and Azure Functions, which Dataverse doesn't track. Use alongside solution-dependency-audit when flows, canvas apps, scripts or sites use SharePoint lists or call Azure Functions, or when someone asks what a function branch would add to or remove from production.
---

# External dependency audit

Dataverse doesn't track SharePoint or Azure Functions, so a solution can import cleanly and still fail when it runs. This audit finds those references, compares DEV with production, and shows what deploying each function branch would change.

The script sends only GET requests. Function keys found in source are reported by location only, and their values are never stored. If a check fails, the row is Unknown. Never turn a failed check into a clean result.

## Where the scripts are

The scripts are in `${CLAUDE_PLUGIN_ROOT}/scripts`. If that text wasn't replaced with a real path, they're in `../../scripts` relative to this file.

Run them from the customer's project folder, so results land in its `audit/` folder. Never write results, maps or downloaded source into this plugin's folder.

## Steps

### 1. Set up

Read `${CLAUDE_PLUGIN_ROOT}/docs/agent-setup.md` and follow it before anything else. If that text wasn't replaced with a real path, the file is `../../docs/agent-setup.md` relative to this file. In that file, `<scripts>` means the scripts folder above.

If the config says the release includes neither SharePoint nor Azure Functions, tell the person this audit has nothing to check, and ask whether to run it anyway.

### 2. Pair the environments

Find or write `external-map.json`, which pairs every DEV SharePoint site and function app with its production one. See `examples/external-map.example.json` in the plugin. Ask the person for anything you don't know. Never guess a production pair. Anything missing from this file is reported as Unknown.

### 3. Ask which code to analyse

If `solution-dependency-audit` ran just now in this session, reuse its `audit/sources-lock.json` and tell the person which branches it holds. Otherwise, ask on this run:

- Which Azure DevOps repositories hold the functions and plug-ins for this release?
- Which branch of each repository is being released? Never assume the default branch.
- Which function app does each function repository deploy to?

Then download them, as `solution-dependency-audit` describes:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/fetch_audit_sources.py" fetch --manifest audit-sources.json
```

### 4. Confirm what will be read

Run the audit with `--check`, show the person what it prints, and wait for them to confirm.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/external_dependency_audit.py" --map ./external-map.json --check
```

### 5. Run the audit

Pass a `--scan-dir` for every unpacked solution in the release, and for the Power Pages site if there is one.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/external_dependency_audit.py" --map ./external-map.json \
  --sources-lock audit/sources-lock.json \
  --scan-dir ./solutions/<Target1> --scan-dir ./solutions/<Target2> --scan-dir ./sources/site
```

An Azure CLI token often lacks SharePoint access. If Microsoft Graph answers 401 or 403, ask the person how they get a Graph token with `Sites.Read.All` or `Sites.Selected`, and pass that command with `--graph-token-command`.

Exit code 0 means nothing needs action. Exit code 3 means the audit ran and rows are open. Exit code 1 means it couldn't run.

### 6. Review and report

Work through `audit/external-dependencies.csv`, and explain each Unknown row in `explanation`. `False positive: <reason>` closes a reference that isn't real. `Accepted: <reason>` closes one the person confirms will ship with the release, but never a hardcoded address. Write `Accepted` only when the person has told you, and record who.

Then report:

1. Whether Gate 2 is ready, and what still blocks it.
2. What production lacks, and what's hardcoded.
3. What each function branch would add or remove.
4. Anything set at runtime, which needs a manual check.

Ask the owner to confirm anything Not in production. SharePoint and Azure Functions have no solutions, so the audit can't tell which project owns an item. `docs/playbook.md` in the plugin has the fixes, and only people approve a fix.

## Statuses

| Status | Meaning |
| --- | --- |
| In production | Production has it |
| Not in production | The release uses it, and production doesn't have it |
| New in branch | The chosen branch adds a function production doesn't have |
| Removed by branch | Production has a function the chosen branch doesn't define |
| DEV only | DEV has it, production doesn't, and no scanned source uses it |
| Unknown | A check failed or the map has no pair. A person must look at it |

A separate Hardcoded flag marks an address or key written directly in source. It always needs a fix, even when production has the item.

## Output

| File | Holds |
| --- | --- |
| `audit/external-dependencies.csv` | One row per reference, with status, Hardcoded flag, evidence and explanation |
| `audit/external-map.md` | Gate 2 status and a Mermaid diagram of sites and function apps |

These files name customer resources, so keep them in the customer's private repository.

## Limits

- It doesn't find addresses set at runtime. It lists these for a manual check.
- Function names held in constants are also listed for a manual check.
- It can't name the project that owns a list, column or function.

## First run on a new tenant

This tool has been tested against simulated APIs, not yet against every tenant. On a first run, compare a few rows with the real site and function app, and tell the person about anything that doesn't match.
