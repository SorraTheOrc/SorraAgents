# Test Plan: Workflow Descriptor Validation and Engine Behavior

Test specifications for the workflow descriptor schema (`workflow-schema.json`),
the canonical workflow descriptor (`workflow.yaml`/`workflow.json`), and the AMPA
engine behavior described in `engine-prd.md`.

## References

- `docs/validation-rules.md` — Formalized validation rules (V-S, V-SM, V-I, V-R, V-D)
- `docs/workflow-schema.json` — JSON Schema
- `docs/workflow.yaml` / `docs/workflow.json` — Canonical workflow descriptor
- `docs/engine-prd.md` — Engine execution semantics
- `docs/examples/` — Delegation flow examples

---

## Test Categories

| Category | Prefix | Scope | Automation |
|---|---|---|---|
| Schema Validation | T-SV | JSON Schema against descriptors | CI-ready (jsonschema) |
| State Machine Validation | T-SM | Semantic graph analysis | CI-ready (Python script) |
| Invariant Validation | T-IV | Invariant reference integrity | CI-ready (Python script) |
| Role Validation | T-RV | Actor/role reference integrity | CI-ready (Python script) |
| Delegation Validation | T-DV | AMPA-specific constraints | CI-ready (Python script) |
| Canonical Descriptor | T-CD | Validate workflow.yaml/json | CI-ready (combined) |
| State Transition | T-ST | Command execution correctness | Specification only |
| Invariant Enforcement | T-IE | Pre/post invariant behavior | Specification only |
| Delegation Lifecycle | T-DL | End-to-end delegation flows | Specification only |
| Edge Cases | T-EC | Boundary and error scenarios | Specification only |

---

## 1. Schema Validation Tests (T-SV)

These tests validate that `workflow-schema.json` correctly accepts valid descriptors
and rejects invalid ones. Implemented via `jsonschema` library.

### T-SV-01: Valid Canonical Descriptor Passes

| Field | Value |
|---|---|
| **Input** | `docs/workflow.json` |
| **Schema** | `docs/workflow-schema.json` |
| **Expected** | Validation passes with no errors |
| **Validates** | V-S1 through V-S5 |

### T-SV-02: Missing Required Top-Level Field

| Field | Value |
|---|---|
| **Input** | Canonical descriptor with `commands` removed |
| **Expected** | Validation fails: `"'commands' is a required property"` |
| **Validates** | V-S1 |

### T-SV-03: Invalid Version Format

| Field | Value |
|---|---|
| **Input** | `{ "version": "v1.0" }` (rest valid) |
| **Expected** | Validation fails: pattern mismatch on `version` |
| **Validates** | V-S2 |

### T-SV-04: Empty Status Array

| Field | Value |
|---|---|
| **Input** | `{ "status": [] }` (rest valid) |
| **Expected** | Validation fails: `minItems` violation |
| **Validates** | V-S3 |

### T-SV-05: Duplicate Stage Values

| Field | Value |
|---|---|
| **Input** | `{ "stage": ["idea", "idea", "done"] }` |
| **Expected** | Validation fails: `uniqueItems` violation |
| **Validates** | V-S3 |

### T-SV-06: Command Missing Required Fields

| Field | Value |
|---|---|
| **Input** | Command with `description` but no `from`, `to`, or `actor` |
| **Expected** | Validation fails for each missing field |
| **Validates** | V-S4 |

### T-SV-07: Empty Commands Object

| Field | Value |
|---|---|
| **Input** | `{ "commands": {} }` |
| **Expected** | Validation fails: `minProperties` violation |
| **Validates** | V-S5 |

### T-SV-08: Additional Properties Rejected

| Field | Value |
|---|---|
| **Input** | Top-level `{ "foo": "bar" }` added to valid descriptor |
| **Expected** | Validation fails: `additionalProperties` violation |
| **Validates** | Schema strictness |

### T-SV-09: Invalid Invariant When Value

| Field | Value |
|---|---|
| **Input** | Invariant with `"when": "always"` |
| **Expected** | Validation fails: `when` must be `pre`, `post`, `both`, or array of `["pre","post"]` |
| **Validates** | V-S (invariant schema) |

### T-SV-10: Invalid Input Field Type

| Field | Value |
|---|---|
| **Input** | Input with `"type": "date"` |
| **Expected** | Validation fails: `type` must be one of `string`, `number`, `boolean`, `array`, `object` |
| **Validates** | V-S (InputField schema) |

---

## 2. State Machine Validation Tests (T-SM)

