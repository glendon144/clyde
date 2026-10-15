#!/usr/bin/env python3
"""Generate an AI Broker status report JSON file.

This MVP performs a non-spending status/scorecard pass. It reads policy,
runtime health state, and budget state; it does not call cloud providers.
"""

import argparse
import json

from ai_broker import AIBroker, DEFAULT_STATUS_REPORT


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate AI Broker status report JSON")
    parser.add_argument("--policy", default="policy.json", help="Path to policy.json")
    parser.add_argument("--output", default=DEFAULT_STATUS_REPORT, help="Path to write status JSON")
    parser.add_argument("--role", default="developer", help="Role used to test provider eligibility")
    parser.add_argument("--admin", action="store_true", help="Include private/admin-only details")
    parser.add_argument("--print", dest="print_report", action="store_true", help="Print JSON to stdout")
    args = parser.parse_args()

    broker = AIBroker(policy_path=args.policy)
    report = broker.write_status_report(
        output_path=args.output,
        user_role=args.role,
        public=not args.admin,
    )
    if args.print_report:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
