# infrastructure/console_extras.tf
#
# Free AWS console features for observability and security (#64). All three are outside the
# geyser-claude permissions boundary (ce:*, access-analyzer:* and servicecatalog:* are not in its
# allow-list, and Cost Explorer is a us-east-1 API), so the user applies this file's resources.

# Cost Anomaly Detection: AWS learns the account's normal daily spend per service and emails when a day
# looks unusual, usually well before the $10 monthly budget alert would fire. Free.
# AWS allows one DIMENSIONAL/SERVICE monitor per account. If the account already has one (the console
# may have created "Default-Services-Monitor"), set var.existing_anomaly_monitor_arn to its ARN: then no
# monitor is created and the subscription attaches to that one. Check before the first apply:
#   AWS_PROFILE=default aws ce get-anomaly-monitors --query "AnomalyMonitors[].[MonitorName,MonitorType,MonitorDimension,MonitorArn]"
resource "aws_ce_anomaly_monitor" "services" {
  count             = var.existing_anomaly_monitor_arn == "" ? 1 : 0
  name              = "${var.project_name}-services"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "daily_email" {
  name             = "${var.project_name}-anomaly-email"
  frequency        = "DAILY"
  monitor_arn_list = [var.existing_anomaly_monitor_arn != "" ? var.existing_anomaly_monitor_arn : aws_ce_anomaly_monitor.services[0].arn]

  subscriber {
    type    = "EMAIL"
    address = var.budget_alert_email # from terraform.tfvars, never committed
  }

  # Only anomalies whose total impact reaches this many dollars: at this account's spend, cents of noise
  # would otherwise email daily.
  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = [tostring(var.anomaly_alert_usd)]
    }
  }
}

# IAM Access Analyzer (external access, account zone of trust): flags any resource here that a principal
# outside the account can reach, e.g. a bucket policy or role trust that is too open. Free. Useful for
# the going-public checklist (#47).
resource "aws_accessanalyzer_analyzer" "account" {
  analyzer_name = "${var.project_name}-account-analyzer"
  type          = "ACCOUNT"
}

# myApplications: one console page with the project's cost, alarms and resources. AWS groups resources
# by the application's awsApplication tag. A provider's default_tags can't read a resource it is still
# creating, so this takes two applies:
#   1. apply this file: the application exists; `terraform output aws_application_tag` shows its tag;
#   2. set aws_application_tag in terraform.tfvars to that map and apply again: default_tags then puts
#      it on every resource (an in-place tag update).
resource "aws_servicecatalogappregistry_application" "geyser" {
  name        = "${var.project_name}-${var.environment}"
  description = "Geyser Genomics: FASTQ to VCF on S3, Lambda, Step Functions and AWS Batch."
}

output "aws_application_tag" {
  description = "Set var.aws_application_tag to this map for myApplications (step 2 above)."
  value       = aws_servicecatalogappregistry_application.geyser.application_tag
}
