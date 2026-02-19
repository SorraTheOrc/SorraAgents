# Workflow Descriptor Validation Rules

Formalized validation rules for workflow descriptors conforming to `workflow-schema.json`.
These rules go beyond JSON Schema structural validation to enforce semantic correctness
of the state machine, transitions, and AMPA delegation constraints.

## References

- `workflow-language.md` — Validation Rules section (6 base rules)
- `docs/workflow/workflow-schema.json` — JSON Schema (structural validation)
- `docs/workflow/workflow.yaml` — Canonical workflow descriptor
- `docs/workflow/engine-prd.md` — Engine execution semantics

---

## Rule Categories

1. **Structural rules** (V-S): Enforced by JSON Schema validation
2. **State machine rules** (V-SM): Semantic correctness of the state graph
3. **Invariant rules** (V-I): Correctness of invariant references and logic
4. **AMPA delegation rules** (V-D): Constraints specific to AMPA engine delegation
5. **Role rules** (V-R): Actor and role reference integrity

---

## Structural Rules (V-S)

These are enforced by `workflow-schema.json` and do not require additional tooling.

### V-S1: Required Top-Level Fields

| ID | V-S1 |
|---|---|
| **Rule** | The descriptor must contain all required top-level fields: `version`, `metadata`, `status`, `stage`, `invariants`, `commands` |
| **Source** | JSON Schema `required` |
| **Test** | Remove each required field in turn; schema validation must fail with a clear error naming the missing field |
| **Pass Criteria** | Schema validator reports `required property missing` |

### V-S2: Version Format

| ID | V-S2 |
|---|---|
| **Rule** | `version` must match `^\d+\.\d+\.\d+$` (semantic versioning) |
| **Source** | JSON Schema `pattern` |
| **Test Input** | `"1.0.0"` (pass), `"1.0"` (fail), `"v1.0.0"` (fail) |
| **Pass Criteria** | Only strings matching the pattern are accepted |

### V-S3: Non-Empty Status and Stage Arrays

| ID | V-S3 |
|---|---|
| **Rule** | `status` and `stage` arrays must each contain at least 1 unique string |
| **Source** | JSON Schema `minItems: 1, uniqueItems: true` |
| **Test Input** | `[]` (fail), `["open", "open"]` (fail), `["open"]` (pass) |
| **Pass Criteria** | Empty or duplicate arrays are rejected |

### V-S4: Command Structure

| ID | V-S4 |
|---|---|
| **Rule** | Every command must have `description` (string), `from` (array, minItems 1), `to` (StateRef), and `actor` (string) |
| **Source** | JSON Schema `Command` definition |
| **Test** | Remove each required field from a command; schema validation must fail |
| **Pass Criteria** | Schema validator reports the missing required property |

### V-S5: At Least One Command

| ID | V-S5 |
|---|---|
| **Rule** | The `commands` object must contain at least one command |
| **Source** | JSON Schema `minProperties: 1` |
| **Test Input** | `"commands": {}` (fail) |
| **Pass Criteria** | Empty commands object is rejected |

---

## State Machine Rules (V-SM)

These require programmatic validation beyond JSON Schema.

### V-SM1: State Tuple References Valid Status and Stage

| ID | V-SM1 |
|---|---|
| **Rule** | All `from`/`to` state tuples (both inline `{status, stage}` and resolved aliases from `states`) must use values declared in the top-level `status` and `stage` arrays |
| **Source** | workflow-language.md Rule 1 |
| **Input** | The full descriptor |
| **Algorithm** | 1. Collect all `status` values → `S`, all `stage` values → `G` 2. For each entry in `states`, verify `.status ∈ S` and `.stage ∈ G` 3. For each command, resolve all `from[]` and `to` entries: if string → look up in `states`; if object → check `.status ∈ S` and `.stage ∈ G` |
| **Pass Criteria** | No unresolved status or stage values |
| **Fail Example** | `to: { status: "archived", stage: "done" }` where `"archived"` is not in `status[]` |
| **Error Message** | `V-SM1: Command "{cmd}" references undeclared status "{val}". Declared statuses: [...]` |

### V-SM2: Command Has At Least One From State and Exactly One To State

| ID | V-SM2 |
|---|---|
| **Rule** | Every command must have `from` with ≥1 entry (array of StateRef) and `to` as exactly one StateRef |
| **Source** | workflow-language.md Rule 2 |
| **Input** | Each command definition |
| **Algorithm** | 1. Check `from` is an array with `length >= 1` 2. Check `to` is a single StateRef (string or object, not an array) |
| **Pass Criteria** | All commands satisfy both conditions |
| **Note** | JSON Schema enforces this structurally (V-S4), but the programmatic validator should also check post-resolution (alias lookup succeeds) |

