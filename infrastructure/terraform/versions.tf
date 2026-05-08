terraform {
  required_version = ">= 1.6"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 8.12"
    }
  }

  # Remote state on OCI Object Storage via the S3-compat endpoint.
  # The actual bucket / key / endpoint live in config/<env>-backend.hcl
  # and are passed at init time:
  #   terraform init -backend-config=config/<env>-backend.hcl
  backend "s3" {}
}
