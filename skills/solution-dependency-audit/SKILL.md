---
name: solution-dependency-audit
description: Audit what a Power Platform release needs that production doesn't have. Use when a release of one or more Dataverse solutions may depend on solutions still in development, before a production import, or when someone asks for a dependency audit, a dependency map or a release order.
---

# Solution dependency audit

This audit answers one question: what do the target solutions need that production doesn't already have? It covers Dataverse components, plug-ins, the unpacked solutions, a downloaded Power Pages site and the Azure DevOps branches the person chooses.

The audit script sends only GET requests, so it can't change an environment. If a check fails, the row is Unknown. Never turn a failed check into a clean result.

## Where the scripts are

The scripts are in `${CLAUDE_PLUGIN_ROOT}/scripts`. If that text wasn't replaced with a real path, they're in `../../scripts` relative to this file.

Run them from the customer's project folder, so results land in its `audit/` folder. Never write results, maps or downloaded source into this plugin's folder.

## Steps

### 1. Set up

Read `${CLAUDE_PLUGIN_ROOT}/docs/agent-setup.md` and follow it before anything else. If that text wasn't replaced with a real path, the file is `../../docs/agent-setup.md` relative to this file. In that file, `<scripts>` means the scripts folder above.

It checks the tools and sign-ins, confirms the customer's tenant, and gives you the solutions, environments, scope and Azure DevOps projects in `audit-config.json`.

### 2. Ask which code to analyse

Ask this on every run, even if you already know an answer from earlier, and even if the config says the release has no custom code:

- Which Azure DevOps repositories hold plug-ins and functions for this release?
- Which branch of each repository is being released? Never assume the default branch.

If `audit-sources.json` exists from an earlier run, show it as a starting point, and ask whether each repository and branch still applies. Never reuse it silently.

To help them choose, run this with the organization and project from the config:

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/fetch_audit_sources.py" list-repos --organization <org> --project <project>
python "${CLAUDE_PLUGIN_ROOT}/scripts/fetch_audit_sources.py" list-branches --url <repository-url>
```

Write their answers to `audit-sources.json` (see `examples/audit-sources.example.json` in the plugin), then download the branches. This records each commit in `audit/sources-lock.json`.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/fetch_audit_sources.py" fetch --manifest audit-sources.json
```

If they use another Git host, ask them to clone the branches and pass each folder with `--scan-dir`. If they say there's no code to analyse, record that in your report.

### 3. Prepare the other sources

For each target solution, export and unpack it into a folder with its exact unique name. That name ties the folder's findings to the target, and Gate 2 stays blocked for any target without one.

```
pac solution export --name <Target> --path ./build/<Target>.zip --managed false --overwrite --environment <DEV>
pac solution unpack --zipfile ./build/<Target>.zip --folder ./solutions/<Target> --packagetype Unmanaged --allowWrite --allowDelete
```

If the config includes a Power Pages site, download it to `./sources/site`, using its site ID:

```
pac pages download --path ./sources/site --webSiteId <site-id> --modelVersion 2 --environment <DEV>
```

For a code site, use `pac pages download-code-site --path ./sources/site --webSiteId <site-id> --environment <DEV>`.

### 4. Confirm the environments

Run the audit with `--check`, show the person what it prints, and wait for them to confirm it before going on.

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/solution_dependency_audit.py" --target <Target1> --target <Target2> --dev <DEV> --prod <PROD> --check
```

### 5. Dry-run each target on a sandbox

This is the only step that isn't read-only. Use the sandbox from the config, which must be a copy of production. Never use shared DEV or production. Confirm the sandbox with the person, then for each target:

```
pac solution export --name <Target> --path ./build/<Target>_managed.zip --managed true --overwrite --environment <DEV>
python "${CLAUDE_PLUGIN_ROOT}/scripts/stage_solution.py" --zip ./build/<Target>_managed.zip --sandbox <SANDBOX> --dev <DEV> --prod <PROD>
```

Each one writes `audit/stage-<Target>.json`. A failed dry run still writes its file, and the audit needs it. A target that needs another target will report those components as missing, and the audit expects this. If the dry run also reports an error about them, import the earlier target into the sandbox and try again.

If the script says the sandbox and production point to the same organization, stop and ask the person to check the admin center. Only pass `--allow-same-org-id` once they confirm they're separate environments.

If the config has no sandbox and the person can't provide one, carry on without this step and say that Gate 2 can't pass.

### 6. Run the audit

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/solution_dependency_audit.py" \
  --target <Target1> --target <Target2> --dev <DEV> --prod <PROD> \
  --scan-dir ./solutions/<Target1> --scan-dir ./solutions/<Target2> --scan-dir ./sources/site \
  --sources-lock audit/sources-lock.json \
  --stage-json audit/stage-<Target1>.json --stage-json audit/stage-<Target2>.json
```

Exit code 0 means no open rows and Gate 2 is ready. Exit code 3 means the audit ran and something still needs action. Exit code 1 means it couldn't run.

### 7. Review the results

Work through `audit/dependencies.csv`:

- Open each text-scan hit at the file and line in `evidence`. If it isn't a real use, write `False positive: <reason>` in `explanation`.
- Explain every row with a `reconcile_note`, and every Unknown row, in `explanation`.
- Never edit a row's `status`. Run the audit again after changes. Explanations carry over by row `id`.

Any explanation clears a row for Gate 2, but only some close it:

- `False positive: <reason>` closes a file row or a shared-component row.
- `Accepted: <reason>` also closes a plug-in type row, or an Unknown row. Write it only when the person has told you they checked or accept it, and record who.
- In-flight and Version too old dependencies from the graph or a dry run, and circular dependencies, close only when they're fixed.

### 8. Report

Report these, in this order:

1. Whether Gate 2 is ready, and what still blocks it.
2. The release order, any circular dependency, and any solution waiting for one.
3. The components that clear the most rows, from "Fix these first" in `audit/dependency-map.md`.
4. Which repositories and commits were read, from `audit/sources-lock.json`.

Link to `audit/dependency-map.md` rather than repeating it. Propose fixes only if asked. `docs/playbook.md` in the plugin has the decision questions and fixes, and only people approve a fix.

## Statuses

| Status | Meaning |
| --- | --- |
| In target | One of the target solutions holds it. If another target holds it, it sets the release order |
| In production | Production already has it, at a high enough version |
| In-flight | It's only in development, so it blocks the release |
| Version too old | Production has an older version of the managed solution it comes from |
| Unknown | A check failed or sources disagree. A person must look at it |

A failed check can also appear as a blocker under Gate 2 in the map. Report these exactly as written.

## Output

| File | Holds |
| --- | --- |
| `audit/dependencies.csv` | One row per dependency, with `target_solutions`, status, owners, evidence and explanation |
| `audit/impact.csv` | Who else uses each open component |
| `audit/dependency-map.md` | Gate 2 status, release order, what to fix first and two Mermaid diagrams |
| `audit/run-log.json` | Environments, targets, versions, coverage and blockers |

These files name customer components, so keep them in the customer's private repository.

## Limits

- It doesn't find names built at runtime. End-to-end tests have to catch those.
- Only Dataverse dependencies set the release order. References in code don't.
- A dependency that passes through an in-flight component doesn't set the order until that component is fixed.
- Component types above 10000 vary by environment. If one can't be named, its row is Unknown.

## First run on a new tenant

This tool has been tested against simulated APIs, not yet against every tenant. On a first run, compare a few rows with the maker portal's "Show dependencies" view, and tell the person about anything that doesn't match.
