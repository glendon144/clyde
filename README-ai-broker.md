# AI Broker

AI Broker is a small AI control plane that sits between local callers and a set
of external or local providers.

## Purpose

The broker exists to ensure:

- every request is routed intentionally
- policy is enforced before a call leaves the system
- spend is tracked and limited
- repeated work can be served from cache
- no single provider becomes a silent point of failure

The design is described more philosophically in `rfc1977.txt`.
The system architecture and historical lineage are described in
`docs/architecture.md`.

## What It Does

- Keeps the original shell-command policy gate through `command_processor.py`
- Adds task-based policy enforcement in `ai_broker.py`
- Supports per-command shell argument schemas in `policy.json`
- Routes requests by task type and intent using `router.py`
- Uses provider adapters in `provider_adapters.py`
- Tracks daily spend in `state/budget_ledger.json`
- Tracks provider health in `state/provider_status.json`
- Caches reusable responses in `cache/responses.json`
- Retries transient provider failures and fails over to the next approved provider
- Resolves provider API keys from broker-managed key-file references in `policy.json`
- Supports OpenAI-compatible provider switching for `openai`, `deepseek`, and `baseten`
- Supports explicit Dream milestone capture through a broker-managed Dream REST path
- Supports scheduled cache warming via `policy.json` prefetch tasks
- Supports MCP routing, including an OpenBrain configuration driven by `policy.json`
- Adds local heuristic output assurance before successful results are cached

## Output Assurance MVP

Successful provider execution is not treated as proof that the task is true or
complete.  `epistemic_validator.py` adds an `assurance` block to broker results
using local checks only; it does not send output to a third-party verifier.

The initial checks flag missing or empty results, unsupported claims of external
action, non-zero shell return codes, missing write receipts, caller-declared
evidence requirements, and unmediated fallback execution.  A flagged result is
returned intact with:

```json
{
  "completion_status": "pending_human_approval",
  "assurance": {
    "status": "suspicious",
    "requires_human_approval": true,
    "human_summary": "...",
    "signals": []
  }
}
```

Flagged results are audited as `task_flagged_for_review` and are not written to
the successful-response cache.  A `clear` result is only provisionally clear;
the assurance block explicitly states that local heuristics do not establish
factual truth.

Callers may add an optional `expect` object to a task:

```json
{
  "expect": {
    "evidence_required": true,
    "required_result_fields": ["text", "artifact_id"],
    "required_metadata_fields": ["source_id"],
    "substitution_policy": "review"
  }
}
```

## Project Files

- `ai_broker.py` — core broker, policy, cache, budget, provider health, prefetch
- `provider_adapters.py` — provider adapters for shell, local llama-server, OpenAI-compatible backends, Anthropic, Google Vision, and MCP-style routing
- `router.py` — route selection by task type and intent
- `command_processor.py` — compatibility wrapper for shell requests
- `policy.json` — roles, provider permissions, shell command schemas, budgets, routing, MCP config, and prefetch tasks
- `run_example.py` — demo script
- `rfc1977.txt` — design and philosophy document
- `docs/architecture.md` — architectural direction, trust boundaries, and system roles

## Runtime State

These paths are runtime artifacts and should not be committed:

- `cache/responses.json`
- `state/budget_ledger.json`
- `state/provider_status.json`
- `__pycache__/`

## Configuration Notes

The OpenBrain MCP configuration in `policy.json` uses a token file path rather
than embedding a secret in the repository. Keep it that way.

If the MCP endpoint changes, prefer updating:

- `mcp_servers.openbrain.url`
- `mcp_servers.openbrain.token_file`
- `mcp_servers.openbrain.auth_header`

without writing raw secrets into repo files.

Provider retry behavior is configured in `policy.json` with:

- `provider_retry_max_attempts`
- `provider_retry_attempts`

Provider credential behavior is configured in `policy.json` with:

- `provider_credentials.openai`
- `provider_credentials.deepseek` (`DEEPSEEK_API_KEY`)
- optional `DEEPSEEK_BASE_URL` override, defaulting to `https://api.deepseek.com`
- `provider_credentials.baseten`

These entries point to local key files and the environment variables the broker
should populate at call time. Keep raw secrets out of repository files.

OpenAI-compatible provider runtime settings are configured in `policy.json` with:

- `provider_settings.openai`
- `provider_settings.deepseek` (defaults to `deepseek-v4-flash`; use `deepseek-v4-pro` when policy/payload needs it)
- `provider_settings.baseten`
- `provider_settings.local`
- `provider_settings.dream_rest`

