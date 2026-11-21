---
name: fork-coding-conventions
description: Locates automatically injected coding conventions.
version: 1.0.0
author: Parsa Yazdani (@QuixThe2nd), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [delegation, coding-conventions]
    category: dev-lanes
---

# Fork Coding Conventions Skill

This compatibility skill points to coding conventions that delegated agents already receive automatically. It does not duplicate or replace the canonical source at `skills/dev-lanes/SOUL.md` in the Hermes checkout or packaged skill bundle.

## When to Use

Use this skill to locate the canonical conventions or diagnose automatic injection. For ordinary delegated coding work, loading this compatibility skill is not required.

## Prerequisites

A Hermes checkout or packaged skill bundle containing `skills/dev-lanes/SOUL.md`. No credentials, scripts, or additional tools are required to follow the pointer.

## How to Run

Continue with the conventions in the existing delegated context. Use `read_file` on `skills/dev-lanes/SOUL.md` only when the task requires reading or changing the conventions themselves.

## Quick Reference

- Canonical source: `skills/dev-lanes/SOUL.md`.
- Injection: automatic for OMP lanes, Claude, and native delegation.
- Compatibility skill: optional and pointer-only.

## Procedure

1. Use the conventions already present in the effective delegated context.
2. If they are absent, inspect startup injection rather than copying convention text into this skill.
3. Consult the canonical source when explicitly reviewing or updating the conventions.

## Pitfalls

- Do not duplicate the canonical convention sections in this file or another compatibility pointer.
- Selecting this skill must not add a second copy of the conventions.
- A present skill file does not prove that injection reached the delegated agent.

## Verification

Run `scripts/run_tests.sh tests/skills/test_fork_coding_conventions_skill.py -q` and `scripts/run_tests.sh tests/tools/test_delegate_conventions_startup.py -q`. The startup assertions must inspect each harness's effective initial system context with no skills and with this compatibility skill selected: the complete SOUL appears exactly once, while the task, additional context, and lane soul remain intact. Direct-native delegation without a skill must pass the same injection assertions; file existence alone is insufficient.
