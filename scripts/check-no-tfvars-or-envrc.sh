#!/usr/bin/env bash
# =============================================================================
# Block accidental commits of secret-bearing files.
#
# Refuses any staged path that:
#   - Ends in `.tfvars` or `.tfvars.json` (without a `.example` suffix), or
#   - Has the basename `.envrc` (without a `.example` suffix).
#
# Invoked by pre-commit with the staged file list as positional arguments.
# Exits non-zero if any disallowed path is present, with a clear message
# explaining how to fix.
# =============================================================================
set -euo pipefail

violations=()

for path in "$@"; do
    base="$(basename "$path")"

    case "$base" in
        *.tfvars|*.tfvars.json)
            violations+=("$path")
            ;;
        .envrc)
            violations+=("$path")
            ;;
    esac
done

if [[ ${#violations[@]} -gt 0 ]]; then
    echo "ERROR: refusing to commit secret-bearing files." >&2
    echo "" >&2
    echo "The following staged paths are blocked:" >&2
    for v in "${violations[@]}"; do
        echo "  - $v" >&2
    done
    echo "" >&2
    echo "Why: .tfvars and .envrc routinely contain credentials, OCIDs," >&2
    echo "private endpoints, or other sensitive material. Once committed," >&2
    echo "they enter git history permanently." >&2
    echo "" >&2
    echo "Fix:" >&2
    echo "  - For documentation, copy the file to <name>.example with all" >&2
    echo "    real values replaced by placeholders, and commit that instead." >&2
    echo "  - For local use, the .gitignore already excludes the real files;" >&2
    echo "    keep them on your machine but out of git." >&2
    echo "  - If you genuinely need to commit a tfvars file (e.g. a public" >&2
    echo "    region.tfvars.example), add the .example suffix." >&2
    exit 1
fi
