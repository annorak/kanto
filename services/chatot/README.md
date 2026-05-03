# Chatot — the Alerter

Chatot is the last stage of the v1 pipeline. It runs as a stateless
Deployment on OKE, consumes events from `kanto.scored`, filters those
whose novelty score exceeds the configured alert threshold, and
dispatches notifications through the configured channels — a Slack
webhook for routine alerts, PagerDuty for critical, an email digest
for batched routine traffic. The channels are wired up using the
Observer pattern: each channel registers as a subscriber, and adding a
new one (Teams, opsgenie, an analyst inbox queue) is a config change
plus a small adapter, never a change to the dispatcher itself. Chatot
also persists alert state to Mew so analysts can review, comment on,
and resolve alerts without re-deriving threshold decisions. Threshold
tuning is policy, not code: the score is a continuous number and
production thresholds are set against held-out positive and negative
sets to hit a target precision (~50% true positives) at the highest
achievable recall.

> **Status — placeholder.** Channel adapters, secret wiring, the Helm
> chart under `helm/`, and tests have not landed yet.
