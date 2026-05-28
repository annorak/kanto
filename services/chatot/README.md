# Chatot — the Alerter

> **Status — deferred from v1.** Chatot is not implemented in v1. The
> design below describes the intended v2 shape; until it ships, the
> `kanto.scored` topic accumulates events and the `alerts` table is
> populated only by ad-hoc scripts.

## Intended design (deferred)

Chatot is intended to be the last stage of the pipeline. It will run
as a stateless Deployment on AKS, consume events from `kanto.scored`,
filter those whose novelty score exceeds the configured alert
threshold, and dispatch notifications through the configured
channels — a Slack webhook for routine alerts, PagerDuty for
critical, an email digest for batched routine traffic. The channels
are wired up using the Observer pattern: each channel registers as a
subscriber, and adding a new one (Teams, opsgenie, an analyst inbox
queue) is a config change plus a small adapter, never a change to the
dispatcher itself. Chatot will also persist alert state to Mew so
analysts can review, comment on, and resolve alerts without
re-deriving threshold decisions. Threshold tuning is policy, not
code: the score is a continuous number and production thresholds are
set against held-out positive and negative sets to hit a target
precision (~50% true positives) at the highest achievable recall.