Semantic validation of the state graph. Implemented in `tests/validate_state_machine.py`.

### T-SM-01: All State Tuples Reference Valid Status/Stage

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | All states resolve to declared status and stage values |
| **Validates** | V-SM1 |
| **Algorithm** | For each alias in `states`, check `.status ∈ status[]` and `.stage ∈ stage[]`. For each command `from`/`to`, resolve alias → tuple, check both fields |

### T-SM-02: Undeclared Status in State Tuple

| Field | Value |
|---|---|
| **Input** | Descriptor with `states.bad: { status: "archived", stage: "done" }` where `archived` is not in `status[]` |
| **Expected** | Error: `V-SM1: State "bad" references undeclared status "archived"` |
| **Validates** | V-SM1 |

### T-SM-03: Undeclared Stage in Command To

| Field | Value |
|---|---|
| **Input** | Command with `to: { status: "open", stage: "nonexistent" }` |
| **Expected** | Error: `V-SM1: Command "..." references undeclared stage "nonexistent"` |
| **Validates** | V-SM1 |

### T-SM-04: No Unreachable States in Canonical Descriptor

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | All non-initial states are reachable (appear as `to` of some command) |
| **Validates** | V-SM3 |

### T-SM-05: Detect Unreachable State

| Field | Value |
|---|---|
| **Input** | Add state `orphan: { status: open, stage: idea }` with no command transitioning to it |
| **Expected** | Warning: `V-SM3: State "orphan" is unreachable` |
| **Validates** | V-SM3 |

### T-SM-06: No Dead-End States in Canonical Descriptor

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | All non-terminal states have at least one outbound command |
| **Validates** | V-SM4 |

### T-SM-07: Detect Dead-End State

| Field | Value |
|---|---|
| **Input** | Add state `stuck: { status: blocked, stage: in_progress }` with no command having it in `from` and not in `terminal_states` |
| **Expected** | Error: `V-SM4: State "stuck" is a dead-end` |
| **Validates** | V-SM4 |

### T-SM-08: Terminal States Are Declared

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | All entries in `terminal_states` are keys in `states` |
| **Validates** | V-SM5 |

### T-SM-09: Undeclared Terminal State

| Field | Value |
|---|---|
| **Input** | `terminal_states: ["nonexistent"]` |
| **Expected** | Error: `V-SM5: Terminal state "nonexistent" is not defined in states` |
| **Validates** | V-SM5 |

### T-SM-10: State Alias Uniqueness

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | No two aliases resolve to the same `(status, stage)` tuple |
| **Validates** | V-SM6 |

### T-SM-11: Duplicate State Alias Tuples

| Field | Value |
|---|---|
| **Input** | `states: { a: { status: open, stage: idea }, b: { status: open, stage: idea } }` |
| **Expected** | Error: `V-SM6: States "a" and "b" both resolve to open/idea` |
| **Validates** | V-SM6 |

---

## 3. Invariant Validation Tests (T-IV)

### T-IV-01: All Invariant References Resolve

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | All invariant names in `pre`/`post` arrays exist in `invariants[]` |
| **Validates** | V-I1 |

### T-IV-02: Undeclared Invariant Reference

| Field | Value |
|---|---|
| **Input** | Command with `pre: ["nonexistent_invariant"]` |
| **Expected** | Error: `V-I1: Command "..." references undeclared invariant "nonexistent_invariant"` |
| **Validates** | V-I1 |

### T-IV-03: Invariant Names Are Unique

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | No duplicate names in `invariants[]` |
| **Validates** | V-I2 |

### T-IV-04: When-Phase Compatibility

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | `pre`-only invariants not used in `post` lists, and vice versa |
| **Validates** | V-I3 |

### T-IV-05: Phase Mismatch Detection

| Field | Value |
|---|---|
| **Input** | Invariant with `when: "pre"` referenced in a command's `post` array |
| **Expected** | Warning: `V-I3: Invariant "..." is declared as when="pre" but used in post` |
| **Validates** | V-I3 |

---

## 4. Role Validation Tests (T-RV)

### T-RV-01: All Actor References Resolve

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | All command `actor` values match role names in `metadata.roles` |
| **Validates** | V-R1 |

### T-RV-02: Undeclared Actor

| Field | Value |
|---|---|
| **Input** | Command with `actor: "UnknownRole"` |
| **Expected** | Error: `V-R1: Command "..." references undeclared actor "UnknownRole"` |
| **Validates** | V-R1 |

### T-RV-03: Role Names Are Unique

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | No duplicate role names |
| **Validates** | V-R2 |

---

