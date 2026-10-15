# AI Broker Architecture

## Origin

`AI Broker` is conceived in the same architectural tradition as Symark
`PowerBroker`, later carried forward under `BeyondTrust`.

The core idea is the same:

- do not hand out raw privilege
- mediate access through policy
- log every important action
- keep control at the boundary

In the Unix world, `PowerBroker` mediated privileged commands and identities.
In this project, `AI Broker` should mediate model providers, MCP tools, agent
identities, memory systems, and the secrets needed to reach them.

## Problem It Solves

Modern agent systems have several failure modes that look like privileged access
problems:

- provider credentials are too easy to scatter across configs and scripts
- external models can spend money or exfiltrate data without a strong local gate
- multiple agents may share memory systems without a clear admission policy
- tool and memory access often bypasses a central audit trail

`AI Broker` exists to make those actions explicit, bounded, and reviewable.

## Architectural Role

`AI Broker` should be the policy and trust boundary for agentic systems.

Its responsibilities should include:

- provider routing
- budget enforcement
- secret indirection
- audit logging
- agent identity
- capability policy
- admission and revocation

This means requests should not go directly from an agent to a provider or memory
system when the broker can mediate the decision.

## Current Provider Shape

The current implementation uses a broker-managed provider registry in
`policy.json` rather than a process-global switch.

- provider credentials are referenced indirectly from local key files
- OpenAI-compatible providers share one adapter path with per-provider runtime settings
- local `llama.cpp` is treated as a brokered provider with health checks, startup control, and fallback
- Dream milestone writes are explicit brokered tasks rather than automatic side effects of inference
- readiness for the local server is based on queryable API state, not just a generic health endpoint

This keeps provider choice inside policy, budget, audit, and retry/failover
decisions instead of ambient process state.

## WebMCP Governance Direction

If browser tools converge on WebMCP-style transports, the broker should govern
them the same way it governs model and memory paths now.

- transport reachability should not imply browser permission
- browser actions should remain subject to role policy, risk tiering, audit, and approval
- expensive or high-risk browser workflows should fit into the same budget and routing posture as other brokered capabilities

## Relationship To Dream And OpenBrain

These systems are complementary, but they should not collapse into one service.

`AI Broker`
- decides which agent may access which provider, tool, or memory
- holds agent identity and trust policy
- maps local identities to capabilities and secret references
- records audit events for access decisions

`Dream`
- stores structured capsules, lineage, provenance, and recovery artifacts
- handles exchange and federation of capsules between trusted agents or systems
- preserves trust metadata on imported and exported knowledge

`OpenBrain`
- provides semantic memory retrieval
- acts as a practical working memory and recall layer
- should be accessed through broker policy when used by multiple agents

The clean split is:

- `AI Broker` governs access
- `Dream` governs structured memory exchange
- `OpenBrain` governs semantic recall

## Trust Model

The broker should distinguish three different concerns:

- transport access
- agent identity
- capability policy

Those should not be conflated.

### Transport Access

Transport access answers: can a client reach the service at all?

Examples:

- a tokenized MCP endpoint
- an SSH key
- a local Unix socket

This is necessary, but insufficient.

### Agent Identity

Identity answers: who is making the request?

The right long-term direction is a simple local PKI:

- each installation gets an agent identifier
- each installation gets a keypair
- the broker stores public keys and agent metadata
- signed requests or manifests prove identity

### Capability Policy

Policy answers: what may this identified agent do?

Examples:

- read from OpenBrain
- write shared Dream capsules
- import federated bundles
- use high-cost external providers
- access redacted local secrets

This separation lets existing systems keep working during migration. A shared
token may continue to allow transport while signatures and policy determine
whether a sensitive action is accepted.

## Why PKI Belongs Here

`Dream` should understand provenance and signatures on capsules.
`OpenBrain` should remain focused on memory storage and retrieval.

But the decision about whether `Codex`, `Claude Desktop`, or another agent may
obtain a secret, write shared memory, or call a provider is fundamentally a
broker decision.

That is why PKI belongs in `AI Broker`.

## Suggested Capability Domains

The broker should eventually manage policy across these domains:

- `providers`
- `mcp_servers`
- `memory.read`
- `memory.write`
- `dream.import`
- `dream.export`
- `tools.exec`
- `secrets.resolve`
- `budget.override`
- `admin.admit_agent`

This should be explicit and machine-readable rather than implied by environment
or host machine.

## Migration Path

The project can evolve without breaking existing local systems.

### Phase 1

- keep current token or token-file based transport working
- centralize token references in broker-managed config
- avoid writing raw secrets into repository files or shared memory

### Phase 2

- introduce an agent registry
- issue local keypairs to trusted installations
- require signatures for sensitive shared-memory and import/export operations

### Phase 3

- move provider and memory access behind broker mediation by default
- add admission, revocation, and trust tier review
- reduce broad shared-token privileges

### Phase 4

- federate across multiple trusted machines and agent runtimes
- let Dream capsules and OpenBrain memory cooperate under broker policy

## Enforcement Posture And The Open Path

The broker runs strict or permissive.  Strict means the broker's decision is
final.  Permissive means a denied request may still be served by `mcp_proxy`,
the unmediated transport, with the denial recorded.

This is a migration device, not a permanent design.  Permissive mode exists so
that policy can be written against real traffic instead of guesses: run
permissive, read `broker_cli.py fallbacks`, close each gap in `admission_rules`,
and move to strict when the report is empty.  A system that stays permissive
has a broker in name only.

Two boundaries hold in both modes:

- shell tasks may never fall back, and that guard lives in code rather than in
  `policy.json`
- every fallback and every refusal is audited, so the unmediated path is never
  silent

## Admission Rules

Capability policy is expressed as an ordered, deny-by-default rule set in
`policy.json`, evaluated before the role, provider, budget, and agent checks.
Rules are single expressions in a restricted subset of Python enforced by an
AST whitelist.

Two properties matter architecturally:

- the rule gate is additive.  It can only refuse what the older checks would
  have allowed; it can never grant what they would have denied
- a malformed or failing rule denies.  There is no path where a broken policy
  file becomes an open one

This is the machine-readable capability layer the trust model calls for.  The
`Suggested Capability Domains` above are the natural next vocabulary for the
`when` expressions, once agent identity exists to name the subject.

## Design Rule

The original RFC principle still holds:

> No intelligence request shall leave the system unmediated.

The updated version is broader:

> No privileged model, tool, or memory action should occur without brokered
> identity, policy, and audit.
