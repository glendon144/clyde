# Clyde v0.1

Deterministic, offline fixture-fetch prototype integrated with AI Broker
(base commit `47b124fca0a42891a153ea87194425ca92fd962e`). No model, GPU,
credentials, third-party Python packages, or Internet connection is needed to run.
Python 3.10+ is required on Linux and **inside** the NetBSD chroot. Tested here
with Python 3.12 on Linux; interpreter availability in the fishbowl is unverified.
The Cognition OS checkout documents Python 3.10 on the guest, which does not
establish availability inside its chroot.

## Run locally

From this repository root:

```sh
python3 -m clyde.demo
python3 -m unittest discover -s tests -v
```

The demo uses loopback TCP, an ephemeral port, a controller subprocess, and a
broker in the parent process. Its temporary audit/state is printed then removed.
Expected output includes:

```text
LOCAL SIMULATION: no VM or chroot containment demonstrated
sandbox-introduction: approved (80 bytes)
unapproved-document: denied
Clyde: complete
```

A successful approved-then-denied sequence exits 0. Client failures exit 2 for
unexpected denial of the approved fixture, 3 for malformed/truncated/mismatched
response or unexpected approval, 4 for unavailable/reset broker, 5 for timeout.
The expected second denial is success. There are two fixed proposals, one
connection per request, no retry, and a default 2-second total network deadline
per request (including connect/send/read). CLI timeout must be >0 and <=30 seconds.
Argument errors exit 2. Disk I/O and OS scheduling are outside network deadlines.

## Placement and AI Broker integration

```text
Linux blues (outside NetBSD VM)
  python3 -m clyde.broker
    exact wire-schema validation
    AIBroker.process_task(task, user_role='clyde'), enforcement='strict'
      admission rules -> role -> provider/budget/agent gates
      offline fixture provider -> broker-owned local regular file
    native AI Broker audit/state + durable transport decision audit
                ^ dedicated private IPv4 TCP / port 9411
NetBSD disposable clone
  fishbowl chroot, unprivileged UID
    clyde.controller -> clyde.proposals
```

The existing AI Broker had no fixture provider or guest transport. This change
adds a zero-cost `fixture` provider to its real dispatch interface, a dedicated
policy, and a narrow TCP adapter. Existing default `policy.json` is unchanged.
The adapter makes a fresh private `clyde-state-*` directory beside the requested
audit log, copies `clyde-policy.json` into it, and maps only
`sandbox-introduction` to the operator's `--fixture` path. That runtime policy is
authoritative and reloaded by AI Broker for each request. Both admission rules
and the provider validate IDs; the provider accepts no caller filesystem path.
Clyde cannot choose its role, provider, policy, user identity, or task type.

Caching is disabled, retries are one attempt, fallback is disabled, and fixture
tasks are also excluded from permissive MCP fallback in code. There is no shell,
model invocation, network fetch, training, Dream/WEAVE, or autonomous loop in
this path. `proposals.py` is the replaceable proposal source; the controller and
broker authority need not change when a future generator replaces it.

## Wire contract

Numeric IPv4 addresses only, no DNS. Each TCP connection carries exactly one
UTF-8 JSON object terminated by LF in each direction, then closes. A frame is
limited to 16,384 bytes excluding LF. Documents are UTF-8 strings limited to
2,048 bytes; JSON escaping still fits the frame limit. JSON duplicate keys,
non-objects, malformed encoding, invalid fields, booleans as versions, and
nonstandard numeric constants are rejected. Tokens match `[a-zA-Z0-9_-]{1,64}`.
Unknown operations, extra/missing fields, caller paths, and URLs are rejected.
Additional pipelined requests are never processed.

```json
{"version":1,"request_id":"clyde-1","operation":"fixture-fetch","document_id":"sandbox-introduction"}
{"version":1,"request_id":"clyde-1","status":"approved","document":"..."}
```

An unapproved ID returns the same version and request ID with `status: denied`
and `reason: not-approved`, and no document. Invalid requests/oversize frames
are audited as `invalid-or-timeout` and the connection closes without a document.
Slow/incomplete requests close at the broker's network deadline. The controller
matches version, request ID, exact fields, status, and document size. It treats
documents as data and prints their byte counts, never their instructions or
terminal control sequences. Request IDs correlate exchanges; they are not replay
protection, identities, or authorization tokens.

Only those request fields and approved document bytes/denial cross the guest
boundary. Policy files, fixture paths, provider configuration, native execution
results, and audit paths do not cross it. Native broker events and the transport
JSONL audit remain on Linux. The latter records UTC timestamps, request IDs,
operations, and decisions; malformed fields are recorded as null. It is flushed
and fsynced before returning a document. Audit failures fail closed. It is created
exclusively with mode 0600 and refuses an existing path. Native state is under a
fresh mode-0700 directory. Auditing records broker activity; **it does not detect
sandbox escape**.

TCP here has no cryptographic authentication or encryption. Use only a dedicated
host/guest private link with Linux firewall rules allowing the clone address to
this one port, and block all other guest egress. A chroot alone does not restrict
network access. Another permitted peer could impersonate Clyde; a hostile network
peer could spoof replies. This milestone provides no PKI admission or escape
proof. The single-threaded broker has bounded per-connection network waits but
no denial-of-service protection against repeated connections or disk exhaustion.
Run it in the foreground for the demonstration and stop it with Ctrl-C afterward.

