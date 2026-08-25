# Governed AI Action convergence

Date: 2026-08-25

## Outcome

The “尝试” step now uses the same production `GovernedModelGateway` and configured
StepFun provider as candidate-insight generation. A user can only request an AI action
from the current confirmed or user-corrected Memory version. The model receives the
authoritative Memory statement plus its still-live Source evidence under a fresh
`PASSIVE_QA` consent snapshot.

The model may author only bounded copy and a 1–15 minute duration. The server owns
Vault identity, Source/Knowledge references, action kind, reversibility, initial state,
idempotency, model-run lineage, and all accept/complete/revoke transitions. Structured
validation rejects action instructions that require common external side effects.

## Durable boundaries

- `ModelRunInput` records both the live Source fragment and the current Knowledge
  `derived_object` using content-free HMAC fingerprints.
- `ModelRunArtifact` now supports an exactly-one-of Knowledge or Action pointer.
- `ReversibleAction.model_run_id` gives the Action an exact governed-run lineage.
- Action persistence and model success finalization occur in the same short transaction.
- Provider I/O occurs without an open request database transaction.
- Replaying the same HTTP idempotency key returns the same Action and ModelRun without
  dispatching the provider again.

## Verification evidence

- Backend suite: 1055 tests collected; 1053 passed and 2 skipped.
- Ruff and mypy passed.
- Desktop TypeScript typecheck and Vite production build passed.
- Disposable PostgreSQL staging rehearsal passed `upgrade -> downgrade -> upgrade`,
  followed by the non-owner runtime boundary test.
- Local non-owner development canary passed after applying migration
  `b8f4c2d1e706`.
- Real StepFun receipts observed:
  `reversible_action | stepfun-step-plan | succeeded | 2 inputs | action | lineage=true`.
- Browser canary completed:
  record a fragment -> generate candidate -> reveal exact evidence -> confirm -> generate
  AI action -> accept -> complete/revoke. The final reloaded UI shows `AI 生成` and the
  authoritative revoked history.

## Non-blocking follow-ups

1. Action generation is synchronous. Move it to the existing durable background-job
   pattern if latency/cancellation becomes a user problem; do not duplicate provider
   dispatch logic.
2. Add a first-party read-only ModelRun receipt endpoint if product diagnostics should
   expose run state without direct database access. It must remain content-free.
3. Expand action safety from the current structured schema plus bounded external-effect
   deny-list into a versioned policy evaluator with adversarial multilingual fixtures.
4. Capture a sanitized StepFun provider request identifier if the API exposes one in a
   stable technical field; never persist response bodies or credentials.
5. The current UI joins description and rationale into one paragraph. A later visual pass
   may separate “怎么做 / 为什么 / 如何退出” without changing backend behavior.

Memoir, life-line synthesis, and external Todo/Calendar remain intentionally out of scope.
