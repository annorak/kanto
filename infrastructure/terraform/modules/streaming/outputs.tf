output "stream_pool_id" {
  value = oci_streaming_stream_pool.this.id
}

output "kafka_bootstrap_servers" {
  description = "Kafka-compatible bootstrap endpoint for clients."
  value       = oci_streaming_stream_pool.this.kafka_settings[0].bootstrap_servers
}

output "stream_ids" {
  description = "Map of stream name -> OCID."
  value       = { for k, s in oci_streaming_stream.this : k => s.id }
}