## Disposable NetBSD deployment (not executed here)

No VM image, hypervisor connection, credentials, or configured route to `blues`
was available here. Do not run the broker inside NetBSD, even outside the chroot.
Do not enable or modify the original VM's services, disks, or rc.conf.

1. On `blues`, identify the actual hypervisor, original disk format, full disk
   chain, machine architecture, and network configuration. Obtain a consistent
   read-only export using that hypervisor's supported snapshot/export procedure.
   If the original is already stopped, QEMU can make a standalone disk copy:

   ```sh
   SOURCE=/absolute/path/to/already-offline-original.qcow2
   CLONE_DIR=$(mktemp -d /var/tmp/clyde-vm.XXXXXX)
   qemu-img info --backing-chain "$SOURCE"
   qemu-img convert -O qcow2 "$SOURCE" "$CLONE_DIR/netbsd-clone.qcow2"
   qemu-img check "$CLONE_DIR/netbsd-clone.qcow2"
   ```

   Never copy a running disk this way, use `-U` to bypass locks, or boot the
   source. Import/boot **only** this clone with the original's architecture and
   firmware settings, new VM identity/MAC, and a dedicated private host/guest
   link. A launch command cannot be specified safely without the actual VM
   configuration; this repository supplies no hypervisor inventory. Block
   other egress using the hypervisor/Linux firewall, including IPv6. Validate
   those rules before proceeding. Keep all broker files off guest mounts.

2. Put the updated AI Broker source on Linux `blues` in a new private directory.
   From its root, as a dedicated unprivileged Linux broker account:

   ```sh
   umask 077
   RUN=$(mktemp -d /var/tmp/clyde-broker.XXXXXX)
   BROKER_IP=192.0.2.1   # replace with blues address on private host/guest link
   python3 -m clyde.broker --host "$BROKER_IP" --port 9411 \
     --fixture "$PWD/clyde_fixtures/introduction.txt" --audit "$RUN/audit.jsonl"
   ```

   Use the actual private address on both sides; 192.0.2.1 is a placeholder.
   Record `$RUN` for inspection afterward. Run no general MCP/CLI listener on
   this link. Keep the source, runtime policy, fixture, and audit inaccessible
   to the guest and unprivileged Clyde user.

3. Transfer only `clyde-client.tar.gz` from the deliverables to the disposable
   guest via its management console or an existing trusted transfer mechanism.
   It contains only `__init__.py`, `protocol.py`, `proposals.py`, and
   `controller.py`. On the clone, as root, with an existing fishbowl root:

   ```sh
   FISHBOWL=/absolute/path/to/clone/fishbowl
   mkdir "$FISHBOWL/opt/clyde-v01"  # deliberately fails if already present
   tar -xzf /management/path/clyde-client.tar.gz -C "$FISHBOWL/opt/clyde-v01"
   chown -R root:wheel "$FISHBOWL/opt/clyde-v01"
   chmod -R go-w "$FISHBOWL/opt/clyde-v01"
   ```

   Stop if mkdir fails; use a fresh destination instead of overwriting files.
   Provision Python 3.10+ and its stdlib/shared libraries **inside the clone's
   chroot** from offline packages or a prepared image. Do not assume copying the
   Python executable is sufficient. Do not copy credentials or the broker.
   Choose a dedicated non-root numeric UID/GID with no supplementary groups;
   ensure it cannot write application/interpreter files. From the guest:

   ```sh
   chroot -u 65534 -g 65534 -G 65534 "$FISHBOWL" /usr/pkg/bin/python3.10 -c \
     'import json,socket,time,ipaddress,argparse,math; print("runtime ready")'
   chroot -u 65534 -g 65534 -G 65534 "$FISHBOWL" /usr/pkg/bin/python3.10 -I -c \
     'import sys; sys.path.insert(0,"/opt/clyde-v01"); from clyde.controller import main; raise SystemExit(main())' \
     --host 192.0.2.1 --port 9411 --timeout 2
   ```

   Replace the interpreter path, UID/GID, and private Linux IP with verified
   clone values. Validate the selected account is unprivileged on your image.
   Verify approved then denied, exit 0, and both Linux audits. Stop the Linux
   broker and repeat: expect exit 4 (or 5 if firewall drops). A stalled test
   endpoint must produce exit 5. Inspect guest routes and firewall counters;
   confirm other host services and Internet destinations are unreachable.
   Confirm attempts by Clyde's UID to open policy, fixture storage, audit logs,
   and host files fail. These are deployment checks, not escape detection.
   Shut down and discard only the disposable clone when finished.

## Validation performed

Linux local simulation passes approval, denial, native admission-policy denial,
strict/no-fallback behavior, exact fixture contents, both audits, malformed
requests, unknown operations, extra fields, path/URL rejection, invalid encoding,
duplicate fields, oversized messages/documents, response mismatch/schema errors,
Unicode surrogate rejection, stalled request and response deadlines, unavailable
broker, and audit preservation. The full AI Broker test suite also passes.
NetBSD runtime, chroot permissions, VM boundary transport, private-link firewall,
clone boot, and containment remain untested. Local loopback tests do not establish
any of those properties.
