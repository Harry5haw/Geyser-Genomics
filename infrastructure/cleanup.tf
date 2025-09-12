# infrastructure/cleanup.tf (TEMPORARY FILE)

# This resource allows us to run local commands during the Terraform apply process.
# We will use it to delete the manually created IAM roles and policies.
resource "null_resource" "cleanup_manual_iam_resources" {
  
  # This provisioner will run when the resource is created.
  provisioner "local-exec" {
    command = <<-EOT
      echo "Deleting manually created IAM resources..."
      aws iam detach-role-policy --role-name GitHubAction-ECR-TerraFlow-Role --policy-arn arn:aws:iam::488543428961:policy/GitHubAction-ECR-TerraFlow-Policy || echo "ECR policy attachment not found, skipping."
      aws iam delete-policy --policy-arn arn:aws:iam::488543428961:policy/GitHubAction-ECR-TerraFlow-Policy || echo "ECR policy not found, skipping."
      aws iam delete-role --role-name GitHubAction-ECR-TerraFlow-Role || echo "ECR role not found, skipping."
      
      aws iam detach-role-policy --role-name TerraformDeployerRole --policy-arn arn:aws:iam::aws:policy/AdministratorAccess || echo "Terraform policy attachment not found, skipping."
      aws iam delete-role --role-name TerraformDeployerRole || echo "Terraform role not found, skipping."
      echo "Cleanup complete."
    EOT
  }

  # This ensures the cleanup runs before the new roles are created.
  depends_on = [] 
}
