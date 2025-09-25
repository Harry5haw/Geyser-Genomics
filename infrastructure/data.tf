# infrastructure/data.tf

# This file consolidates all common data sources to ensure they are available
# to all other .tf files in this module.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# DELETE the data blocks for aws_vpc, aws_subnets, and aws_iam_role from here.
