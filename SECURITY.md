# Security policy

Kanto operates against public health data and is targeted at biosurveillance
analysts at agencies like CDC, FDA, USDA, and USAMRIID. Credential hygiene
and a working incident-response process are first-class concerns from day
one.

---

## Reporting a vulnerability

**Do not open a public GitHub issue for security reports.** Public issues
are indexed before maintainers see them; a credible exploit posted in the
clear is exposure, not a bug report.

Instead, email **security@kanto.dev** (alias forwards to the maintainer)
with:

- A description of the issue.
- Reproduction steps or proof-of-concept.
- Your assessment of severity and impact.
- Whether you'd like to be credited publicly when the issue is resolved.

Acknowledgement target: **2 business days**. Triage and remediation
target depend on severity (critical issues are resolved with priority
over feature work).

If you discover a credential that has been committed to this repository,
treat it as a security incident and follow the remediation steps below
*before* notifying anyone publicly.

---

## Secret-handling policy

The rules below apply to every contributor and every commit, with no
exceptions.

1. **Never commit credentials.** This includes API keys, OAuth tokens,
   private keys, passwords, connection strings with embedded passwords,
   and pre-authenticated request URLs. The repository's `.gitignore`,
   `.gitleaks.toml`, and pre-commit hooks are defense-in-depth, not the
   first line of defense — that is your eyes on the diff.

2. **Pre-commit hooks are mandatory.** After cloning, run
   `pre-commit install`. Do not bypass hooks with `--no-verify`. CI
   re-runs the same hooks against the full diff and will block the PR.

3. **Real credentials live outside the repo.** Allowed locations:
   - `~/.oci/config` (and the referenced PEM files) for OCI.
   - `~/.modal.toml` for Modal.
   - **OCI Vault** for runtime workload secrets consumed by services on
     OKE.
   - **Modal Secrets** for runtime workload secrets consumed by Ditto on
     Modal.
   The `.envrc.example` file documents which environment variables Kanto
   reads, but the values must be placeholders; the real `.envrc` is
   gitignored.

4. **`*.tfvars` files are blocked from being committed.** Terraform
   variable files frequently end up holding production OCIDs, region
   selectors, and the occasional credential. The custom pre-commit hook
   refuses any staged `*.tfvars` (without `.example` suffix). For
   documentation-grade defaults, commit a `terraform.tfvars.example`
   instead.

5. **Rotate immediately if a credential is committed.** Once a value is
   in git history, treat it as compromised — even if you push a
   force-push deletion seconds later, it is in clones, in the GitHub
   event log, and potentially in mirrors and backups. Always assume an
   adversary has it.

---

## Remediation: a credential was committed

The order of operations matters. **Rotate first, then scrub history.**

### 1. Rotate the credential

The exact steps depend on what was leaked.

- **OCI API key.** In the OCI console: Identity → Users → your user → API
  Keys → delete the key whose fingerprint matches the leaked PEM. Run
  `oci setup keys` to generate a new pair, upload the public key, and
  update `~/.oci/config`. Confirm with `oci iam region list`.
- **Modal token.** `modal token new --profile kanto-dev` issues a new
  token; the old one stops working. Then revoke the old one explicitly
  via `modal token revoke <token-id>` (or in the Modal dashboard).
- **Mew (Postgres) password.** `ALTER USER … WITH PASSWORD …`, update
  the OCI Vault entry, and roll the secret reference in any service that
  consumed it. For Modal-hosted Ditto: rotate the corresponding Modal
  Secret.
- **OCI Streaming credentials.** OCI manages Streaming auth via IAM —
  rotate the user's auth token in the console, then update the Vault
  entry consumed by the producing/consuming services.
- **Slack webhook URL** (if Chatot's webhook is leaked): regenerate from
  Slack, update the Vault entry.

Confirm the old credential no longer works before moving to step 2.

### 2. Notify

Tell the team in the ops channel. Notification before history scrubbing
is intentional — it prevents another contributor from racing the scrub
with their own work.

### 3. Scrub history

Use `git filter-repo` (preferred) or BFG Repo-Cleaner. `git
filter-branch` is deprecated and slow; do not use it.

```bash
# Recommended: git-filter-repo
pip install git-filter-repo

# Replace the leaked literal with a redaction marker. Adjust the path to
# the file the secret was in, or use --replace-text for a content match.
echo 'leaked-credential-value==>***REMOVED***' > /tmp/replacements.txt
git filter-repo --replace-text /tmp/replacements.txt --force

# Or, if the credential lived in a single file that should be removed
# entirely from history:
git filter-repo --path path/to/leaked/file --invert-paths --force
```

### 4. Force-push the scrubbed history

```bash
git push origin --force --all
git push origin --force --tags
```

Coordinate with anyone who has a clone — they will need to re-clone or
hard-reset their local copies. Stale clones still hold the credential.

### 5. Post-incident

- Confirm the rotated credential is the only one in active use (no
  service is still pinned to the old one via cached config).
- Add a regex for the leaked literal to `.gitleaks.toml` if its shape
  was not already covered.
- Write a short post-mortem and link it from the team's ops log so the
  next incident is faster.

---

## Out-of-scope reports

- Findings that depend on a compromised host or compromised maintainer
  account — these are not vulnerabilities in Kanto itself.
- Reports of "I found a public OCID" — OCIDs are public identifiers, not
  secrets. They are safe to commit.
- Denial-of-service via abuse of the public NCBI FTP. Take that to NCBI.
