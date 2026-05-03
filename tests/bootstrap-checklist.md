# Bootstrap checklist

Manual verification that the repository bootstrap is correct. Run
these on a fresh clone in a clean directory. Tests for application
behaviour land alongside the application code in their respective
service directories; the goal here is to prove the repository-level
guardrails work as described.

Each test is independent and idempotent — re-running it on the same
clone is safe. None of these tests modify cloud resources.

---

## Test 1 — gitleaks blocks a fake secret on commit

Verifies the pre-commit gitleaks hook is installed, configured against
`.gitleaks.toml`, and refuses to let a credential-shaped string into
git history.

```bash
# Fresh clone in a scratch directory
git clone git@github.com:annorak/kanto.git /tmp/kanto-bootstrap-test
cd /tmp/kanto-bootstrap-test

# Set up tooling
uv sync --group dev
uv run pre-commit install

# Stage a fake Modal token (matches the kanto-modal-token-secret rule).
# Note: we deliberately use a non-".env" filename — the .gitignore would
# refuse to stage anything matching .env*, which is itself a working
# guardrail but not the one this test exercises.
echo 'modal_token_secret = "as-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"' > scratch-leak.txt
git add scratch-leak.txt

# Attempt the commit — expected to FAIL
git commit -m "test: this should be blocked"
```

**Expected outcome:**

- The commit is rejected with non-zero exit status.
- gitleaks output names the offending file (`scratch.env`) and the rule
  ID (`kanto-modal-token-secret`).
- No commit object is created (verify with `git log -1` — the head is
  unchanged).

Repeat with a fake OCI PEM block to exercise the
`kanto-oci-private-key` rule. Use a `.txt` extension so the .gitignore
doesn't pre-empt the test (the *.pem ignore rule would otherwise refuse
to stage the file at all):

```bash
cat > scratch-leak-pem.txt <<'EOF'
-----BEGIN RSA PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQ...
-----END RSA PRIVATE KEY-----
EOF
git add scratch-leak-pem.txt
git commit -m "test: this should also be blocked"
```

Both commits must fail. Cleanup:

```bash
git reset HEAD scratch-leak.txt scratch-leak-pem.txt
rm -f scratch-leak.txt scratch-leak-pem.txt
```

---

## Test 2 — `pre-commit run --all-files` passes on the clean repo

Verifies the entire repository as committed conforms to every hook
configured in `.pre-commit-config.yaml`.

```bash
cd /tmp/kanto-bootstrap-test
uv run pre-commit run --all-files
```

**Expected outcome:** exit status 0. Every hook reports `Passed` (or
`Skipped` when there are no relevant files — for example mypy when no
Python sources exist yet).

If `pre-commit run --all-files` modifies files (whitespace, EOL
fixers), re-stage and run again. A second run with no modifications
must pass cleanly.

---

## Test 3 — `uv sync` succeeds on the empty workspace

Verifies the root `pyproject.toml` is syntactically valid and the uv
workspace resolves with no members yet.

```bash
cd /tmp/kanto-bootstrap-test
rm -rf .venv uv.lock
uv sync --group dev
```

**Expected outcome:**

- `uv sync` completes with exit status 0.
- A `.venv/` is created with `pre-commit`, `ruff`, `mypy`, `pytest`,
  and `pytest-cov` installed.
- A `uv.lock` file is generated.
- `uv run python -c "import sys; print(sys.version)"` reports a 3.12
  interpreter.

---

## Test 4 — `.envrc` is ignored

Verifies the `.gitignore` excludes the real direnv file while keeping
the `.example` template tracked.

```bash
cd /tmp/kanto-bootstrap-test
cp .envrc.example .envrc
git status --porcelain
```

**Expected outcome:** `.envrc` does not appear in the porcelain
output. The status should be empty (or list only any changes you have
made for other reasons).

```bash
git check-ignore -v .envrc
```

**Expected outcome:** prints the line in `.gitignore` that excludes
`.envrc` (currently `.envrc`). Exit status 0 means the file is ignored.

```bash
git add -f .envrc 2>&1 || true
```

If forced, `-f` will track the file, demonstrating that *only* the
gitignore is keeping it out — the real protection is the gitleaks hook
plus the custom `block-tfvars-and-envrc` hook, both of which would
refuse the commit. Reset before continuing:

```bash
git reset HEAD .envrc
rm -f .envrc
```

---

## Test 5 — GitHub Actions workflows are syntactically valid

After pushing the bootstrap commit to GitHub, open the **Actions** tab
of `github.com/annorak/kanto` and verify:

- `ci` workflow appears in the workflow list.
- `secret-scan` workflow appears in the workflow list.
- The most recent run on `main` (or the bootstrap PR) shows both
  workflows triggered.
- The `lint`, `test (placeholder)`, and `gitleaks` jobs all complete
  with green checkmarks.

If a workflow fails to parse, GitHub displays a red banner at the top
of the Actions tab with a link to the syntax error. Fix and push again.

---

## Test 6 — `block-tfvars-and-envrc` hook fires

Verifies the custom local pre-commit hook catches the two file shapes
that most commonly leak credentials in practice.

```bash
cd /tmp/kanto-bootstrap-test
echo 'foo = "bar"' > terraform.tfvars
git add terraform.tfvars
git commit -m "test: should be blocked by block-tfvars-and-envrc"
```

**Expected outcome:** commit fails with the script's clear error
message naming `terraform.tfvars` and explaining the fix. Cleanup:

```bash
git reset HEAD terraform.tfvars
rm -f terraform.tfvars
```

The same test should pass for `terraform.tfvars.example` (the
`.example` suffix is allowed):

```bash
echo 'foo = "placeholder"' > terraform.tfvars.example
git add terraform.tfvars.example
git commit -m "chore: add tfvars example" --dry-run
```

---

## When all six tests pass

The bootstrap is correct. Open the PR, get review, and merge. Service
implementation work can then proceed against this substrate.
