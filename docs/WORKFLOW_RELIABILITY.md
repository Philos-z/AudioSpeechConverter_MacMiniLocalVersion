# Workflow recovery and queue status

The controller preserves source bindings, factual checks and lifetime recovery budgets. A saved model approval remains subject to current local checks and later source-bound refusals.

## Local guard failures

For an `evidence_first_v1` job, a fully validated saved review that still fails local factual checks stops before a new recovery reservation, ASR pass or text request. The stop does not establish that the local check is wrong. A genuinely incorrect number, condition or negation remains blocked.

After an independently verified rule repair, the existing recovery entry can replay the same valid saved review without new text requests. Stale, incomplete, superseded or corrupt evidence cannot use this path. Existing partial transactions and all charged calls remain recorded.

Recovery diagnostics and the writer share the request identity contract. Supported legacy and current code identities are explicit; missing fields, extra identities and changed current code do not acquire a valid binding. Additional source-recheck evidence must retain its own identity checks.

## Daily queue status

`web/tagesschau_daily_tick.py` observes local job state and emits a deterministic plan. Its default plan mode does not change queue state. Reconcile mode writes only derived status under the existing queue lock, with both generation and whole-file SHA checks. Repeating the same observation does not create a new business transition.

Run from a configured installation with `python -B web/tagesschau_daily_tick.py --root . --mode plan`. The empty `config/tagesschau_daily_automation.example.json` documents the local configuration shape; account destinations and automation identities must be configured locally. This utility does not create a queue, an automation or a mail connection. A saved observation is accepted only by observe or plan mode; reconciliation collects a fresh local observation.

Historical notification records remain available. Current pending decisions derive from their actual decision records, so an old question does not keep a completed and notified job waiting. User holds, cancellation, frozen source identity, submission history and mail outbox records retain their original authority.

The tick has no executor for submissions, retries, model calls, service restarts or mail. Such operations continue through the existing authorized controller and connector paths. A plan is data and does not grant permission. Unknown submission or mail outcomes require reconciliation before another attempt.

Local observation uses the liveness and job GET endpoints. The health endpoint can probe the configured text provider and is not used for routine queue observation.

## Verification scope

Offline tests cover repeated failures, preserved budgets, changed evidence, later refusals, compare-and-swap conflicts, repeated reconciliation, held or canceled jobs, unverified completion and uncertain mail outcomes. These checks do not establish a new full-video performance result or human listening acceptance.
