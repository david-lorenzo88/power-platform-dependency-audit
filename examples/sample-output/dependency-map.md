# Dependency map: Sales, Core

Generated from simulated environments by solution_dependency_audit 1.0.0.
DEV: https://contoso-dev.crm.dynamics.com. Production: https://contoso.crm.dynamics.com.

## Status

| Status | Rows |
| --- | --- |
| Unknown | 0 |
| In-flight | 6 |
| Version too old | 0 |
| False positive | 0 |
| Accepted | 0 |
| In production | 2 |
| In target | 1 |

## Gate 2

- Components found by only some sources, with no explanation: 1.

## Release order

Import in this order. Each solution needs only those above it.

1. Core
2. Sales (components needed from Core: 1)

Only Dataverse dependencies decide this order. References in code don't, because they don't stop an import.

## Coverage

- Text scan: files read 3, files not read 0, names searched 24, components that couldn't be named 0
- Dry run: Sales Failed, Core Failed

## Components in more than one target solution

This doesn't block an import, but the solution imported last decides what these look like. Keep each in one solution.

| Component | Type | Target solutions |
| --- | --- | --- |
| Core form | Form | Core, Sales |

## Fix these first

Components ranked by how many rows a fix would clear.

| Rows | Required component | Type | Status | Owning solutions | Needed by | Used by |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | account.b_col | Column | In-flight | ProjB | Core, Sales |  |
| 2 | b_tbl | Table | In-flight | ProjB | Sales |  |
| 1 | b_choice | Global choice | In-flight | ProjB | Sales |  |
| 1 | b_tbl.b_hidden | Column | In-flight | ProjB (via table) |  |  |

## Solutions

```mermaid
flowchart LR
  subgraph targets["In this release"]
    t1["Sales"]
    t2["Core"]
    repo["Repository code"]
  end
  subgraph others["Not in this release"]
    o1["ProjB"]:::inflight
  end
  t2 -->|"1"| o1
  repo -->|"1"| o1
  t1 -->|"1"| t2
  t1 -->|"3"| o1
  classDef inflight fill:#fde2e1,stroke:#b42318,color:#111
  classDef unknown fill:#e8e8e8,stroke:#555,color:#111
```

Each arrow points to what a solution needs, and its number counts the components. Red solutions block the release. Grey solutions are in a circular dependency.

## Structure

```mermaid
flowchart LR
  subgraph s1["Core"]
    n2["Core form"]
  end
  subgraph s2["ProjB"]
    n1["account.b_col"]:::inflight
    n4["b_tbl"]:::inflight
    n5["b_tbl.b_hidden"]:::inflight
    n7["b_choice"]:::inflight
  end
  subgraph s3["Repository code"]
    n6["Source files"]
  end
  subgraph s4["Sales"]
    n3["Sales form"]
  end
  n2 --> n1
  n3 --> n1
  n3 --> n4
  n6 -.-> n5
  n4 --> n7
  n7 --> n4
  classDef inflight fill:#fde2e1,stroke:#b42318,color:#111
  classDef version fill:#fff1cc,stroke:#b54708,color:#111
  classDef unknown fill:#e8e8e8,stroke:#555,color:#111
```

Red is In-flight, amber is Version too old and grey is Unknown. Dashed arrows come from the text scan.

## Warnings

- Components in more than one target solution: 1. The solution imported last decides what they look like.
