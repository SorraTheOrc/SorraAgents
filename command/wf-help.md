---
description: Print a concise, markdown summary of the PRD-driven workflow
tags:
  - workflow
  - help
  - docs
agent: plan
---

This command emits a concise Markdown summary of docs/dev/Workflow.md focused on the four core workflow steps and the specific OpenCode commands used in each step. It is intended as a quick reference for contributors working with beads and PRDs.

Usage
- `/wf-help` — No arguments accepted. The command always prints the same concise markdown summary.

Behavior
- Locate and read docs/dev/Workflow.md in the repository root. Treat that file as the authoritative source.
- Produce a short Markdown document with the following sections:
  - One-line Purpose (introduction)
  - Prerequisites (bullet list)
  - High-level workflow summary (one sentence)
  - Four step sections (Project Definition, Define Milestones, Feature Decomposition, Implement Milestones/Epics). Each step has a 1–3 bullet summary and explicitly lists the commands used in that step (these commands are part of the step, not a separate "related commands" list).
- If docs/dev/Workflow.md is missing or unreadable, return a clear error message in plain text indicating the path and that the file is required.

Rationale
- Provides a stable, reviewable command that helps contributors quickly recall the repository workflow without opening the full doc.
- Uses `agent: plan` to match existing command agent conventions in this repo.

Output (Markdown)

The output below is the canonical summary produced by this command. Keep it concise and suitable for copy-paste into a comment or chat.

# PRD-Driven Workflow — Quick Reference

## Purpose
A lightweight summary of the repository's PRD-driven workflow for delivering features using human and agent collaborators.

## Prerequisites
- Repo access with permissions to create/edit files.
- A shared issue tracker (this repo uses `bd` / beads).
- An agreed PRD process (use the `/prd` command to create PRDs).
- A minimal quality bar for `main` (tests, feature-flag policy, review policy).

## High-level summary
Define a clear PRD, decompose work into milestones and features, then implement vertical end-to-end slices (code, tests, infra, docs, observability).

## Steps

### 1) Project Definition
- Define scope, success signals, constraints, and top risks in the PRD.
- Capture a one-paragraph project summary at the top of the PRD.
- Commands in this step: `/intake <Project Title>`, `/prd <Bead ID>`

  Note: `/prd` creates PRDs (epics/features) and follows assignment conventions: Epics → Build by default.

### 2) Define Milestones
- Map end-to-end user outcomes into master epic(s) and record owners and milestones (M0/M1).
- Ensure outcome maps and short feedback milestones are specified.
- Commands in this step: `/milestones <PRD Path>`

### 3) Feature Decomposition
- Break each epic into discrete features with acceptance criteria, minimal implementation, and prototypes/experiments where assumptions are risky.
- Create bd tasks for implementation, infra, docs, and tests; link to the PRD and epic.
- Commands in this step: `/plan <Epic ID>`

### 4) Implement Milestones / Epics
- Deliver vertical, end-to-end slices including code, tests, CI/deploy config, observability, and a rollback/feature-flag plan.
- Produce demo-ready slices deployable to staging.
- Commands in this step: `plan <Issue ID>`, `doc <Issue ID>`, `testplan <Issue ID>`, `implement <Issue ID>`

---

If the Workflow document changes, update this command so the summary remains accurate and minimal.
