# Clyde v0.1

A deterministic agent prototype for Cognition Sandbox, integrated with
[AI Broker](https://github.com/glendon144/ai_broker). It requests one approved
fixture, receives it, requests an unapproved identifier, receives a denial, and
exits. No LLM, GPU, API credentials, external network, or third-party Python
packages are required for the demo. Requires Python 3.10+.

```sh
python3 -m clyde.demo
python3 -m unittest discover -s tests -v
```

Expected demo result:

```text
LOCAL SIMULATION: no VM or chroot containment demonstrated
sandbox-introduction: approved (80 bytes)
unapproved-document: denied
Clyde: complete
```

The broker runs on Linux outside the NetBSD guest. Only the controller, protocol,
and proposal generator belong inside the unprivileged fishbowl chroot. The
versioned TCP endpoint accepts only fixture-fetch requests. Policy, fixture
storage, and authoritative audit logs remain on Linux.

Read [CLYDE.md](CLYDE.md) for exact commands, protocol limits, explicit failure
outcomes, process placement, and disposable NetBSD clone deployment instructions.
The local demo does not prove VM or chroot containment; those tests remain to be
performed. The private TCP link has no authentication or encryption.

Build the client-only deployment archive without overwriting an existing file:

```sh
python3 -m clyde.build_client clyde-client.tar.gz
```

## AI Broker integration and provenance

This initial source snapshot includes AI Broker at upstream commit
`47b124fca0a42891a153ea87194425ca92fd962e` plus the Clyde integration so the demo
runs offline after checkout. It is an independent repository with fresh history.
The original repository and VM are unchanged. [UPSTREAM.md](UPSTREAM.md) records
provenance and integration changes. General upstream documentation is retained in
[README-ai-broker.md](README-ai-broker.md); its other provider features are outside
the Clyde v0.1 execution path.

Validation: 133 tests passed on Linux/Python 3.12 before publication preparation.
