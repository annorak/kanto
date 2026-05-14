# network module

VNet, subnets, NSG, and the private DNS zone Postgres Flexible Server
registers in.

| Subnet     | CIDR (within /16) | Purpose                                         |
| ---------- | ----------------- | ----------------------------------------------- |
| nodes      | `.1.0/24`         | AKS worker node VNICs                           |
| pods       | `.16.0/20`        | AKS pod IPs (Azure CNI)                         |
| lb         | `.0.0/26`         | Public-facing Service LoadBalancers (Task 5+)   |
| mew        | `.32.0/28`        | Postgres Flexible Server delegated subnet       |

The `mew` subnet is delegated to `Microsoft.DBforPostgreSQL/flexibleServers`,
so no other resource can use it. The private DNS zone is linked to the VNet
without registration_enabled — Flexible Server registers itself.

The `nodes` subnet has service endpoints for Storage, Key Vault, and Event
Hubs so managed-service traffic from worker nodes stays on the Microsoft
backbone and avoids NAT egress cost.

## Inputs

See `variables.tf`. Required: `resource_group_name`, `region`,
`name_prefix`, `vnet_cidr`, `ssh_public_key`.

## Outputs

`vnet_id`, `vnet_name`, `subnet_{nodes,pods,lb,mew}_id`,
`private_dns_zone_postgres_{id,name}`.
