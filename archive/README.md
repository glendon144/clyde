# archive/

Frozen source kept for reference. Nothing here is imported, tested, or run by
the broker. Do not edit these files — if something here is worth having, port
it forward into the live tree rather than reviving the directory.

## prototype-2026-04-09/

The original AI Broker prototype, recovered from an un-versioned directory at
`~/src/ai_broker` on m2 and committed here so it stops being the only copy.

It predates this repository: the first commit (`61e46d6`, 2026-04-23) was a
fresh start, not a continuation, so none of this is in the repo's history. Its
`policy.json` still points at a long-dead `PiKit` path, and its `ai_broker.py`
is 2.5 KB against the current 48 KB. As a whole it is superseded.

### Why it was kept

`json_tool_executor.py` is the piece worth remembering. It is a standalone,
deterministic executor for tool-call JSON emitted by a *local* model, and its
design note still reads well:

> the model suggests, Python decides

The part with no equivalent in the current broker is
`extract_first_json_object()` — a balanced-brace scanner that pulls the first
complete JSON object out of surrounding model chatter, correctly tracking
string state and backslash escapes so that braces inside string literals do not
throw off the depth count. It turns

    I'll use bash now. {"name": "bash", "arguments": {...}} Hope that helps!

into a parsed object without regexes and without trusting the model to emit
clean JSON.

Today's broker never needs this, because it is called with structured tasks
from Python and from MCP clients that already speak JSON-RPC. It would become
relevant again the moment a local `llama.cpp` model is asked to *propose*
brokered actions in free text — which is exactly the direction
`docs/architecture.md` points.

The rest is context: `command_processor.py` here is the ancestor of the live
one, and the shell guardrails (whitelist, forbidden-token rejection, no
`shell=True`, no `python3 -c`) are recognisably where the current
`READ_ONLY_DENY_COMMANDS` and `SAFE_SHELL_METACHARS` came from.

### Contents

| File | Note |
|---|---|
| `json_tool_executor.py` | The reason this directory exists. Standalone; still runs. |
| `ai_broker.py` | 2.5 KB prototype broker: load policy, check role, execute. |
| `command_processor.py` | Ancestor of the live `command_processor.py`. |
| `policy.json`, `policy-old.json` | Role/path policy pointing at a dead PiKit tree. |
| `good.json`, `bad_policy.json`, `bad_syntax.json` | Parser fixtures. |
| `ai_broker.spec` | PyInstaller spec. The 23 MB `build/` and `dist/` output was not kept. |
| `README.md` | The prototype's own README, as found. |