## 5. Delegation Validation Tests (T-DV)

### T-DV-01: Delegate Command Has Required Pre-Invariants

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | `delegate` command includes `requires_work_item_context`, `requires_acceptance_criteria`, and `no_in_progress_items` in `pre` |
| **Validates** | V-D1, V-D2, V-D3 |

### T-DV-02: Missing Delegation Pre-Invariant

| Field | Value |
|---|---|
| **Input** | `delegate` command with `requires_work_item_context` removed from `pre` |
| **Expected** | Error: `V-D1: Command "delegate" does not require "requires_work_item_context"` |
| **Validates** | V-D1 |

### T-DV-03: Close-With-Audit Requires Positive Audit

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | `close_with_audit` command includes `audit_recommends_closure` in `pre` |
| **Validates** | V-D4 |

### T-DV-04: Audit Fail Requires Negative Audit

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | `audit_fail` command includes `audit_does_not_recommend_closure` in `pre` |
| **Validates** | V-D5 |

### T-DV-05: Escalate Requires Reason Input

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | `escalate` command has `inputs.reason` with `required: true` |
| **Validates** | V-D6 |

### T-DV-06: Delegate Actor Is PM

| Field | Value |
|---|---|
| **Input** | Canonical descriptor |
| **Expected** | `delegate.actor == "PM"` |
| **Validates** | V-D7 |

---

## 6. Canonical Descriptor Tests (T-CD)

Integration tests that run all validators against the canonical descriptor.

### T-CD-01: workflow.json Passes JSON Schema

| Field | Value |
|---|---|
| **Input** | `docs/workflow.json` validated against `docs/workflow-schema.json` |
| **Expected** | No schema errors |
| **CI** | `tests/validate_schema.py` |

### T-CD-02: workflow.json Passes State Machine Validation

| Field | Value |
|---|---|
| **Input** | `docs/workflow.json` |
| **Expected** | No V-SM errors or warnings |
| **CI** | `tests/validate_state_machine.py` |

### T-CD-03: workflow.json Passes Invariant Validation

| Field | Value |
|---|---|
| **Input** | `docs/workflow.json` |
| **Expected** | No V-I errors |
| **CI** | `tests/validate_state_machine.py` (combined) |

### T-CD-04: workflow.json Passes Role Validation

| Field | Value |
|---|---|
| **Input** | `docs/workflow.json` |
| **Expected** | No V-R errors |
| **CI** | `tests/validate_state_machine.py` (combined) |

### T-CD-05: workflow.json Passes Delegation Validation

| Field | Value |
|---|---|
| **Input** | `docs/workflow.json` |
| **Expected** | No V-D errors |
| **CI** | `tests/validate_state_machine.py` (combined) |

### T-CD-06: workflow.yaml and workflow.json Are Equivalent

| Field | Value |
|---|---|
| **Input** | Both files loaded and compared after normalization |
| **Expected** | Identical structure (YAML is the authored source, JSON is generated) |
| **CI** | `tests/validate_schema.py` (optional equivalence check) |

---

## 7. State Transition Tests (T-ST)

Specification-only — describe expected behavior for engine implementation testing.

### T-ST-01: Happy Path — Full Lifecycle

| Field | Value |
|---|---|
| **Initial State** | `idea` (open/idea) |
| **Commands** | intake → author_prd → plan → delegate → complete_work → submit_review → audit_result → close_with_audit → approve |
| **Expected States** | idea → intake → prd → plan → delegated → building → review → audit_passed → completed/in_review → shipped |
| **Reference** | `docs/examples/01-happy-path.md` |

### T-ST-02: Audit Failure and Retry

| Field | Value |
|---|---|
| **Initial State** | `review` (in_progress/in_review) |
| **Commands** | audit_fail → retry_delegation → delegate → complete_work → submit_review → audit_result |
| **Expected States** | review → audit_failed → plan → delegated → building → review → audit_passed |
| **Reference** | `docs/examples/02-audit-failure.md` |

### T-ST-03: Blocked and Unblocked

| Field | Value |
|---|---|
| **Initial State** | `delegated` (in_progress/delegated) |
| **Commands** | block → unblock |
| **Expected States** | delegated → blocked_delegated → delegated |
| **Reference** | `docs/examples/03-blocked-flow.md` |

### T-ST-04: Escalation Flow

| Field | Value |
|---|---|
| **Initial State** | `audit_failed` (in_progress/audit_failed) |
| **Commands** | escalate → de_escalate → delegate |
| **Expected States** | audit_failed → escalated → plan → delegated |
| **Reference** | `docs/examples/06-escalation.md` |

