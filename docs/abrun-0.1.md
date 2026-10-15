# AB Run 0.1

AB Run extends the shell-task format used by `bsh` and `command_processor.py`.
It uses `AIBroker.review_task` for existing policy, role, provider and budget checks.
The existing entry points are unchanged.

## Release roadmap

- 0.1: recommendation plus human approval; local master; no root execution.
- 0.2: automatic execution where explicitly permitted by policy.
- 0.3: remote AB master over encrypted, authenticated TCP connections.

Completion review is separate from authorization review and is not implemented here.

## Protected deployment

Run Quinn under a separate unprivileged OS account. The master and human console
run under the human account, never root. OS peer credentials authenticate socket
callers on Linux or macOS/BSD. The master assigns the role; submission cannot supply
identity, roles or approval. The admin socket is private to the human account.

Protect master code, policy, config, credentials, executables and runtime state
from Quinn, including their parent directories. Do not run Quinn or its harness
under the human/master account. Confine its ordinary tools with OS permissions,
a container or VM; adding `abrun` alone does not sandbox Codex. The human account
is trusted, including every program running under that account. This release is
a broker prototype, not a complete sandbox installer.

## Setup and use

Use the existing AI Broker Python environment. Copy `ab_master.example.json` to
a master-owned config (mode 0600). Set the actual Quinn UID (`id -u quinn`), paths,
server-assigned role, and exact allowed argv/cwd pairs. The example admits only
`/usr/bin/pwd` with no arguments in one workspace. Do not allow shells, interpreters,
or commands whose inputs can execute arbitrary code. Optional `utc_hours: [9, 17]`
adds a hard operating-hours gate, explicitly in UTC.

Adapt existing broker policy for Quinn's username and workspace: the repository
currently names `gross` and `/Users/gross`. Both existing policy and the new exact
operation allowlist must pass. No permissive fallback can execute a denied command.
Use a master-owned submission socket directory traversable by Quinn; the runtime
folder is master-owned mode 0700. Existing socket paths are not automatically removed.

Start as the human account:

```bash
python3 ab_master.py --config /absolute/path/ab-master.json
```

From Quinn's account, in the configured workspace:

```bash
/path/to/ai_broker/abrun --reason 'Inspect working directory' -- /usr/bin/pwd
```

The response includes the operation, request ID, digest and recommendation.
Nothing executes yet. From the human's terminal:

```bash
python3 ab_review.py --socket /home/gross/.local/state/ab-master/admin.sock REQUEST_ID
```

Read the exact operation and reason, then type `APPROVE`. Anything else denies.
There is no `--yes`. Approval binds the displayed digest. Requests expire after
five minutes, are consumed before execution and disappear on restart. Policy is
rechecked immediately before execution; denials cannot be overridden. Shell actions
are not cached. Execution uses an absolute executable, fixed environment, timeout,
and `shell=False`. Audit records stay in the private runtime directory.

## Optional model review

Default recommendations are labeled deterministic policy decisions, with no model
expense. Set `reviewer_provider` and configure a dedicated `reviewer_role` permitting
LLM review in existing policy to enable it. The existing provider credentials,
budgets, routing, audit and failover apply; configure allowed fallback providers
explicitly. Review receives the command, cwd, username and caller explanation.
The explanation is untrusted. The model must return JSON containing `recommend`
(`proceed` or `deny`) and `reason`. Failure, malformed output or denial stops the
request. A positive review still requires human approval. Model text never becomes
a command. Live model review was not exercised against a paid provider.

## Known limits

A request digest binds command and cwd, not file contents. Mutable code, symlinks,
and filesystem changes require further containment; do not allow operations whose
safety depends on agent-controlled executable content. Timeout covers the direct
child, not arbitrary descendants; captured output is not bounded. The local server
is intended for trusted-host experiments, not hostile multi-tenant service. Model
review blocks submission while running. Verify the actual two-account deployment
on the target machine before extending the exact allowlist beyond harmless commands.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests cover no execution on submission, human denial, hard policy denial, role
spoofing, exact argv, digest matching, single use, expiry, recheck, invalid/negative
model reviews and Linux OS peer UID. Root service startup is refused. No root or
paid-model execution is needed for verification.
