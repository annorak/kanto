output "namespace" {
  value = data.oci_objectstorage_namespace.tenancy.namespace
}

output "bucket_names" {
  description = "Map of role -> bucket name."
  value       = { for k, b in oci_objectstorage_bucket.this : k => b.name }
}

output "bucket_proteins" {
  value = oci_objectstorage_bucket.this["proteins"].name
}

output "bucket_embeddings" {
  value = oci_objectstorage_bucket.this["embeddings"].name
}

output "bucket_metadata" {
  value = oci_objectstorage_bucket.this["metadata"].name
}
