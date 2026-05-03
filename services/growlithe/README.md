# Growlithe — the Discoverer

Growlithe is the entry point of the Kanto pipeline. It runs as a
single Deployment on OKE with a CronJob that fires every six hours: it
lists the NCBI Pathogen Detection FTP site, downloads the latest
metadata TSV per organism, diffs against the previous run's TSV (cached
in Object Storage), and writes one `IsolateDiscovered` event per new
PDT accession to the `kanto.discovered` OCI Streaming topic. NCBI does
not offer webhooks and updates at most daily, so polling is the right
shape here. Growlithe also keeps a "last seen accession version" cursor
in Mew so it survives restarts cleanly, and exposes a `DataSource`
adapter interface so future sources (ENA, GISAID, customer submissions)
can be added without touching downstream stages.

> **Status — placeholder.** Application code, Dockerfile, the Helm
> chart under `helm/`, and tests have not landed yet.
