# Proposed folder structure — Content-to-Campaign workspace

**Audience:** Marketing team (owner of the SharePoint/OneDrive workspace)
**Why:** the agents need a small set of known locations, and the Campaign-in-a-Box
Orchestrator (Agent 2) creates campaign workspaces from a versioned folder/naming
template per the Phase 1 spec. Existing content does **not** need reorganizing —
the repository is read in place; the structure below applies to the new
Content-to-Campaign root only.

```
/Content-to-Campaign/                    ← the one root IT grants agent access to
├── 00-Intake/
│   ├── campaign-requests.xlsx           ← Microsoft Forms responses workbook
│   ├── quarterly-plan.xlsx              ← current quarterly marketing plan (or copy)
│   └── ad-hoc/                          ← drop-a-file campaign requests (watched)
├── 01-Campaigns/                        ← Agent 2 creates one folder per campaign
│   └── <YYYY>-<QN>-<topic-slug>/        ←   e.g. 2026-Q4-erp-modernization
│       ├── brief/                       ← Agent 1 writes the approved brief here
│       ├── drafts/                      ← Agents 3–4: versioned drafts, tracked changes
│       └── final/                       ← Agent 5 locks approved versions (read-only)
├── 02-Reference/
│   ├── brand-guidelines/                ← read by agents 2, 3, 5 (tone/terminology)
│   └── intel-library/                   ← curated market/competitor files (Agent 2)
└── 03-Repository/                       ← existing reusable content — moved or linked;
                                            agents READ only, never modify
```

## Naming conventions (Agent 5 enforces these at packaging)

- Campaign folders: `YYYY-QN-topic-slug` (lowercase slug, hyphens)
- Files: `<campaign-slug>-<asset-type>-vN` (e.g. `erp-modernization-blog-v2`)
- Final versions live only in `final/`, locked read-only after BU sign-off

## Ground rules the agents follow (enforced in code)

- Never delete, move, or overwrite existing files — writes create new files only
- `03-Repository/` and `02-Reference/` are read-only to every agent
- Every file operation is recorded in the audit telemetry
- Access starts only after IT's `Sites.Selected` grant on this root's site

## What we need back from Marketing

1. Link to the current folder root (URL only — tells us SharePoint site vs
   personal OneDrive, which decides the permission path)
2. Site/folder owner (who approves creating `/Content-to-Campaign/`)
3. The intake Form link, if one exists (its responses workbook must live in
   `00-Intake/`)
