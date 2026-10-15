import argparse
import ipaddress
import math
import socket
import time
from .protocol import MAX_DOCUMENT, ProtocolError, receive, send, token
from .proposals import proposals

CODES = {'denied': 2, 'malformed_response': 3, 'unavailable': 4, 'timeout': 5}

class Outcome(Exception):
    def __init__(self, name):
        self.name = name

def endpoint(host):
    ipaddress.IPv4Address(host)  # Numeric IPv4 only: no DNS dependency.
    return host

def positive(value):
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 30:
        raise argparse.ArgumentTypeError('timeout must be >0 and <=30 seconds')
    return number

def fetch(host, port, document, request_id, timeout=2):
    request = dict(version=1, request_id=request_id, operation='fixture-fetch', document_id=document)
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((endpoint(host), port))
            send(sock, request, deadline)
            response = receive(sock, deadline)
    except TimeoutError as exc:
        raise Outcome('timeout') from exc
    except ProtocolError as exc:
        raise Outcome('malformed_response') from exc
    except OSError as exc:
        raise Outcome('unavailable') from exc
    status = response.get('status')
    keys = {'version', 'request_id', 'status', 'document' if status == 'approved' else 'reason'}
    if (set(response) != keys or type(response.get('version')) is not int or
            response['version'] != 1 or response.get('request_id') != request_id or
            status not in ('approved', 'denied')):
        raise Outcome('malformed_response')
    if status == 'approved':
        content = response['document']
        try:
            if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_DOCUMENT:
                raise Outcome('malformed_response')
        except UnicodeError as exc:
            raise Outcome('malformed_response') from exc
    elif response['reason'] != 'not-approved':
        raise Outcome('malformed_response')
    return response

def run(host, port, timeout=2):
    for index, document in enumerate(proposals(), 1):
        response = fetch(host, port, document, 'clyde-%d' % index, timeout)
        status = response['status']
        # Document is data: report length only, never execute or render its contents.
        print('%s: %s%s' % (document, status, ' (%d bytes)' % len(response['document'].encode()) if status == 'approved' else ''))
        if (index == 1 and status != 'approved'):
            raise Outcome('denied')
        if index == 2 and status != 'denied':
            raise Outcome('malformed_response')
    print('Clyde: complete')
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=endpoint, default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9411)
    parser.add_argument('--timeout', type=positive, default=2)
    args = parser.parse_args()
    try:
        return run(args.host, args.port, args.timeout)
    except Outcome as exc:
        print('Clyde: ' + exc.name)
        return CODES[exc.name]

if __name__ == '__main__':
    raise SystemExit(main())