These settings act as the provider registry for runtime selection. Each entry
can define base URL, default model, timeout, and any local launch details the
adapter needs.

Unlike FunKit's process-global provider switch, AI Broker keeps provider
selection inside per-task routing, budget, policy, and audit decisions.
That keeps provider choice economical and reviewable instead of ambient.

For the local `llama.cpp` backend, readiness is determined primarily from
`/v1/models`. Some `llama-server` builds keep `/health` at `503 Loading model`
while the model is already queryable, so the broker treats `/v1/models` as the
first readiness probe and falls back to `/health` and `/` only as secondary
signals.

Dream milestone capture is intentionally explicit. The broker does not write to
Dream on every inference. Instead, a caller must submit a `dream` task with the
`store_milestone` intent, which is routed to the local Dream REST server using
the existing `/store_capsule` convention.

## Running

Install Python dependencies from `requirements.txt`, then run:

```bash
python3 run_example.py
```

## Current State

- OpenAI-compatible providers (`openai`, `deepseek`, `baseten`) and Anthropic
  now use real SDK-backed integrations when credentials are configured.
- The `local` provider now talks to the machine's existing `llama-server`
  binary/model configuration and performs a health check before use.
- Default LLM routing now prefers the cheaper external path first
  (`deepseek`, then `openai`, then `baseten`, then `anthropic`, then `local`),
  while policy, health, and budget checks still control the final decision.
- Retry/failover is provider-aware: transient failures retry the same provider
  first, then fall through to the next approved candidate.
- `google_vision` remains an intentionally disabled placeholder adapter until
  explicit cost controls are added.
- The next integration work should preserve policy, budget, cache, and audit
  boundaries while replacing the remaining placeholders.
- Long term, `ai_broker` should own agent identity, PKI-backed admission,
  capability policy, and secret indirection for systems like Dream and OpenBrain.

## Design Note

AI Broker is positioned to become a governance layer for WebMCP-style browser
tools as those tools become more capable.

- Browser automation should be routed like any other privileged tool path:
  through role policy, cost/risk review, provider health, and audit.
- WebMCP transport should not imply permission. The broker should remain the
  place where agent identity and capability policy decide whether a browser
  action is allowed.
- This keeps future browser tools aligned with the same mediation boundary used
  for model providers, MCP servers, Dream capsules, and memory systems.

## Status Report MVP

AI Broker can now publish a non-spending JSON status report for humans,
agents, cron jobs, dashboards, and Cognition OS-style routing layers.

Generate the public-safe report:

```bash
python3 ai_broker_status.py --output state/status_report.json
```

Print it as well:

```bash
python3 ai_broker_status.py --print
```

Generate the admin report, which may include internal policy/runtime details:

```bash
python3 ai_broker_status.py --admin --output state/status_report.admin.json
```

A simple daily cron entry could run the public report at 2:00 AM:

```cron
0 2 * * * cd /home/gross/git/ai_broker && /usr/bin/python3 ai_broker_status.py --output state/status_report.json
```

The MVP report does not call cloud providers and does not spend money.  It
combines policy, provider health/cooldown state, daily budget state, estimated
costs for small sample tasks, and configured quality scores.  Later benchmark
jobs can update the quality scores with measured calibration results.

`policy.json` now supports a `provider_registry` block.  This is where explicit
negative provider policy belongs.  A blocked provider remains blocked even for a
role with wildcard provider access, and it cannot be used as a fallback when the
registry policy says so.

## Enforcement Modes

The broker runs in one of two modes, selected per run:

```bash
python3 broker_cli.py --strict     run ...   # broker decision is final
python3 broker_cli.py --permissive run ...   # denials may reach mcp_proxy
```

- **strict** — a rejected request is rejected and logged.  Nothing escapes the
  broker.  This is the default, and the mode that surfaces every case policy
  does not yet cover.
- **permissive** — a rejected request may instead be forwarded to the
  standalone `mcp_proxy` (the open connectivity path), so work continues while
  policy is still being written.  Every forward is audited.

The mode also reads from `policy.json`:

```json
"enforcement": { "mode": "strict" }
```

Precedence is: command line flag, then `policy.json`, then strict.

### The intended workflow

Run permissive first so nothing breaks, then read the gap report:

```bash
python3 broker_cli.py fallbacks
```

