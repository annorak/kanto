# aks module

A single AKS cluster with one CPU node pool. No GPU pool — Modal handles
the GPU workload (Ditto).

Highlights:

- **Azure CNI** with a dedicated pod subnet. NetworkPolicy enforcement is
  built into Azure CNI (`network_policy = "azure"`) — no separate add-on.
- **Workload Identity Federation** enabled. K8s service accounts get
  annotations pointing at the AKS-workloads user-assigned managed identity
  (`iam` module). Pods then fetch AAD tokens via the projected service
  account token without any secret material in the cluster.
- **Public API endpoint** with NSG-equivalent IP allowlist via the
  `api_server_access_profile.authorized_ip_ranges` attribute. Forwarded
  from root `operator_cidrs`.
- **OMS agent + diagnostic settings** stream container-insights, kube-
  apiserver, kube-audit-admin, controller-manager, scheduler, and
  cluster-autoscaler logs to the Log Analytics workspace.
- **No `prevent_destroy` on the cluster.** Initial create can fail (region
  capacity, quota); `prevent_destroy` would block taint+replace. The
  cluster has no local persistent state — workloads come from Helm in
  later tasks, etcd is managed by AKS.
- **Etcd encryption with a customer-managed Key Vault key:** currently
  *omitted from initial create* because of a chicken-and-egg between the
  cluster's system-assigned identity and the required Key Vault Crypto
  User role assignment. Etcd is encrypted with platform-managed keys
  meanwhile (FIPS 140-2 validated). To switch to CMK, run an
  `azapi_update_resource` against the cluster post-create. See main.tf
  comments.

## Inputs

See `variables.tf`. Notable required vars: `nodes_subnet_id`,
`pods_subnet_id`, `key_vault_id`, `key_vault_etcd_key_id`,
`workload_identity_id`, `log_analytics_workspace_id`, `kubernetes_version`,
`ssh_public_key`, sizing (`node_vm_size`, `node_count`).

## Outputs

`cluster_id`, `cluster_name`, `cluster_kubernetes_version`,
`oidc_issuer_url` (used when wiring K8s ServiceAccount → UAMI federation
in later tasks), `kubelet_identity_object_id`.

## kubeconfig

```bash
az aks get-credentials \
  --resource-group <rg> \
  --name $(terraform output -raw aks_cluster_name) \
  --file $HOME/.kube/kanto-<env>
```

We don't expose kubeconfig as a Terraform output — that would write its
bearer token into state.
