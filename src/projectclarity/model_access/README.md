# Model requests and usage accounting

Provides the common connection between research stages and the model provider,
while retaining enough information to inspect what was requested and returned.

- [model_client.py](model_client.py) builds schema-constrained requests, loads the
  provider credential, sends calls, saves responses, and exposes token-count preflight.
- [model_outcomes.py](model_outcomes.py) distinguishes confirmed output truncation
  from other failures; only extraction uses it to schedule smaller source pieces.
- [research_budget.py](research_budget.py) reserves shared call/token allowance,
  persists usage and pauses requests for an explicit Continue or Quit decision.
- [model_ledger.py](model_ledger.py) records attempts, outcomes, validation metadata,
  and known token use for the browser's usage panel.

The default web transport uses Anthropic. Credentials come from the configured
private environment file or the `ANTHROPIC_API_KEY` environment variable; the latter
has precedence. Keep credentials and generated receipts out of source exports.

This package does not choose the next workflow stage. A failed or interrupted call
is not automatically replayed by restarting the server. Explicit stage recovery
owns any saved-response reuse or new attempt.

Missing usage remains **unknown**, not zero. Recorded tokens are not a complete
provider bill. Session-backed generation requests reserve provider-counted input
plus maximum output before dispatch. Input, output and cache counters are summed;
reasoning is already inside output. Unused reservations are released. A provider
count is an estimate, so actual usage can still differ; no new generation is
admitted after the allowance is spent. Token-count API calls are not generation
calls and do not consume the model-request allowance.

Defaults are **750 generation attempts / 8 million tokens per session**. Settings
apply to setup, rounds, repairs and retries together, not independently per stage.
Continue adds the original configured allowances (or enough token room for an
indivisible request, shown before approval). Duplicate decisions cannot grant
multiple extensions. Quit keeps completed artifacts and cancels pending calls.
Already dispatched calls finish and are accounted for.

`model-budget.json` persists counters separately from stage progress. Interrupted
reservations reconcile against the ledger on the next explicit start. Unknown
usage retains its reserved estimate and needs acknowledgement; older unknown
usage with no reservation stays explicitly unknown. Restart never grants more
allowance or replays work. Reads never create budget files. The admission lock is
process-local: run one server for a data directory. Standalone CLI experiments
without a session file are outside this web-session budget. None of this is a
monetary spending cap.
