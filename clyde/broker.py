import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socketserver
import time
import tempfile
from ai_broker import AIBroker
from .controller import endpoint, positive
from .protocol import MAX_DOCUMENT, ProtocolError, receive, send, token

class Broker(socketserver.TCPServer):
    allow_reuse_address = False

    def __init__(self, address, fixture, audit, timeout=2):
        # Trusted operator supplies fixture path; no path ever comes from the wire.
        with open(fixture, 'rb') as stream:
            data = stream.read(MAX_DOCUMENT + 1)
        if len(data) > MAX_DOCUMENT:
            raise ValueError('fixture too large')
        data.decode('utf-8')
        self.timeout = timeout
        fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.audit = os.fdopen(fd, 'w', encoding='utf-8')
        try:
            # Dedicated private state, never the operator's general broker policy/state.
            self.state_dir = Path(tempfile.mkdtemp(prefix='clyde-state-', dir=Path(audit).parent))
            policy = json.loads((Path(__file__).parent.parent / 'clyde-policy.json').read_text())
            policy['fixture_documents'] = {'sandbox-introduction': str(Path(fixture).resolve())}
            policy_path = self.state_dir / 'policy.json'
            policy_path.write_text(json.dumps(policy), encoding='utf-8')
            self.backend = AIBroker(
                policy_path=str(policy_path), cache_path=str(self.state_dir / 'cache.json'),
                budget_ledger_path=str(self.state_dir / 'budget.json'),
                provider_status_path=str(self.state_dir / 'providers.json'),
                audit_log_path=str(self.state_dir / 'ai-broker-audit.jsonl'), enforcement='strict')
            super().__init__(address, Handler)
        except BaseException:
            self.audit.close()
            raise

    def record(self, request_id, operation, decision):
        self.audit.write(json.dumps(dict(timestamp=datetime.now(timezone.utc).isoformat(),
            request_id=request_id, operation=operation, decision=decision)) + '\n')
        self.audit.flush()
        os.fsync(self.audit.fileno())  # Fail closed before returning a document.

    def server_close(self):
        super().server_close()
        self.audit.close()

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        deadline = time.monotonic() + self.server.timeout
        rid = operation = None
        try:
            request = receive(self.request, deadline)
            rid = request.get('request_id') if token(request.get('request_id')) else None
            operation = request.get('operation') if token(request.get('operation')) else None
            valid = (set(request) == {'version', 'request_id', 'operation', 'document_id'} and
                     type(request.get('version')) is int and request['version'] == 1 and
                     rid is not None and operation == 'fixture-fetch' and token(request.get('document_id')))
            if not valid:
                raise ProtocolError('invalid request')
            task = dict(type='fixture', intent='fetch', provider='fixture',
                        payload={'document_id': request['document_id']})
            result = self.server.backend.process_task(task, user_role='clyde')
            approved = result.get('ok') is True
            if approved and (not isinstance(result.get('result'), str) or
                             len(result['result'].encode('utf-8')) > MAX_DOCUMENT):
                raise ProtocolError('invalid provider document')
            self.server.record(rid, operation, 'approved' if approved else 'denied')
            response = dict(version=1, request_id=rid, status='approved' if approved else 'denied')
            response.update({'document': result['result']} if approved else {'reason': 'not-approved'})
            send(self.request, response, deadline)
        except (ProtocolError, TimeoutError):
            self.server.record(rid, operation, 'invalid-or-timeout')
            # Invalid framing/schema: close connection, no document returned.
        except OSError:
            # Audit errors fail closed; disconnected callers get no further response.
            return

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=endpoint, default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9411)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--timeout', type=positive, default=2)
    args = parser.parse_args()
    with Broker((args.host, args.port), args.fixture, args.audit, args.timeout) as server:
        print('Broker listening on %s:%d' % server.server_address, flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    main()
