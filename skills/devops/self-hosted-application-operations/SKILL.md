---
name: self-hosted-application-operations
description: "Operate and publish services on existing infrastructure."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [devops, homelab, deployment, operations, self-hosted]
    category: devops
---

# Self-Hosted Application Operations

Runbook hub for operating services on infrastructure the user already owns:
deploys, upgrades, rollback, and publication all reuse the existing homelab —
this skill never provisions new cloud accounts, public repositories, or
tunnels unless the user explicitly asks for them.

## When to Use

- Deploying, updating, or rolling back a self-hosted service or static
  site/SPA on the user's existing infrastructure.
- Debugging a deployed service through its real ingress.

## Prerequisites

- The user's existing homelab deployment and ingress mechanisms (service
  definitions, reverse proxy, storage layout). Discover them before acting;
  do not invent new ones.

## How to Run

Read the reference matching the task and follow it top to bottom via
`terminal` (and `read_file` for inspecting configuration).

## Quick Reference

| Task | Reference |
|---|---|
| Static-site / SPA deploy, verify, rollback | `references/static-site-publication.md` |

## Procedure

1. Identify the existing host, service, and mechanism for the task.
2. Open the matching reference in `references/` and follow it in order.
3. Prefer immutable, versioned releases with the previous artifact preserved.
4. Verify through the real origin and user-facing route before reporting.

## Pitfalls

- Creating new cloud resources, public repos, or tunnels as a default —
  external hosting requires an explicit user request.
- Treating a successful upload as a successful deployment — run the
  reference's acceptance checks.
- Overwriting live output in place instead of switching to a new release.

## Verification

- The reference's acceptance checks pass (page content, assets, deep route,
  origin vs edge).
- The previous release is still intact and the rollback mechanism is known.
