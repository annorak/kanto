variable "resource_group_name" {
  description = "Azure resource group for the namespace and event hubs."
  type        = string
}

variable "region" {
  description = "Azure region for the namespace."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'. Suffixed with a random hex string for global namespace uniqueness."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

# Event Hubs (topics) the design doc names. Partition counts: 4 for main
# streams (Standard tier minimum is 2, Kafka-style fan-out wants a few),
# 2 per DLQ (volume should be near zero, but Standard tier minimum is 2).
# Standard tier retention max is 7 days — DLQs share that ceiling; oncall
# must triage DLQ messages within a week.
locals {
  event_hubs = {
    "kanto.discovered"     = { partitions = 4, retention_days = 7 }
    "kanto.embedded"       = { partitions = 4, retention_days = 7 }
    "kanto.scored"         = { partitions = 4, retention_days = 7 }
    "kanto.modal-failures" = { partitions = 2, retention_days = 7 }
    "kanto.discovered.dlq" = { partitions = 2, retention_days = 7 }
    "kanto.embedded.dlq"   = { partitions = 2, retention_days = 7 }
    "kanto.scored.dlq"     = { partitions = 2, retention_days = 7 }
  }
}