### T-ST-05: Manual Build Path (No Delegation)

| Field | Value |
|---|---|
| **Initial State** | `plan` (open/plan_complete) |
| **Commands** | start_build → submit_review → approve |
| **Expected States** | plan → building → review → shipped |
| **Note** | Tests the non-AMPA path through the workflow |

### T-ST-06: Invalid Transition Rejected

| Field | Value |
|---|---|
| **Initial State** | `idea` (open/idea) |
| **Command** | `delegate` |
| **Expected** | Rejected — `idea` is not in `delegate.from[]` |
| **Error** | `Command "delegate" cannot be executed from state "idea" (open/idea). Allowed from: [plan]` |

### T-ST-07: Reopen From Shipped

| Field | Value |
|---|---|
| **Initial State** | `shipped` (closed/done) |
| **Command** | `reopen` |
| **Expected State** | `plan` (open/plan_complete) |
| **Note** | Tests transition from terminal state via explicit reopen command |

---

## 8. Invariant Enforcement Tests (T-IE)

Specification-only — describe pre/post invariant behavior.

### T-IE-01: Pre-Invariant Failure Blocks Command

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **State** | `plan` |
| **Setup** | Work item description is empty (length < 100) |
| **Expected** | Command rejected: `requires_work_item_context` fails |
| **Engine Behavior** | No state transition occurs, error logged |

### T-IE-02: Pre-Invariant — Do Not Delegate Tag

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **State** | `plan` |
| **Setup** | Work item tagged `do-not-delegate` |
| **Expected** | Command rejected: `not_do_not_delegate` fails |

### T-IE-03: Pre-Invariant — Single Concurrency

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **State** | `plan` |
| **Setup** | Another work item is in `in_progress` status |
| **Expected** | Command rejected: `no_in_progress_items` fails |
| **Reference** | `docs/examples/05-work-in-progress.md` |

### T-IE-04: Pre-Invariant — No Acceptance Criteria

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **State** | `plan` |
| **Setup** | Work item description has no AC section or checkbox list |
| **Expected** | Command rejected: `requires_acceptance_criteria` fails |

### T-IE-05: Pre-Invariant — Audit Recommends Closure

| Field | Value |
|---|---|
| **Command** | `close_with_audit` |
| **State** | `audit_passed` |
| **Setup** | No audit comment with "Can this item be closed? Yes" |
| **Expected** | Command rejected: `audit_recommends_closure` fails |

### T-IE-06: Multiple Pre-Invariant Failures

| Field | Value |
|---|---|
| **Command** | `delegate` |
| **State** | `plan` |
| **Setup** | Empty description, no AC, another item in progress |
| **Expected** | All 3 invariant failures reported (not just the first) |
| **Engine Behavior** | Command rejected, all failures collected and reported |

### T-IE-07: Post-Invariant — Requires Approvals

| Field | Value |
|---|---|
| **Command** | `approve` |
| **State** | `completed/in_review` |
| **Setup** | No "Approved by" comment on the work item |
| **Expected** | Post-invariant `requires_approvals` fails after transition |
| **Engine Behavior** | Transition rolled back, error logged |

---

## 9. Delegation Lifecycle Tests (T-DL)

Specification-only — end-to-end scenarios testing AMPA engine behavior.

### T-DL-01: Full Delegation — Happy Path

| Field | Value |
|---|---|
| **Scenario** | AMPA selects item via `wl next`, delegates, Patch implements, audit passes, closure |
| **Input** | Work item with AC, sufficient description, plan_complete stage |
| **Expected Flow** | select → delegate → complete_work → submit_review → audit_result → close_with_audit → approve |
| **Verification** | Final state is `shipped`, all audit comments present, Discord notifications sent |
| **Reference** | `docs/examples/01-happy-path.md` |

### T-DL-02: Delegation — Audit Failure with Retry

| Field | Value |
|---|---|
| **Scenario** | Audit finds gaps, engine retries, second audit passes |
| **Expected Flow** | delegate → ... → audit_fail → retry_delegation → delegate → ... → audit_result → close |
| **Verification** | Two audit comments present, `audit_failed` tag removed after retry |
| **Reference** | `docs/examples/02-audit-failure.md` |

### T-DL-03: Delegation — Escalation

| Field | Value |
|---|---|
| **Scenario** | Two audit failures trigger escalation to Producer |
| **Expected Flow** | delegate → ... → audit_fail → retry → delegate → ... → audit_fail → escalate |
| **Verification** | Status is `blocked`, stage is `escalated`, assignee is `Producer`, Discord notification sent |
| **Reference** | `docs/examples/06-escalation.md` |

