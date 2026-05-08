# streaming module

One OCI Streaming pool per environment plus seven streams:

| Stream                  | Partitions | Retention | Purpose                                      |
| ----------------------- | ---------- | --------- | -------------------------------------------- |
| `kanto.discovered`      | 3          | 7 days    | Growlithe -> Snorlax                         |
| `kanto.embedded`        | 3          | 7 days    | Ditto -> Alakazam                            |
| `kanto.scored`          | 3          | 7 days    | Alakazam -> Chatot                           |
| `kanto.modal-failures`  | 1          | 7 days    | Modal failure events Snorlax subscribes to   |
| `kanto.discovered.dlq`  | 1          | 30 days   | DLQ; longer retention for human review       |
| `kanto.embedded.dlq`    | 1          | 30 days   | DLQ                                          |
| `kanto.scored.dlq`      | 1          | 30 days   | DLQ                                          |

`auto_create_topics_enable=false` so a typo'd topic name fails loudly instead
of silently creating a junk stream. Streams are encrypted with the env's KMS
key.

## Inputs

| Name             | Type          | Required | Description                                  |
| ---------------- | ------------- | -------- | -------------------------------------------- |
| `compartment_id` | `string`      | yes      | Compartment for the stream pool and streams. |
| `environment`    | `string`      | yes      | Suffix for the stream pool name.             |
| `kms_key_id`     | `string`      | yes      | KMS key OCID for stream encryption.          |
| `freeform_tags`  | `map(string)` | no       | Tags applied to every resource.              |

## Outputs

`stream_pool_id`, `kafka_bootstrap_servers`, `stream_ids`.
