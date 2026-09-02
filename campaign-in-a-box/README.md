# Agent 2 — Campaign-in-a-Box Orchestrator

From an approved campaign brief (Agent 1's output) to the confirmed campaign
foundation and, once every asset is content-confirmed, the hashed Campaign-in-a-Box
package manifest. Planning pass: Claude Opus 5 (Azure GPT in dev). Packaging module:
deterministic, no LLM.

## Run locally (CLI)

```powershell
cd Agents\campaign-in-a-box
..\.venv\Scripts\python -m c2c_campaign_box.cli plan --campaign CMP_ID
..\.venv\Scripts\python -m c2c_campaign_box.cli confirm --campaign CMP_ID --kind pack --actor-id lead@x
..\.venv\Scripts\python -m c2c_campaign_box.cli confirm --campaign CMP_ID --kind plan --actor-id lead@x
..\.venv\Scripts\python -m c2c_campaign_box.cli confirm-asset --campaign CMP_ID --asset flagship_blog --file draft.docx --actor-id reviewer@x
..\.venv\Scripts\python -m c2c_campaign_box.cli package --campaign CMP_ID
```

The easier way to drive it is the dev bridge + Marketing Studio: sidebar →
**Campaign box (live)** (see `Agents/bridge/README.md`).

## Tests / gates

```powershell
cd Agents\campaign-in-a-box
..\.venv\Scripts\python -m pytest tests -q     # 52 tests, all external calls mocked
..\.venv\Scripts\python -m mypy                # strict
..\.venv\Scripts\python -m ruff check src tests
```

## Key files

- `config/campaign_in_a_box.json` — versioned Business Capability config
  (composition v0.1.0-draft, pending Marketing Lead sign-off)
- `prompts/campaign-in-a-box.system.v1.0.0.md` — spec system prompt, verbatim
- `agent-spec.md` — build notes, conflict resolutions, open items
- `CHECKLIST-campaign-in-a-box.md` — acceptance checklist
