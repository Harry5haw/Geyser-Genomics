#!/usr/bin/env python3
"""Create or update the Geyser Genomics failure alarm, sending to the pipeline's SNS topic.

Design: one email per incident. A failed stage is already emailed by the state machine's Notify_Failure
step, so this script deliberately creates no ExecutionsFailed or per-stage FailureCount alarms (they
would email the same failure two more times). It creates one alarm for the gap Notify_Failure cannot
see: the trigger Lambda failing, so that an upload never starts a run.

  <prefix>-trigger-lambda-errors     AWS/Lambda Errors >= 1 for the trigger function

See FAILURE_NOTIFICATIONS in scripts/monitoring.py for who emails for each kind of failure.

Preview (no AWS calls; no ARN needed, a placeholder is used):
  python scripts/deploy_alarms.py --dry-run

Deploy (uses your ambient AWS credentials):
  SNS_TOPIC_ARN=<topic arn> python scripts/deploy_alarms.py
"""

import argparse
import os
import sys

import monitoring

PLACEHOLDER_SNS = "<SNS_TOPIC_ARN>"


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
    p = argparse.ArgumentParser(description="Deploy the Geyser Genomics CloudWatch failure alarm.")
    p.add_argument("--region", default=monitoring.DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument("--sns-topic-arn", default=os.environ.get("SNS_TOPIC_ARN"),
                   help="SNS topic for alarm notifications (default: $SNS_TOPIC_ARN)")
    p.add_argument("--trigger-function-name",
                   default=os.environ.get("TRIGGER_FUNCTION_NAME", monitoring.DEFAULT_TRIGGER_FUNCTION),
                   help=f"Trigger Lambda name (default: $TRIGGER_FUNCTION_NAME or "
                        f"{monitoring.DEFAULT_TRIGGER_FUNCTION})")
    p.add_argument("--prefix", default="geyser-dev", help="Alarm name prefix (default: geyser-dev)")
    p.add_argument("--dry-run", action="store_true", help="Print the alarm definitions and exit (no AWS calls)")
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    args = parse_args(argv)
    monitoring.check_arn(args.sns_topic_arn, "sns", "--sns-topic-arn")
    sns_arn = args.sns_topic_arn
    if args.dry_run:
        sns_arn = sns_arn or PLACEHOLDER_SNS
    elif not sns_arn:
        raise SystemExit("FATAL: provide --sns-topic-arn or set SNS_TOPIC_ARN")

    try:
        alarms = monitoring.build_alarms(sns_arn, trigger_function_name=args.trigger_function_name,
                                         prefix=args.prefix)
    except ValueError as err:
        raise SystemExit(f"FATAL: {err}")

    if args.dry_run:
        print(monitoring.to_json(alarms))
        return 0
    if client is None:
        import boto3
        client = boto3.client("cloudwatch", region_name=args.region)
    return deploy(client, alarms)


if __name__ == "__main__":
    sys.exit(main())