### V-SM3: No Unreachable States

| ID | V-SM3 |
|---|---|
| **Rule** | Every non-initial state must be the `to` target of at least one command |
| **Source** | workflow-language.md Rule 4 |
| **Input** | The full descriptor |
| **Algorithm** | 1. Collect all resolved `to` states across all commands → `reachable_set` 2. Identify the initial state(s) — by convention, the first entry in `states` or states with the first `stage` value (e.g., `idea`) 3. For each state in `states` that is not initial, check that it appears in `reachable_set` |
| **Pass Criteria** | `states - initial_states - reachable_set == ∅` |
| **Fail Example** | A state `prd` exists in `states` but no command has `to: prd` |
| **Error Message** | `V-SM3: State "{alias}" ({status}/{stage}) is unreachable — no command transitions to it` |
| **Severity** | Warning (non-blocking) — states may be reachable via external mechanisms |

### V-SM4: No Dead-End States

| ID | V-SM4 |
|---|---|
| **Rule** | Every state that is not explicitly listed in `terminal_states` must appear in the `from` list of at least one command |
| **Source** | workflow-language.md Rule 5 |
| **Input** | The full descriptor |
| **Algorithm** | 1. Collect all resolved `from` states across all commands → `has_outbound` 2. Collect `terminal_states` (resolved) → `terminal_set` 3. For each state in `states`, if it is not in `terminal_set` and not in `has_outbound`, flag it |
| **Pass Criteria** | `non_terminal_states - has_outbound == ∅` |
| **Fail Example** | State `escalated` exists but no command has `from: [escalated, ...]` and it is not in `terminal_states` |
| **Error Message** | `V-SM4: State "{alias}" ({status}/{stage}) is a dead-end — no command transitions from it and it is not terminal` |
| **Severity** | Error (blocking) — dead-end states trap work items |

### V-SM5: Terminal States Are Declared

| ID | V-SM5 |
|---|---|
| **Rule** | Every entry in `terminal_states` must reference a valid state alias defined in `states` |
| **Source** | Implied by terminal_states semantics |
| **Input** | `terminal_states[]` and `states` |
| **Algorithm** | For each entry in `terminal_states`, verify it exists as a key in `states` |
| **Pass Criteria** | All terminal state references resolve |
| **Error Message** | `V-SM5: Terminal state "{name}" is not defined in states` |

### V-SM6: State Alias Uniqueness

| ID | V-SM6 |
|---|---|
| **Rule** | No two state aliases may resolve to the same `{status, stage}` tuple |
| **Source** | Implied — ambiguous aliases make transitions unpredictable |
| **Input** | `states` map |
| **Algorithm** | Group aliases by resolved `(status, stage)`. Flag groups with >1 alias |
| **Pass Criteria** | Each `(status, stage)` tuple maps to at most one alias |
| **Error Message** | `V-SM6: States "{a}" and "{b}" both resolve to {status}/{stage}` |
| **Severity** | Error |

---

## Invariant Rules (V-I)

### V-I1: Invariant References Exist

| ID | V-I1 |
|---|---|
| **Rule** | Every invariant name referenced in a command's `pre` or `post` array must match the `name` field of an entry in the top-level `invariants` array |
| **Source** | workflow-language.md Rule 3 |
| **Input** | All commands' `pre[]` and `post[]`, plus `invariants[].name` |
| **Algorithm** | 1. Collect `invariants[].name` → `declared` 2. For each command, for each name in `pre` and `post`, check `name ∈ declared` |
| **Pass Criteria** | No unresolved invariant references |
| **Error Message** | `V-I1: Command "{cmd}" references undeclared invariant "{name}". Declared invariants: [...]` |

### V-I2: Invariant Names Are Unique

| ID | V-I2 |
|---|---|
| **Rule** | No two invariants may share the same `name` |
| **Source** | Implied — duplicate names cause ambiguity |
| **Input** | `invariants[]` |
| **Algorithm** | Check for duplicate `name` values |
| **Pass Criteria** | All invariant names are unique |
| **Error Message** | `V-I2: Duplicate invariant name "{name}"` |

### V-I3: Invariant When-Phase Compatibility

