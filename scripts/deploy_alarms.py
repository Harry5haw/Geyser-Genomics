#!/usr/bin/env python3
"""Create or update the Geyser Genomics CloudWatch alarm.

  <prefix>-trigger-lambda-errors     AWS/Lambda Errors >= 1 for the trigger function

The trigger Lambda failing is the one failure that leaves no trace in the pipeline's own metrics (no run
starts). Its state shows in the dashboard's alarm status widget. Email is off by default: pass
--sns-topic-arn to also notify that topic.

Preview (no AWS calls):
  python scripts/deploy_alarms.py --dry-run

Deploy (uses your ambient AWS credentials):
  python scripts/deploy_alarms.py
  python scripts/deploy_alarms.py --sns-topic-arn <topic arn>    # also email
"""

import argparse
import os
import sys

import monitoring


def deploy(client, alarms: list) -> int:
    failed = 0
    for alarm in alarms:
        try:
            client.put_metric_alarm(**alarm)
            print(f"[OK] {alarm['AlarmName']}")
        except Exception as err:  # noqa: BLE001 - report each failure, keep going
            failed += 1
            print(f"[ERROR] {alarm['AlarmName']}: {err}", file=sys.stderr)
    return 1 if failed else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Deploy the Geyser Genomics CloudWatch alarm.")
    p.add_argument("--region", default=monitoring.DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument("--sns-topic-arn", default=None,
                   help="Optional: also notify this SNS topic (email). Off by default.")
    p.add_argument("--trigger-function-name",
                   default=os.environ.get("TRIGGER_FUNCTION_NAME", monitoring.DEFAULT_TRIGGER_FUNCTION),
                   help=f"Trigger Lambda name (default: $TRIGGER_FUNCTION_NAME or "
                        f"{monitoring.DEFAULT_TRIGGER_FUNCTION})")
    p.add_argument("--prefix", default=monitoring.DEFAULT_ALARM_PREFIX, help="Alarm name prefix (default: geyser-dev)")
    p.add_argument("--dry-run", action="store_true", help="Print the alarm definitions and exit (no AWS calls)")
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    args = parse_args(argv)
    monitoring.check_arn(args.sns_topic_arn, "sns", "--sns-topic-arn")
    try:
        alarms = monitoring.build_alarms(args.sns_topic_arn, trigger_function_name=args.trigger_function_name,
                                         prefix=args.prefix)
    except ValueError as err:
        raise SystemExit(f"FATAL: {err}") from err

    if args.dry_run:
        print(monitoring.to_json(alarms))
        return 0
    if client is None:
        import boto3
        client = boto3.client("cloudwatch", region_name=args.region)
    return deploy(client, alarms)


if __name__ == "__main__":
    sys.exit(main())
