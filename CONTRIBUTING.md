# Contributing to Kanto

Kanto is a multi-service, multi-cloud system. The set-up below is what
every engineer is expected to have running locally before opening a PR.

If you are setting up a fresh tenancy or workspace as well, work through
[`PREREQUISITES.md`](PREREQUISITES.md) first — the steps below assume
your OCI tenancy and Modal workspace already exist.

---

## 1. Local toolchain

Required:

- **git**
- **Python 3.12.x** — the workspace pins `>=3.12,<3.13`. Newer Python
  patch versions are fine; 3.13 is not yet supported.
- **[uv](https://docs.astral.sh/uv/)** — package and workspace manager.
  Install with `brew install uv` (macOS) or
  `curl -LsSf https://astral.sh/uv/install.sh | sh` (Linux).
- **[direnv](https://direnv.net/)** — loads per-repo environment
  variables. Install with `brew install direnv` (macOS) or your
  distribution package manager. Hook it into your shell as documented at
  <https://direnv.net/docs/hook.html>; the one-line snippet for zsh is
  `eval "$(direnv hook zsh)"` in `~/.zshrc`.
- **[gitleaks](https://github.com/gitleaks/gitleaks)** — secret scanner
  invoked by both pre-commit and CI. `brew install gitleaks`.
- **[pre-commit](https://pre-commit.com/)** — the dev dependency group
  installs it inside the workspace venv (`uv sync --group dev`).

Optional but recommended:

- **[gh](https://cli.github.com/)** — GitHub CLI, for opening PRs.
- **Docker / OrbStack** — needed once the local Postgres compose file
  lands.

---

## 2. First-time clone

```bash
git clone git@github.com:annorak/kanto.git
cd kanto

# 2a. Configure environment variables
cp .envrc.example .envrc
$EDITOR .envrc                 # set OCI compartment OCID, Mew password, etc.
direnv allow

# 2b. Set up Python toolchain
uv sync --group dev            # creates .venv/ with pre-commit, ruff, mypy, pytest

# 2c. Install pre-commit hooks (mandatory)
uv run pre-commit install
uv run pre-commit run --all-files   # one-time sanity check
```

After `direnv allow`, your shell will load `.envrc` whenever you enter
the repo. The `.envrc` file is gitignored — only the `.envrc.example`
template is tracked.

---

## 3. OCI configuration

Real OCI credentials never live in this repo. They live in
`~/.oci/config`, which you populate once with `oci setup config`. See
Oracle's official guide:
<https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm>

The config file references a private API key (`*.pem`). The `.gitignore`
blocks every common location for that key from being added to the repo,
and gitleaks blocks any private-key block in any file.

If you ever need to test OCI calls locally, edit `~/.oci/config`. Never
copy keys into the working tree.

---

## 4. Modal authentication

```bash
modal token new --profile kanto-dev
```

This opens a browser, performs OAuth, and writes the resulting token to
`~/.modal.toml`. The `.envrc` references the profile by name; the token
itself stays out of the repo.

For Kanto's runtime workloads, Modal needs OCI credentials and Mew
credentials. Those are uploaded as Modal Secrets (`modal secret create
oci-credentials …`). See `services/ditto/README.md` for the exact
secret schema once Ditto's Modal app lands.

---

## 5. Day-to-day commands

```bash
# Run linters and formatters on everything
uv run pre-commit run --all-files

# Run the test suite (empty until the first workspace package lands)
uv run pytest

# Add a new dependency to the dev group
uv add --group dev <package>

# Run a one-off script in the workspace venv
uv run python scripts/<script>.py
```

---

## 6. Branching and PR conventions

Kanto uses **trunk-based development** off `main`.

- Branch off `main`. Short, descriptive names: `feat/snorlax-prodigal`,
  `fix/alakazam-score-overflow`, `chore/upgrade-uv`.
- Keep PRs small and focused. One logical change per PR.
- Every PR requires:
  - Passing CI (`ci` and `secret-scan` workflows).
  - One review approval from another contributor.
  - The pre-commit hooks must have been run locally — CI re-runs them as
    a safety net.
- Do **not** force-push to `main`. Merge via GitHub's "squash and merge".
- Conventional commit prefixes (`feat:`, `fix:`, `chore:`, `docs:`,
  `refactor:`, `test:`) are used in commit messages and PR titles.

---

## 7. Adding a new package to the workspace

The high-level flow when introducing a new service or shared library:

1. Create the directory under `services/<name>/` or `libs/<name>/`.
2. Add a `pyproject.toml` declaring the package's name, dependencies,
   and any package-specific tool overrides.
3. Add the package path to the `members` list in the root
   `pyproject.toml` under `[tool.uv.workspace]`.
4. Run `uv sync` to materialise the new venv layout.
5. Add per-service tests under the package's `tests/` directory.
6. For OKE-deployed services, add the Helm chart under
   `services/<name>/helm/`. Ditto (on Modal) is the only exception.

The repo-level ruff, mypy, and pytest configurations apply by default;
each package can override them locally if it needs different rules.

---

## 8. Pre-commit hooks

The hooks in `.pre-commit-config.yaml` enforce:

- Trailing whitespace, EOL, large-file blocking (1 MB), merge conflict
  marker detection, private key detection.
- gitleaks against staged files (custom rules in `.gitleaks.toml`).
- ruff lint and format.
- mypy on changed Python files.
- A custom hook blocking any staged `*.tfvars` file or `.envrc` (without
  `.example` suffix).

If a hook fails, fix the underlying issue rather than bypassing it. Never
run `git commit --no-verify`. Do the same for `--no-gpg-sign` and any
other override flag.

CI re-runs `pre-commit run --all-files`, so a bypass at the local level
will fail the PR check anyway.
