# event_hubs module

One Event Hubs namespace (Standard tier, Kafka surface) plus seven event
hubs (Kafka "topics"):

| Event Hub               | Partitions | Retention | Purpose                                  |
| ----------------------- | ---------- | --------- | ---------------------------------------- |
| `kanto.discovered`      | 4          | 7 days    | Growlithe → Snorlax                      |
| `kanto.embedded`        | 4          | 7 days    | Ditto → Alakazam                         |
| `kanto.scored`          | 4          | 7 days    | Alakazam → Chatot                        |
| `kanto.modal-failures`  | 2          | 7 days    | Modal failure events Snorlax subscribes  |
| `kanto.discovered.dlq`  | 2          | 7 days    | DLQ; Event Hubs Standard cap is 7 days   |
| `kanto.embedded.dlq`    | 2          | 7 days    | DLQ                                      |
| `kanto.scored.dlq`      | 2          | 7 days    | DLQ                                      |

**Tier choice:** Standard, not Basic. Basic only supports AMQP — Kafka
protocol surface requires Standard. 1 throughput unit covers dev load;
auto-inflate to 2 if the broker hits the per-TU ingress/egress ceiling.

**Kafka clients** connect to `<namespace>.servicebus.windows.net:9093` with
either SASL/PLAIN (username `$ConnectionString`, password = namespace
connection string) or SASL/OAUTHBEARER via Azure AD. The IAM module wires
the second; the first is available as a fallback for local development.

## Inputs

See `variables.tf`. Required: `resource_group_name`, `region`, `name_prefix`.

## Outputs

`namespace_id`, `namespace_name`, `kafka_bootstrap_servers`,
`event_hub_ids` (map), `event_hub_names`.
