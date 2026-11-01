#!/usr/bin/env python3
"""Create or update the Geyser Genomics failure alarms, sending to the pipeline's SNS topic.

Alarms (definitions in scripts/monitoring.py):
  <prefix>-pipeline-executions-failed      Step Functions ExecutionsFailed >= 1 (needs the state machine ARN)
  <prefix>-stage-failed-<TaskName>         FailureCount >= 1 for each stage that records failures

Note: the state machine's Notify_Failure step already emails on a caught failure. These alarms also
catch failures that bypass it and give the console an alarm history.

Preview (no AWS calls; ARNs not needed, placeholders are used):
  python scripts/deploy_alarms.py --dry-run

Deploy (uses your ambient AWS credentials):
  SNS_TOPIC_ARN=<topic arn> SFN_ARN=<state machine arn> python scripts/deploy_alarms.py
"""

import argparse
import os
import sys

import monitoring

PLACEHOLDER_SNS = "<SNS_TOPIC_ARN>"
PLACEHOLDER_SFN = "<SFN_ARN>"


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
    p = argparse.ArgumentParser(description="Deploy the Geyser Genomics CloudWatch failure alarms.")
    p.add_argument("--region", default=monitoring.DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument("--namespace", default=monitoring.DEFAULT_NAMESPACE)
    p.add_argument("--sns-topic-arn", default=os.environ.get("SNS_TOPIC_ARN"),
                   help="SNS topic for alarm notifications (default: $SNS_TOPIC_ARN)")
    p.add_argument("--state-machine-arn", default=os.environ.get("SFN_ARN"),
                   help="Step Functions state machine ARN (default: $SFN_ARN). Without it the "
                        "ExecutionsFailed alarm is left out.")
    p.add_argument("--prefix", default="geyser-dev", help="Alarm name prefix (default: geyser-dev)")
    p.add_argument("--schema", choices=monitoring.SCHEMAS, default="per-sample",
                   help="per-sample: Metrics Insights alarms over all SampleIds (today's metrics); "
                        "aggregated: plain alarms on FailureCount by TaskName")
    p.add_argument("--groups", default=",".join(monitoring.STAGE_GROUPS),
                   help=f"Comma-separated stage groups (default: all of {','.join(monitoring.STAGE_GROUPS)})")
    p.add_argument("--dry-run", action="store_true", help="Print the alarm definitions and exit (no AWS calls)")
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    args = parse_args(argv)
    monitoring.check_arn(args.sns_topic_arn, "sns", "--sns-topic-arn")
    monitoring.check_arn(args.state_machine_arn, "states", "--state-machine-arn")
    sns_arn, sfn_arn = args.sns_topic_arn, args.state_machine_arn
    if args.dry_run:
        sns_arn = sns_arn or PLACEHOLDER_SNS
        sfn_arn = sfn_arn or PLACEHOLDER_SFN
    elif not sns_arn:
        raise SystemExit("FATAL: provide --sns-topic-arn or set SNS_TOPIC_ARN")
    if not args.state_machine_arn:
        print("NOTE: no --state-machine-arn/SFN_ARN" +
              (", using a placeholder." if args.dry_run else ", so the ExecutionsFailed alarm is left out."),
              file=sys.stderr)

    try:
        alarms = monitoring.build_alarms(
            sns_arn, sfn_arn, namespace=args.namespace, schema=args.schema, prefix=args.prefix,
            groups=[g.strip() for g in args.groups.split(",") if g.strip()])
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
