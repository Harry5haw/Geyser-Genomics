#!/usr/bin/env python3
"""Create or update the Geyser Genomics CloudWatch dashboard.

Widgets: alarm status, pipeline runs per day and end-to-end time (Step Functions, when a state machine
ARN is given), failures by stage, result quality per sample from the Batch job logs (Logs Insights:
mapped %, duplicate %, mean depth, variants, Ti/Tv, and the samples outside thresholds; --no-quality
leaves them out), and stage durations for the core pipeline, DeepVariant and pangenome DeepVariant.
The alarm widget needs the account ID: --account-id, $AWS_ACCOUNT_ID, or looked up with STS on deploy
(a placeholder is used with --dry-run).
The JSON is built in scripts/monitoring.py.

Preview (no AWS calls):
  python scripts/deploy_dashboard.py --dry-run
  python scripts/deploy_dashboard.py --dry-run --state-machine-arn <arn> --schema aggregated

Deploy (uses your ambient AWS credentials):
  SFN_ARN=<state machine arn> python scripts/deploy_dashboard.py
"""

import argparse
import os
import sys

import monitoring

PLACEHOLDER_ACCOUNT = "<ACCOUNT_ID>"


def deploy(client, dashboard_name: str, body: dict) -> int:
    try:
        resp = client.put_dashboard(DashboardName=dashboard_name, DashboardBody=monitoring.to_json(body))
    except Exception as err:  # noqa: BLE001 - report any AWS error and exit non-zero
        print(f"[ERROR] Failed to put dashboard: {err}", file=sys.stderr)
        return 1
    messages = resp.get("DashboardValidationMessages") or []
    for message in messages:
        print(f"[WARNING] {message}", file=sys.stderr)
    print(f"[OK] Dashboard '{dashboard_name}' deployed{' with warnings' if messages else ''}.")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Deploy the Geyser Genomics CloudWatch dashboard.")
    p.add_argument("--dashboard-name", default="geyser-dashboard-dev")
    p.add_argument("--region", default=monitoring.DEFAULT_REGION, help="AWS region (default: eu-west-2)")
    p.add_argument("--namespace", default=monitoring.DEFAULT_NAMESPACE)
    p.add_argument(
        "--state-machine-arn",
        default=os.environ.get("SFN_ARN"),
        help="Step Functions state machine ARN (default: $SFN_ARN). Without it the "
        "Step Functions widgets are left out.",
    )
    p.add_argument(
        "--schema",
        choices=monitoring.SCHEMAS,
        default="per-sample",
        help="Metric dimensions to read: per-sample (what app/metrics.py publishes today) "
        "or aggregated (no SampleId; gives real p50/p95)",
    )
    p.add_argument(
        "--groups",
        default=",".join(monitoring.STAGE_GROUPS),
        help=f"Comma-separated stage groups (default: all of {','.join(monitoring.STAGE_GROUPS)})",
    )
    p.add_argument("--lookback", default="-P14D", help="Default time range, e.g. -P3D or -P14D")
    p.add_argument(
        "--alarm-prefix",
        default=monitoring.DEFAULT_ALARM_PREFIX,
        help="Prefix used by deploy_alarms.py (default: geyser-dev)",
    )
    p.add_argument(
        "--account-id",
        default=os.environ.get("AWS_ACCOUNT_ID"),
        help="AWS account ID for the alarm widget (default: $AWS_ACCOUNT_ID, else looked up with STS)",
    )
    p.add_argument("--no-alarm-widget", action="store_true", help="Leave out the alarm status widget")
    p.add_argument(
        "--bucket",
        default=os.environ.get("BUCKET_NAME"),
        help="Data lake bucket for the S3 link (default: $BUCKET_NAME; link left out if unset)",
    )
    p.add_argument(
        "--trigger-function-name",
        default=os.environ.get("TRIGGER_FUNCTION_NAME", monitoring.DEFAULT_TRIGGER_FUNCTION),
        help="Trigger Lambda name for its console links",
    )
    p.add_argument(
        "--sfn-log-group",
        default=monitoring.DEFAULT_SFN_LOG_GROUP,
        help="Step Functions log group for its console link",
    )
    p.add_argument(
        "--batch-log-group",
        default=monitoring.BATCH_LOG_GROUP,
        help=f"Batch job log group the quality widgets query (default: {monitoring.BATCH_LOG_GROUP})",
    )
    p.add_argument(
        "--no-quality",
        action="store_true",
        help="Leave out the result-quality widgets (each view runs a Logs Insights scan of the Batch log group)",
    )
    p.add_argument("--dry-run", action="store_true", help="Print the dashboard JSON and exit (no AWS calls)")
    return p.parse_args(argv)


def resolve_account_id(args, sts_client=None) -> str:
    if args.account_id:
        if not (args.account_id.isdigit() and len(args.account_id) == 12):
            raise SystemExit(f"FATAL: --account-id must be 12 digits: {args.account_id!r}")
        return args.account_id
    if args.dry_run:
        return PLACEHOLDER_ACCOUNT
    if sts_client is None:
        import boto3

        sts_client = boto3.client("sts", region_name=args.region)
    return sts_client.get_caller_identity()["Account"]


def existing_alarms(client, names) -> list:
    """The alarms among `names` that exist, so the status widget never points at a missing one."""
    try:
        found = {a["AlarmName"] for a in client.describe_alarms(AlarmNames=list(names)).get("MetricAlarms", [])}
    except Exception as err:  # noqa: BLE001 - e.g. no cloudwatch:DescribeAlarms; don't block the deploy
        print(f"WARNING: could not check the alarms exist ({err}); adding them anyway.", file=sys.stderr)
        return list(names)
    for name in names:
        if name not in found:
            print(
                f"NOTE: alarm '{name}' does not exist (run deploy_alarms.py first, same --prefix); "
                "left off the dashboard.",
                file=sys.stderr,
            )
    return [n for n in names if n in found]


def main(argv=None, client=None, sts_client=None) -> int:
    args = parse_args(argv)
    monitoring.check_arn(args.state_machine_arn, "states", "--state-machine-arn")
    if client is None and not args.dry_run:
        import boto3

        client = boto3.client("cloudwatch", region_name=args.region)
    alarm_arns = None
    if not args.no_alarm_widget:
        names = monitoring.alarm_names(args.alarm_prefix)
        if not args.dry_run:
            names = existing_alarms(client, names)
        account_id = resolve_account_id(args, sts_client) if names else None
        alarm_arns = [monitoring.alarm_arn(args.region, account_id, name) for name in names]
    try:
        body = monitoring.build_dashboard(
            args.region,
            args.namespace,
            state_machine_arn=args.state_machine_arn,
            schema=args.schema,
            groups=[g.strip() for g in args.groups.split(",") if g.strip()],
            lookback=args.lookback,
            alarm_arns=alarm_arns,
            links=monitoring.console_links(
                args.region,
                state_machine_arn=args.state_machine_arn,
                bucket=args.bucket,
                trigger_function_name=args.trigger_function_name,
                sfn_log_group=args.sfn_log_group,
                batch_log_group=args.batch_log_group,
            ),
            quality=not args.no_quality,
            batch_log_group=args.batch_log_group,
        )
    except ValueError as err:
        raise SystemExit(f"FATAL: {err}") from err
    if not args.state_machine_arn:
        print("NOTE: no --state-machine-arn/SFN_ARN, so the Step Functions widgets are left out.", file=sys.stderr)

    if args.dry_run:
        print(monitoring.to_json(body))
        return 0
    return deploy(client, args.dashboard_name, body)


if __name__ == "__main__":
    sys.exit(main())
