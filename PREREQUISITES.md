# Prerequisites

Manual cloud and account setup that must be completed before any Kanto
service can be deployed or run end-to-end against real infrastructure.
The repository bootstrap itself does not depend on any of this — you
can clone, set up direnv and uv, and run pre-commit hooks without any
cloud access.

The items below are operator responsibilities. Do not script them in
this repository.

---

## 1. Oracle Cloud Infrastructure (OCI)

### Tenancy

- A tenancy with the operator as a member of an admin group, or another
  group with sufficient privileges to create:
  - Compartments
  - VCNs and subnets
  - Object Storage buckets
  - OKE clusters and node pools
  - OCI Database for PostgreSQL instances
  - OCI Streaming streams and stream pools
  - OCI Vault and Vault secrets
  - IAM users, groups, dynamic groups, and policies

### API key

- Generate an API signing key pair (RSA 2048+).
- Upload the public half to your OCI user (Identity → Users → API Keys).
- Save the private half (`*.pem`) to `~/.oci/config`-referenced
  location, never inside this repo.
- Run `oci setup config` to write `~/.oci/config`. Confirm with
  `oci iam region list`.

Reference: Oracle's "Getting started with the Terraform Provider for
Oracle Cloud Infrastructure" walks through every step including IAM
policies, API key generation, and Terraform-friendly tenancy
configuration:
<https://docs.oracle.com/en-us/iaas/Content/dev/terraform/getting-started.htm>

### Region

- Home region: **us-sanjose-1**. All Terraform modules under
  `infrastructure/terraform/` default to this region.
- Confirm with `oci iam region-subscription list` and verify
  `us-sanjose-1` is in the subscribed list.

### Compartment

- Create a development compartment under the tenancy root for sandbox
  work. Suggested name: `kanto-dev`. Capture its OCID and put it in
  `.envrc` as `OCI_COMPARTMENT_ID` (it is not a secret).

---

## 2. Modal

### Account and workspace

- Create a Modal account at <https://modal.com>.
- Create a workspace named **`kanto-dev`** (the value `MODAL_PROFILE`
  in `.envrc.example` matches this). If you prefer a different name,
  adjust `.envrc` and document the change in your fork.
- Within the workspace, the default Modal environment is `main`. The
  `.envrc.example` uses `dev` for development; either is fine — pick one
  and stick to it.

### CLI authentication

```bash
pip install modal              # or `uv tool install modal`
modal token new --profile kanto-dev
```

This opens a browser, performs OAuth, and writes the token to
`~/.modal.toml`. The token is not stored in this repo.

### Modal Secrets (created later)

Once the OCI side is provisioned, two Modal Secrets will need to be
created. They are listed here for context — do not create them yet;
the documentation that covers the exact field names and rotation
policy lives next to the code that consumes them, in
`services/ditto/`.

- `oci-credentials` — OCI tenancy/user/region/key-fingerprint and the
  PEM private key, used by Ditto to read from / write to OCI Object
  Storage.
- `mew-credentials` — Mew's Postgres user, password, host, and CA cert,
  used by Ditto to write per-genome embeddings.

---

## 3. GitHub

### Repository

- A GitHub repo at `github.com/annorak/kanto` with the operator as
  admin (so they can configure repo secrets, Actions permissions, and
  branch protection).

### Branch protection (manual follow-up)

Once the bootstrap PR is merged, configure branch protection for `main`
under Settings → Branches:

- Require pull requests before merging.
- Require at least 1 approving review.
- Require status checks to pass: **`ci / lint (pre-commit)`**,
  **`ci / test (placeholder)`**, **`secret-scan / gitleaks`**.
- Require branches to be up to date before merging.
- Require linear history.
- Do not allow force-pushes or deletions on `main`.
- Restrict who can push to `main` to repository admins (with the policy
  that no one bypasses the PR flow).

These cannot be enforced from inside the repo; they are a one-time
console action.

### Repository secrets

Set these in Settings → Secrets and variables → Actions if and when
they are needed. None are required for the bootstrap workflows.

- `GITLEAKS_LICENSE` — only needed if the repo becomes private and
  you hit the gitleaks Action's free-tier rate limit.

### Actions permissions

- Settings → Actions → General → "Workflow permissions" → "Read
  repository contents and packages permissions" (read-only by default;
  individual workflows declare what they need).

---

## 4. Local machine

- macOS, Linux, or WSL2.
- `git` 2.40+, `python` 3.12.x, a POSIX shell.
- Tools listed in [`CONTRIBUTING.md`](CONTRIBUTING.md) §1: `uv`,
  `direnv`, `gitleaks`, `pre-commit` (via `uv sync --group dev`).

---

## 5. Verification

Before declaring the prerequisites complete, run:

```bash
oci iam region list                    # OCI auth works
modal token info --profile kanto-dev   # Modal auth works
gh repo view annorak/kanto             # GitHub auth works
uv --version                           # uv installed
direnv --version                       # direnv installed
gitleaks version                       # gitleaks installed
```

If any command fails, fix the corresponding prerequisite before moving
on to building any of the services.

---

## What to do if you can't complete a prerequisite

If a prerequisite is genuinely blocked (e.g. tenancy approval pending,
Modal account under review), create a `PREREQUISITES_NOT_MET.md` file at
the repo root listing the blockers and stop. Do not work around them by
hardcoding placeholder values into infrastructure code; downstream
deployment work assumes the listed prerequisites hold.
