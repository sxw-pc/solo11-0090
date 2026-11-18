# Compliance and acceptable use

> **Read this first.** pagedrift is designed exclusively for synthetic data
> sources and for sources you own or have explicit written authorization to
> test. It does not provide, and must not be used to build, any capability for
> bypassing anti-bot systems, impersonating identities, evading rate limits, or
> exerting pressure on a real target.

## Scope

- **Authorized / synthetic only.** The bundled data is generated from local
  seeds and placeholder identifiers such as `itm-0001`; placeholder hostnames use
  the `.invalid` convention. There are no real domains, company names, customer
  records, credentials, cookies or tokens in this repository.
- **Loopback only.** The mock source binds to `127.0.0.1`, `localhost` or `::1`.
  The HTTP client rejects any URL whose host is not loopback before it creates a
  socket. The HTTP server rejects non-loopback `Host` headers and non-loopback
  client addresses, records the event, and returns HTTP 403.
- **Offline.** No runtime code contacts external APIs, CDNs, cloud services or
  third-party telemetry. Tests must pass with networking disabled.
- **No stealth features.** There is no user-agent rotation, proxy rotation,
  CAPTCHA handling, browser fingerprint modification, login automation, or
  request pacing designed to defeat a target's controls.
- **No target pressure.** The project does not include distributed request
  generation, retry storms, concurrency attacks or traffic amplification.

## What this tool is for

- Reproducing pagination drift against a *local* mutable list.
- Proving that a collection missed, repeated, reordered or truncated records.
- Comparing pagination strategies before using the same analysis on data you are
  authorized to collect.
- Producing evidence that a collection stopped for a valid, declared reason.

## Operator responsibilities

1. Confirm you have the right to access and analyse the data source.
2. Keep the mock bound to loopback. Do not patch the guard out to point at a real
   service.
3. Treat the generated evidence, mutation logs and ledger as audit artefacts;
   do not edit them after a run.
4. Do not add credentials or personal data to plans, fixtures or golden files.
5. If adapting the project for a new environment, preserve the loopback guard,
   offline tests and deterministic fixtures.

## Explicit non-goals

pagedrift does not implement real-site collection, DOM parsing, browser
rendering, anti-bot countermeasures, credential handling, task queues, DAG
scheduling, resumable orchestration, link health checks, archival crawling,
subscription polling, form-filling RPA, fuzzy entity resolution or metrics
calculation.

The fictional election seat-settlement feature is a contract-algorithm demo
over invented `fictional-seven-rivers` data only. It is not an implementation
of any real jurisdiction's electoral system, and its vote totals, district
names, parties and majority conclusions must not be presented as real
political facts or used to predict or interpret an actual election.

This document is a scope statement, not legal advice. Obtain authorization and
legal guidance appropriate to your jurisdiction and the data source.
