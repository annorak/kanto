# network module

A single VCN with the four subnets Kanto needs:

| Subnet      | CIDR within /16 | Purpose                                          |
| ----------- | --------------- | ------------------------------------------------ |
| `public`    | `.0.0/24`       | OKE API endpoint, public load balancers          |
| `nodes`     | `.1.0/24`       | OKE worker node primary VNICs                    |
| `pods`      | `.16.0/20`      | OKE pod IPs (VCN-Native CNI)                     |
| `mew`       | `.32.0/28`      | OCI Database for PostgreSQL endpoint             |

Plus an internet gateway, NAT gateway, service gateway (for free Object Storage
traffic on the OCI backbone), and four network security groups: OKE API
endpoint, workers, public load balancer, Mew Postgres.

## Inputs

| Name                       | Type           | Required | Description                                                                                              |
| -------------------------- | -------------- | -------- | -------------------------------------------------------------------------------------------------------- |
| `compartment_id`           | `string`       | yes      | Compartment for all networking resources.                                                                |
| `name_prefix`              | `string`       | yes      | Resource name prefix, e.g. `kanto-dev`.                                                                  |
| `vcn_cidr`                 | `string`       | yes      | A `/16` block. Must not overlap any other Kanto environment.                                             |
| `operator_cidrs`           | `list(string)` | no       | CIDRs allowed to reach the OKE Kubernetes API on TCP/6443. Empty closes it down.                         |
| `mew_public_ingress_cidrs` | `list(string)` | no       | CIDRs allowed to reach the Mew Postgres endpoint on TCP/5432. Used to allowlist Modal in prod.           |
| `freeform_tags`            | `map(string)`  | no       | Tags applied to every resource.                                                                          |

## Outputs

`vcn_id`, `subnet_public_id`, `subnet_nodes_id`, `subnet_pods_id`,
`subnet_mew_id`, `nsg_oke_api_id`, `nsg_oke_workers_id`, `nsg_lb_id`,
`nsg_mew_id`.