| ID | V-I3 |
|---|---|
| **Rule** | If an invariant has `when: "pre"`, it should only appear in `pre[]` lists. If `when: "post"`, only in `post[]` lists. If `when: "both"` or `when: ["pre", "post"]`, it may appear in either |
| **Source** | Implied — using a pre-only invariant as a post-check is a configuration error |
| **Input** | All commands' `pre[]` and `post[]`, plus `invariants[].when` |
| **Algorithm** | For each invariant reference in a command, check that its declared `when` phase is compatible with its usage position |
| **Pass Criteria** | No phase mismatches |
| **Error Message** | `V-I3: Invariant "{name}" is declared as when="{when}" but used in {position} of command "{cmd}"` |
| **Severity** | Warning (non-blocking) — the engine may still evaluate it, but the intent is unclear |

---

## Role Rules (V-R)

### V-R1: Actor References Valid Role

| ID | V-R1 |
|---|---|
| **Rule** | Every command's `actor` value must match a role name declared in `metadata.roles` |
| **Source** | Implied by actor resolution (engine-prd.md §4) |
| **Input** | All commands' `actor` fields, plus `metadata.roles[]` |
| **Algorithm** | 1. Collect role names from `metadata.roles` (either string entries or `.name` from object entries) → `role_names` 2. For each command, check `actor ∈ role_names` |
| **Pass Criteria** | All actor references resolve to declared roles |
| **Error Message** | `V-R1: Command "{cmd}" references undeclared actor "{actor}". Declared roles: [...]` |

### V-R2: Roles Are Unique

| ID | V-R2 |
|---|---|
| **Rule** | No two role definitions may share the same name |
| **Source** | Implied |
| **Input** | `metadata.roles[]` |
| **Algorithm** | Collect all role names, check for duplicates |
| **Pass Criteria** | All role names are unique |
| **Error Message** | `V-R2: Duplicate role name "{name}"` |

---

## AMPA Delegation Rules (V-D)

These rules are specific to AMPA engine workflows and enforce constraints from `engine-prd.md`.

### V-D1: Delegation Command Requires Work Item Context Invariant

| ID | V-D1 |
|---|---|
| **Rule** | Any command with `to` resolving to the `delegated` state alias must include `requires_work_item_context` in its `pre` array |
| **Source** | engine-prd.md §5 — delegation requires sufficient context |
| **Input** | Commands where `to` resolves to `delegated` |
| **Algorithm** | For each command where resolved `to == states.delegated`: 1. If *all* `from` states have `status == "blocked"`, skip (this is a restoration from blocked state, not an initial delegation) 2. Otherwise, check `"requires_work_item_context" in pre` |
| **Pass Criteria** | All initial delegation commands require context (restoration commands are exempt) |
| **Error Message** | `V-D1: Command "{cmd}" transitions to delegated state but does not require "requires_work_item_context" pre-invariant` |
| **Exemptions** | Commands like `unblock_delegated` that restore from `blocked_delegated` → `delegated` are exempt because the delegation invariants were already checked during the initial `delegate` command |

### V-D2: Delegation Command Requires Acceptance Criteria Invariant

| ID | V-D2 |
|---|---|
| **Rule** | Any command with `to` resolving to the `delegated` state must include `requires_acceptance_criteria` in its `pre` array |
| **Source** | engine-prd.md §5 — audit cannot verify without AC |
| **Input** | Commands where `to` resolves to `delegated` |
| **Algorithm** | Same as V-D1 (with blocked-state exemption) but checking for `requires_acceptance_criteria` |
| **Pass Criteria** | All initial delegation commands require acceptance criteria |
| **Error Message** | `V-D2: Command "{cmd}" transitions to delegated state but does not require "requires_acceptance_criteria" pre-invariant` |

### V-D3: Delegation Command Requires Single-Concurrency Invariant

| ID | V-D3 |
|---|---|
| **Rule** | Any command with `to` resolving to the `delegated` state must include `no_in_progress_items` in its `pre` array |
| **Source** | engine-prd.md §5, scheduler.py single-concurrency constraint |
| **Input** | Commands where `to` resolves to `delegated` |
| **Algorithm** | Same as V-D1 (with blocked-state exemption) but checking for `no_in_progress_items` |
| **Pass Criteria** | All initial delegation commands enforce single concurrency |
| **Error Message** | `V-D3: Command "{cmd}" transitions to delegated state but does not require "no_in_progress_items" pre-invariant` |

### V-D4: Close-With-Audit Requires Audit Result Invariant

| ID | V-D4 |
|---|---|
| **Rule** | The `close_with_audit` command (or any command transitioning from `audit_passed`) must include `audit_recommends_closure` in its `pre` array |
| **Source** | engine-prd.md §5 — closure requires positive audit |
| **Input** | Command named `close_with_audit` or commands with `from` including `audit_passed` that transition toward closure |
| **Algorithm** | For the `close_with_audit` command, check `"audit_recommends_closure" in pre` |
| **Pass Criteria** | Audit-based closure requires positive audit recommendation |
| **Error Message** | `V-D4: Command "close_with_audit" does not require "audit_recommends_closure" pre-invariant` |

