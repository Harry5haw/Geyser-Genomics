# infrastructure/web/variables.tf. project_name and environment come from ../terraform.tfvars (as for the account
# stack); the rest has defaults and is filled in once the main stack's API exists.

variable "project_name" {
  description = "Prefix for resource names (as in the main stack)."
  type        = string
  default     = "geyser"
}

variable "environment" {
  description = "The environment name (as in the main stack)."
  type        = string
  default     = "dev"
}

variable "price_class" {
  description = "CloudFront edge locations: PriceClass_100 (Europe and North America, London included) is the cheapest."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be PriceClass_100, PriceClass_200 or PriceClass_All."
  }
}

variable "api_origin" {
  description = "The web API's origin for the CSP's connect-src, e.g. https://abc123.lambda-url.eu-west-2.on.aws (the main stack's web_api_url, without the trailing slash). Empty until then."
  type        = string
  default     = ""

  validation {
    condition     = var.api_origin == "" || can(regex("^https://[a-z0-9.-]+$", var.api_origin))
    error_message = "api_origin must be empty or an https:// origin with no path or trailing slash."
  }
}

variable "data_lake_origin" {
  description = "The data lake's regional S3 origin, e.g. https://geyser-data-lake-<suffix>.s3.eu-west-2.amazonaws.com: the browser PUTs upload parts there (connect-src). Empty until uploads are on."
  type        = string
  default     = ""

  validation {
    condition     = var.data_lake_origin == "" || can(regex("^https://[a-z0-9.-]+\\.s3\\.[a-z0-9-]+\\.amazonaws\\.com$", var.data_lake_origin))
    error_message = "data_lake_origin must be empty or https://<bucket>.s3.<region>.amazonaws.com."
  }
}

variable "script_hashes" {
  description = "CSP hashes for the site's inline scripts, e.g. [\"sha256-...\"]: csp-hashes.json's \"script\" list in the published site's root (today one, the speculation rules). The only inline script allowed."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for h in var.script_hashes : can(regex("^sha(256|384|512)-[A-Za-z0-9+/]+=*$", h))])
    error_message = "Each script hash must look like sha256-<base64>."
  }
}

variable "style_hashes" {
  description = "CSP hashes for inline <style> blocks: csp-hashes.json's \"style\" list (the inlined site CSS and the figures page's block)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for h in var.style_hashes : can(regex("^sha(256|384|512)-[A-Za-z0-9+/]+=*$", h))])
    error_message = "Each style hash must look like sha256-<base64>."
  }
}
