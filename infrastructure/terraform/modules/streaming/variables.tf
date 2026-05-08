variable "compartment_id" {
  description = "Compartment OCID for the stream pool and streams."
  type        = string
}

variable "environment" {
  description = "Environment name suffix on stream pool and stream names."
  type        = string
}

variable "kms_key_id" {
  description = "KMS key OCID. Without this OCI uses Oracle-managed keys; we always pass our own."
  type        = string
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

# Streams the design doc names. Partition counts: 3 for main streams (small
# parallelism for v1, can grow), 1 per DLQ (volume should be near zero).
# Retention: 7 days for main, 30 days for DLQ (DLQs may sit waiting on
# human review, per Section 10 of the design doc).
locals {
  streams = {
    "kanto.discovered"     = { partitions = 3, retention_hours = 168 }
    "kanto.embedded"       = { partitions = 3, retention_hours = 168 }
    "kanto.scored"         = { partitions = 3, retention_hours = 168 }
    "kanto.modal-failures" = { partitions = 1, retention_hours = 168 }
    "kanto.discovered.dlq" = { partitions = 1, retention_hours = 720 }
    "kanto.embedded.dlq"   = { partitions = 1, retention_hours = 720 }
    "kanto.scored.dlq"     = { partitions = 1, retention_hours = 720 }
  }
}
