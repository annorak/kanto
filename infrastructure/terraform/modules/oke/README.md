# oke module

A managed OKE Enhanced Cluster with one CPU node pool. No GPU nodes — Modal
handles the GPU workload (Ditto).

Highlights:

- **Etcd-at-rest encryption** uses the env's KMS key. The cluster accesses
  the key via its own *resource principal* (ENHANCED_CLUSTER pattern); the
  required grant lives in the vault module's `kms_service_access` policy.
- **VCN-Native CNI** with a dedicated pod subnet. NetworkPolicy enforcement
  is built into the VCN-Native CNI itself — the `NetworkPolicies` addon is
  Flannel-only and OCI rejects installing it on a VCN-Native cluster.
- **Public API endpoint** with NSG-based IP allowlist (from the network
  module's `operator_cidrs`).
- **Cluster autoscaler** is *not* installed by Terraform; the autoscaler
  Helm chart in a later task is configured by node-pool OCID + min/max
  values passed as Helm values, not by node-pool tags.
- Worker images come from `oci_containerengine_node_pool_option` so the
  pool always matches a known-good OKE image for the chosen K8s version.
- **No `prevent_destroy` on the cluster.** If initial create fails (OCI
  marks the cluster `FAILED`), Terraform needs to taint+replace; the
  lifecycle block would block that. The cluster carries no local
  persistent state — workloads come from Helm in later tasks, and etcd is
  managed by OCI. Data-bearing protection sits on the Vault, KMS key,
  state bucket, and compartments.

## Inputs

See `variables.tf`. Notable required vars: `endpoint_subnet_id`,
`nodes_subnet_id`, `pods_subnet_id`, `lb_subnet_id`, `nsg_oke_api_id`,
`nsg_workers_id`, `kms_key_id`, `kubernetes_version`, `ssh_public_key`,
sizing (`node_ocpus`, `node_memory_gb`, `node_count`). Autoscaler min/max
live at the root level and feed the Task 5+ autoscaler chart directly.

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