Every line is a request the broker refused but you still needed — that is, a
hole in policy.  Close it in `admission_rules`, re-run, and switch to strict
when the report comes back empty.

### What can never fall back

Shell tasks are excluded from fallback in code, not in configuration, and
setting `allow_task_types` to include `shell` does not change that.  A denied
shell command escaping to an unmediated executor would defeat the point of the
broker.

## Admission Rules

`policy.json` carries an ordered, first-match-wins, **deny-by-default** rule
set that runs *before* the role, agent, provider, and budget checks.  It does
not replace them: a request must satisfy the rules *and* every existing
control.

```json
"admission_rules": {
  "default": "deny",
  "rules": [
    { "name": "deny_unknown_user",
      "when": "user != 'gross'",
      "decision": "deny",
      "reason": "only the broker owner may submit tasks" },

    { "name": "allow_llm_tasks",
      "when": "task_type == 'llm'",
      "decision": "allow",
      "reason": "model inference is permitted subject to budget and routing" }
  ]
}
```

If the `admission_rules` block is absent the gate is inert and broker behaviour
is unchanged, which keeps older policy files working.

### The expression subset

`when` is a single expression in a deliberately small subset of Python,
enforced by an AST whitelist in `policy_rules.py`.  Permitted: comparisons,
`and` / `or` / `not`, `in`, list literals, subscripts, and the helper functions
below.  Rejected at load time: imports, attribute access, method calls,
lambdas, comprehensions, assignment, f-strings, arithmetic, and any function
not on the list.

Names available in an expression:

| name | meaning |
|---|---|
| `user` | requesting user (task `user` field, else the process user) |
| `role` | role the request is being evaluated as |
| `task_type` | `shell`, `llm`, `mcp`, `ocr`, `dream` |
| `intent` | task intent; for MCP this is the upstream tool name |
| `provider` / `mcp_server` | requested provider, and its MCP server suffix |
| `command` / `argv` | shell command string, and its split form |
| `cwd` | absolute working directory |
| `risk_tier` | the broker's computed risk tier |
| `mode` / `enforcement` | policy mode, and strict/permissive |
| `agent_count` | declared agent fan-out |
| `payload` / `payload_keys` | the task payload, and its sorted keys |

Helpers: `startswith`, `endswith`, `contains`, `matches` (anchored regex),
`lower`, `upper`, `len`, `any_of`, `path_under`, `get`.

A rule that fails to parse is dropped at load with its error recorded; a rule
that raises at evaluation is skipped and recorded.  Both outcomes end in deny,
never in an accidental allow.

Test a rule without executing anything:

```bash
python3 broker_cli.py --role developer rules --type shell --command "ls -la" --cwd ~/src
```

## MCP Server

`mcp_server.py` is the broker's inbound MCP face, so MCP clients such as Claude
Desktop can call tools *through* policy rather than around it.

```bash
python3 mcp_server.py --strict                  # stdio, for Claude Desktop
python3 mcp_server.py --permissive --transport sse --port 3002
```

It exposes `broker_run`, `broker_review`, and `broker_status`, plus mediated
versions of the OpenBrain memory tools (`capture_thought`, `search_thoughts`,
`list_thoughts`, `thought_stats`) that `mcp_proxy` previously served directly.

`--role` defaults to `maintainer` because that is the only role permitted `mcp`
tasks in the shipped policy.  Narrowing this to a dedicated, less privileged
role is a sensible follow-up.

## Relationship To mcp_proxy

`mcp_proxy` continues to exist as the deliberately open connectivity path.  It
injects upstream credentials and forwards whatever it is given, which is what
makes it useful as a fallback while broker policy is still being written — and
exactly why nothing should point at it once strict mode is clean.

The broker reaches it through `mcp_proxy_bridge.py`, and only in permissive
mode.


## AB Run 0.1

`abrun` submits exact operations to a separate local AB master. Policy approval
and a recommendation are followed by mandatory human approval before execution.
See [setup, trust boundary and roadmap](docs/abrun-0.1.md). This prototype requires
separate OS accounts and does not itself install a sandbox.

## Clyde v0.1 offline fishbowl prototype

Run `python3 -m clyde.demo` for a deterministic approved-fixture/denial demo
through the real AI Broker policy and provider interfaces. No model, credentials,
or third-party packages are required. See [CLYDE.md](CLYDE.md) for protocol,
tests, external Linux broker placement, limitations, and disposable NetBSD
clone/chroot deployment steps. The local simulation does not prove containment.