### T-DL-04: Delegation — Blocked During Implementation

| Field | Value |
|---|---|
| **Scenario** | Patch encounters a blocker during implementation |
| **Expected Flow** | delegate → block → unblock → complete_work → submit_review |
| **Verification** | Block comment recorded, status transitions through blocked back to in_progress |
| **Reference** | `docs/examples/03-blocked-flow.md` |

### T-DL-05: Delegation — No Candidates

| Field | Value |
|---|---|
| **Scenario** | Scheduler runs but `wl next` returns no candidates |
| **Expected** | No delegation occurs, idle state logged, Discord notification sent |
| **Reference** | `docs/examples/04-no-candidates.md` |

### T-DL-06: Delegation — Concurrent Work In Progress

| Field | Value |
|---|---|
| **Scenario** | Scheduler runs but another item is already in progress |
| **Expected** | `no_in_progress_items` invariant fails, delegation skipped |
| **Reference** | `docs/examples/05-work-in-progress.md` |

---

## 10. Edge Case Tests (T-EC)

### T-EC-01: Reopen After Closure

| Field | Value |
|---|---|
| **Scenario** | Work item is shipped, then reopened |
| **Input** | Item in `shipped` state, execute `reopen` |
| **Expected** | State transitions to `plan`, work item can be re-delegated |

### T-EC-02: Block From Multiple States

| Field | Value |
|---|---|
| **Scenario** | `block` command executed from `delegated` vs `building` |
| **Expected** | Both transitions work: `delegated` → `blocked_delegated`, `building` → `blocked_in_progress` |

### T-EC-03: Double Delegation Attempt

| Field | Value |
|---|---|
| **Scenario** | `delegate` command executed while another item is in `delegated` state |
| **Expected** | Rejected by `no_in_progress_items` invariant (delegated status = in_progress) |

### T-EC-04: Escalation From Delegated State

| Field | Value |
|---|---|
| **Scenario** | `escalate` executed directly from `delegated` (critical issue found) |
| **Expected** | Allowed — `delegated` is in `escalate.from[]` per workflow.yaml |
| **Note** | This is an emergency path, not the normal audit-failure escalation |

### T-EC-05: De-Escalate Without Producer Guidance

| Field | Value |
|---|---|
| **Scenario** | `de_escalate` executed but no Producer comment exists |
| **Expected** | Command succeeds (no invariant requires Producer comment on de_escalate). The Producer is the actor executing the command, which is sufficient |
| **Note** | Consider whether a post-invariant should be added to require guidance documentation |

### T-EC-06: Audit Result on Non-Review State

| Field | Value |
|---|---|
| **Scenario** | `audit_result` command attempted from `delegated` state |
| **Expected** | Rejected — `delegated` is not in `audit_result.from[]` (only `review` is allowed) |

### T-EC-07: Empty Workflow Descriptor

| Field | Value |
|---|---|
| **Scenario** | Minimal valid descriptor (1 status, 1 stage, 1 state, 1 command, 1 invariant, 1 role) |
| **Expected** | Passes all validation rules |
| **Purpose** | Verify validators handle minimal inputs correctly |

### T-EC-08: Large Workflow Descriptor

| Field | Value |
|---|---|
| **Scenario** | Descriptor with 50+ commands, 20+ states, 30+ invariants |
| **Expected** | Validation completes in < 1 second |
| **Purpose** | Performance regression test |

---

## CI Integration

### Test Scripts

| Script | Purpose | Validates |
|---|---|---|
| `tests/validate_schema.py` | JSON Schema validation of workflow.json | T-SV, T-CD-01 |
| `tests/validate_state_machine.py` | Semantic validation (state graph, invariants, roles, delegation) | T-SM, T-IV, T-RV, T-DV, T-CD-02 through T-CD-05 |

### Running Tests

```bash
# Schema validation only
python tests/validate_schema.py

# Full semantic validation
python tests/validate_state_machine.py

# Both (CI)
python tests/validate_schema.py && python tests/validate_state_machine.py
```

### Exit Codes

| Code | Meaning |
|---|---|
| 0 | All checks passed (warnings may be present) |
| 1 | One or more errors found |
| 2 | File not found or invalid input |

### CI Pipeline Integration

```yaml
# Example GitHub Actions step
- name: Validate workflow descriptor
  run: |
    pip install jsonschema pyyaml
    python tests/validate_schema.py
    python tests/validate_state_machine.py
```
