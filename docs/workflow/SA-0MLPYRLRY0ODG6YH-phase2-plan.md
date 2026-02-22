Phase 2 Implementation Plan for SA-0MLPYRLRY0ODG6YH

This file records the Phase 2 plan produced by the implement skill. It breaks the work into smaller tracked work items and outlines first steps.

Child work items created:

- Implement AMPA engine core: candidate selection, context assembly, dispatch
- Audit integration & lifecycle updates: call audit skill and update work items
- Validation & tests: schema, state machine, integration tests
- Docs & examples: workflow descriptor, examples, PRD updates

Next steps

1. Implement the audit integration (critical path): call the `audit` skill on Patch completion and implement lifecycle transitions based on the audit result.
2. Implement engine core for candidate selection and dispatch.
3. Add validation tests for the workflow descriptor and state machine.
4. Update docs and provide examples for happy/failure audit paths.

Notes

- Branch: feature/SA-0MLPYRLRY0ODG6YH-phase2-plan
- This commit is local and will not be pushed without operator approval.
