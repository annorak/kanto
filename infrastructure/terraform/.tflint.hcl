# tflint config — only the bundled `terraform` plugin.
#
# An OCI-specific tflint ruleset does not exist as of May 2026
# (terraform-linters issue #808 is open with no released plugin).
# The bundled terraform plugin covers the real linting value:
# deprecated arguments, unused declarations, naming conventions,
# required-version pinning. Provider-specific rules would be a nice
# add but aren't load-bearing for correctness.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