### V-D5: Audit Fail Requires Negative Audit Result

| ID | V-D5 |
|---|---|
| **Rule** | The `audit_fail` command must include `audit_does_not_recommend_closure` in its `pre` array |
| **Source** | engine-prd.md — audit failure requires confirmation of gaps |
| **Input** | Command named `audit_fail` |
| **Algorithm** | Check `"audit_does_not_recommend_closure" in pre` for the `audit_fail` command |
| **Pass Criteria** | Audit failure requires negative audit result |
| **Error Message** | `V-D5: Command "audit_fail" does not require "audit_does_not_recommend_closure" pre-invariant` |

### V-D6: Escalation Command Requires Reason Input

| ID | V-D6 |
|---|---|
| **Rule** | The `escalate` command must define a required `reason` input field |
| **Source** | workflow.yaml — escalation must be justified for Producer review |
| **Input** | Command named `escalate` |
| **Algorithm** | Check `escalate.inputs.reason` exists and has `required: true` |
| **Pass Criteria** | Escalation requires a documented reason |
| **Error Message** | `V-D6: Command "escalate" does not require a "reason" input` |

### V-D7: Delegation Command Actor Is PM

| ID | V-D7 |
|---|---|
| **Rule** | The `delegate` command must have `actor: PM` (the AMPA scheduler role) |
| **Source** | engine-prd.md — delegation is an AMPA engine action |
| **Input** | Command named `delegate` |
| **Algorithm** | Check `delegate.actor == "PM"` |
| **Pass Criteria** | Delegation is performed by the PM role |
| **Error Message** | `V-D7: Command "delegate" has actor "{actor}" but expected "PM"` |

---

## Summary Table

| ID | Category | Rule Summary | Source | Severity |
|---|---|---|---|---|
| V-S1 | Structural | Required top-level fields | JSON Schema | Error |
| V-S2 | Structural | Version format (semver) | JSON Schema | Error |
| V-S3 | Structural | Non-empty status/stage arrays | JSON Schema | Error |
| V-S4 | Structural | Command required fields | JSON Schema | Error |
| V-S5 | Structural | At least one command | JSON Schema | Error |
| V-SM1 | State Machine | State tuples reference valid status/stage | workflow-language.md | Error |
| V-SM2 | State Machine | Commands have ≥1 from, exactly 1 to | workflow-language.md | Error |
| V-SM3 | State Machine | No unreachable states | workflow-language.md | Warning |
| V-SM4 | State Machine | No dead-end states (unless terminal) | workflow-language.md | Error |
| V-SM5 | State Machine | Terminal states are declared | Implied | Error |
| V-SM6 | State Machine | State alias uniqueness | Implied | Error |
| V-I1 | Invariant | Invariant references exist | workflow-language.md | Error |
| V-I2 | Invariant | Invariant names are unique | Implied | Error |
| V-I3 | Invariant | When-phase compatibility | Implied | Warning |
| V-R1 | Role | Actor references valid role | engine-prd.md | Error |
| V-R2 | Role | Role names are unique | Implied | Error |
| V-D1 | Delegation | Delegation requires context invariant | engine-prd.md | Error |
| V-D2 | Delegation | Delegation requires AC invariant | engine-prd.md | Error |
| V-D3 | Delegation | Delegation requires concurrency invariant | engine-prd.md | Error |
| V-D4 | Delegation | Close-with-audit requires positive audit | engine-prd.md | Error |
| V-D5 | Delegation | Audit fail requires negative audit | engine-prd.md | Error |
| V-D6 | Delegation | Escalation requires reason input | workflow.yaml | Error |
| V-D7 | Delegation | Delegation actor is PM | engine-prd.md | Error |

---

## Validation Execution Order

Validators should execute in this order to produce the most useful error messages:

1. **JSON Schema validation** (V-S1 through V-S5) — fails fast on structural issues
2. **State resolution** — resolve all state aliases before checking transitions
3. **State machine rules** (V-SM1 through V-SM6) — check the state graph
4. **Invariant rules** (V-I1 through V-I3) — check invariant references
5. **Role rules** (V-R1, V-R2) — check actor references
6. **AMPA delegation rules** (V-D1 through V-D7) — check delegation-specific constraints

Errors at any level should be collected and reported together (do not fail on the first error).
Warnings should be reported but should not cause overall validation failure.
