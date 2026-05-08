# oke module

A managed OKE Enhanced Cluster with one CPU node pool. No GPU nodes — Modal
handles the GPU workload (Ditto).

Highlights:

- **Etcd-at-rest encryption** uses the env's KMS key.
- **VCN-Native CNI** with a dedicated pod subnet (Kubernetes NetworkPolicy
  enforcement comes via the OKE-managed `NetworkPolicies` add-on).
- **Public API endpoint** with NSG-based IP allowlist (from the network
  module's `operator_cidrs`).
- **Cluster autoscaler** is *not* installed by Terraform; node pool tags
  expose min/max so the autoscaler Helm chart (Task 5) can self-configure.
- Worker images come from `oci_containerengine_node_pool_option` so the
  pool always matches a known-good OKE image for the chosen K8s version.

## Inputs

See `variables.tf`. Notable required vars: `endpoint_subnet_id`,
`nodes_subnet_id`, `pods_subnet_id`, `lb_subnet_id`, `nsg_oke_api_id`,
`nsg_workers_id`, `kms_key_id`, `kubernetes_version`, `ssh_public_key`,
sizing (`node_ocpus`, `node_memory_gb`, `node_count`, `node_count_min`,
`node_count_max`).

## Outputs

`cluster_id`, `cluster_name`, `cluster_kubernetes_version`,
`cluster_endpoint`, `node_pool_id`.

## kubeconfig

Fetch with the OCI CLI after apply:

```bash
oci ce cluster create-kubeconfig \
  --cluster-id <cluster_id from outputs> \
  --file $HOME/.kube/config-kanto-<env> \
  --region us-sanjose-1 \
  --token-version 2.0.0
```

The CLI command is the canonical way; we don't expose the kubeconfig as a
Terraform output because that would write its bearer token into state.
