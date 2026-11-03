# Model requests and usage accounting

Provides the common connection between research stages and the model provider,
while retaining enough information to inspect what was requested and returned.

- [model_client.py](model_client.py) builds schema-constrained requests, loads the
  provider credential, sends calls, saves responses, and exposes token-count preflight.
- [model_ledger.py](model_ledger.py) records attempts, outcomes, validation metadata,
  and known token use for the browser's usage panel.

The default web transport uses Anthropic. Credentials come from the configured
private environment file or the `ANTHROPIC_API_KEY` environment variable; the latter
has precedence. Keep credentials and generated receipts out of source exports.

This package does not choose the next workflow stage. A failed or interrupted call
is not automatically replayed by restarting the server. Explicit stage recovery
owns any saved-response reuse or new attempt.

Missing usage remains **unknown**, not zero. Recorded tokens are not a complete
provider bill, and the ledger does not enforce a spending cap. Provider token
preflight for report requests is distinct from post-generation usage accounting.
