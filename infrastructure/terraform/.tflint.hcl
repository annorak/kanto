plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "oci" {
  enabled = true
  version = "0.1.0"
  source  = "github.com/terraform-linters/tflint-ruleset-oci"
}
