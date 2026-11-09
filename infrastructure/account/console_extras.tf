# infrastructure/account/console_extras.tf
#
# Free AWS console features for observability and security (#64), applied by the user (see main.tf).

# Cost Anomaly Detection: AWS learns the account's normal daily spend per service and emails when a day
# looks unusual, usually well before the $10 monthly budget alert would fire. Free.
# AWS allows one DIMENSIONAL/SERVICE monitor per account, and the console already created one
# ("Default-Services-Monitor", listed by the user on 2 Nov 2026). It is adopted as is, not recreated:
# the arguments match it, so the import changes nothing.
import {
  to = aws_ce_anomaly_monitor.services
  id = "arn:aws:ce::${data.aws_caller_identity.current.account_id}:anomalymonitor/50e63a29-285c-4a0c-8e89-0f66ca9840ee"
}

resource "aws_ce_anomaly_monitor" "services" {
  name              = "Default-Services-Monitor"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "daily_email" {
  name             = "${var.project_name}-anomaly-email"
  frequency        = "DAILY"
  monitor_arn_list = [aws_ce_anomaly_monitor.services.arn]

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

# No myApplications (AppRegistry): AWS put AppRegistry in maintenance mode and closed it to new customers
# on 30 July 2026, so creating the application fails with 403 (9 Nov 2026). Resource Groups and the
# Project/Environment/Stage cost allocation tags cover the same grouping.
