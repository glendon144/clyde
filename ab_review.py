#!/usr/bin/env python3
"""Human-only review console for AB master 0.1."""
import argparse
import json
import sys
from ab_master import exchange

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--socket', required=True, help='Master-private admin socket')
parser.add_argument('request_id')
args = parser.parse_args()
if not sys.stdin.isatty():
    parser.error('Human review requires an interactive terminal')
try:
    result = exchange(args.socket, {'action': 'show', 'request_id': args.request_id})
    print(json.dumps(result, indent=2))
    if not result.get('ok'):
        sys.exit(1)
    approve = input('Type APPROVE to execute this exact request; anything else denies: ') == 'APPROVE'
    result = exchange(args.socket, {'action': 'approve' if approve else 'deny',
                                  'request_id': args.request_id, 'digest': result['digest']})
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get('ok') else 1)
except (OSError, ValueError, EOFError) as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
