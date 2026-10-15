# Upstream provenance

This repository includes a source snapshot of
https://github.com/glendon144/ai_broker at commit
47b124fca0a42891a153ea87194425ca92fd962e.
Upstream source and documentation retain their attribution. No new license is
asserted for the included upstream code.

Clyde additions: `clyde/`, `clyde-policy.json`, `clyde_fixtures/`,
`fixture_provider.py`, `tests/test_clyde.py`, and `CLYDE.md`.
Existing AI Broker dispatch/cost estimation recognize the offline fixture
provider, fixture tasks are classified as file reads, and fixture requests are
excluded from permissive MCP fallback. Existing general `policy.json` is unchanged.
The upstream README is retained as `README-ai-broker.md`.

No upstream Git history or runtime state is copied into the initial repository.
