# Snorlax — the Ingester and Gene Predictor

Snorlax is the heavy CPU stage of the pipeline. It runs as a StatefulSet
on OKE, one partition per pod with two replicas per partition, and
consumes events from `kanto.discovered`. For each new isolate, Snorlax
downloads the genome FASTA from NCBI's FTP into pod memory, validates
the file, runs Prodigal in the same pod (`prodigal -i genome.fna -a
proteins.faa -p single`, ~30 seconds wall time), gzips the protein
FASTA, writes it to OCI Object Storage at
`os://kanto-proteins/{accession}/{version}.faa.gz`, and then directly
invokes the Ditto Modal function with the OS key. Prodigal lives in the
same pod as the downloader on purpose — they share input data and
identical lifecycle requirements, so splitting them would only add
latency and a redundant queue. All operations are idempotent on
`(accession, version)`; isolates flagged in NCBI's Exceptions TSV are
recorded in Mew with `status = QC_FAILED` and never sent to Modal.

> **Status — placeholder.** Application code, Dockerfile, Prodigal
> binary packaging, the Helm chart under `helm/`, and tests have not
> landed yet.
