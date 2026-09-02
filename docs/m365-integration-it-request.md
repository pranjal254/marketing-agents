# IT Request — Microsoft 365 access for the Campaign Identification agent

**From:** AiCoE (Content-to-Campaign Phase 1)
**Purpose:** the Campaign Identification agent (approved LevelShift build, Python,
onboarding to ShiftAI Execution Studio) needs programmatic access to Microsoft 365
via **Microsoft Graph** to read campaign requests and write campaign briefs.
**Date:** 2026-09-02

---

## 1. What the agent does with M365 (exact scope)

| Operation | Resource | Access |
|---|---|---|
| Read intake form responses | The Excel workbook that Microsoft Forms syncs responses to ("Open in Excel" workbook) | Read rows of one table |
| Read the quarterly marketing plan | Quarterly plan Excel workbook | Read rows (never writes) |
| Watch for ad-hoc requests | One designated intake folder | List/read files |
| Write campaign briefs | Campaign workspace folder | Upload **new** .docx files only — conflict behavior "fail": the agent never overwrites, moves, or deletes anything |

Later phases (agents 2–5) will add: content-repository search (read), intel library
(read), status tracker workbook (append), final-version locking. Worth provisioning
the same site once, but only the four rows above are needed now.

## 2. Authentication model

- **App-only (daemon) access**: Microsoft Entra ID app registration, client-credential
  flow (MSAL). No user sign-in, no delegated permissions.
- Secrets live in environment configuration only (dev: local `.env`, git-ignored;
  production: ShiftAI Execution Studio secret store). Never in code, prompts, logs,
  or telemetry — enforced and tested in the codebase.

## 3. What we need from IT

1. **Entra ID app registration** (suggested name: `shiftai-c2c-campaign-identification`)
   - Directory (tenant) ID
   - Application (client) ID
   - Client secret (a certificate is fine too — say so and we'll add cert support);
     please state the expiry/rotation policy so we can calendar it
2. **Graph API permissions (application type, admin-consented) — least privilege preferred:**
   - **Preferred:** `Sites.Selected` + a per-site grant (read/write) on the one
     SharePoint site / Teams site that hosts the Content-to-Campaign workspace.
     This gives the app access to *nothing else* in the tenant.
   - **Only if the workspace truly lives in a personal OneDrive** (not a SharePoint
     library): `Files.ReadWrite.All` is required — note this is tenant-wide, so we'd
     rather the workspace move to a SharePoint site and stay with `Sites.Selected`.
3. **Resource locations** (URLs are enough — we resolve drive/item IDs via Graph):
   - Intake form's responses workbook (and the table/sheet name)
   - Quarterly marketing plan workbook (and the table/sheet name)
   - Ad-hoc intake folder
   - Campaign workspace root folder
4. **A non-production copy for testing** (strongly preferred): a test site/library
   with a dummy form + dummy plan workbook, so all integration testing runs against
   test data before the app is granted to the real site.

## 4. Security posture (for IT's review)

- Data touched: internal marketing plans and campaign strategy — internal-confidential;
  **no customer PII** in Phase 1 (per the approved agent spec).
- Writes are additive only (new files); reads are logged; every Graph call is retried
  with backoff, timed out, and recorded in the agent's STS v2 audit telemetry.
- No Salesforce/Pardot access exists anywhere in the codebase (Phase 1 guardrail).
- Kill switch pauses all agent actions instantly; approvals remain human-only.
- Note: Microsoft Forms has no stable Graph API for responses — the agent reads the
  form's synced Excel workbook instead, which is why that workbook must live in the
  granted site.

## 5. What we do once we receive this

Set (never commit) the environment variables `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`,
`GRAPH_CLIENT_SECRET` plus the resource IDs; run the live smoke test (read plan rows,
read form responses, upload one test brief to the test workspace); then wire the
event triggers. The connector code (auth, retry, Excel/OneDrive/Word operations) is
already built and unit-tested against mocks.
