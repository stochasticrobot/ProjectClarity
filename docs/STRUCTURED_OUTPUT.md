# Structured output: representations are not evidence

## Falsifiable acceptance

Anthropic and Nova must reach the same research-stage schemas and evidence checks
through registered provider adapters and explicit codecs. A new text/JSON provider
must not require provider branches in research stages. Malformed, incomplete or
unsupported output must retain its receipt and fail the appropriate boundary;
format adaptation must not invent entities, quotes, facts or required fields.

The extension contract is exercised by a third fixture provider, not an Ollama
implementation. Support for a new modality would deliberately extend the contract.

## Three boundaries

1. **Provider envelope:** adapters translate requests and recognize transport
   completion, refusals and truncation. They preserve native responses and usage.
2. **Declared representation:** shared response codecs convert a known wire
   format to canonical JSON. Anthropic uses identity decoding. Nova defaults to
   JSON text with an explicit `{` prefill. Only that exact prefix is restored;
   duplicate keys, non-finite values, Markdown fences, trailing prose and broken
   JSON fail. Experimental tool mode uses reversible nullable zero/one lists.
3. **Domain validation:** canonical Pydantic schemas and source/reference checks
   decide whether returned records are usable. A decoded response is not validated
   evidence. `ModelResponse.completed_text()` applies the same completion and
   structure gate to web calls and standalone tools.

Schema instructions and temperature zero guide Nova but do not guarantee correct
output. No implicit fallback, extra provider retry, field default insertion or
string-to-null guessing is introduced. Existing bounded domain repair remains
explicit and accounted for.

### Entity catalogues versus additions

After strict `LinkReview` schema validation, the omission-review boundary may
remove an entity entry **exactly equal** to an already-validated entity. This
supports models returning the known catalogue instead of only additions. Each
removal records its path, ID and matched original; the raw review is preserved.
Redefined entities and repeated IDs still fail. New entities and link decisions
are not changed. All existing reference and quotation checks run afterward.
This is a deliberate domain-boundary behavior change, not weaker schema parsing.

### Accounting and cache identity

Receipts distinguish format, conversions, structure failures and validation
outcomes. A malformed reply still settles actual provider token usage. Nova's
pre-request count remains an explicitly labelled conservative estimate, not a
proven upper bound. Anthropic retains provider counting.

Registry contract versions and declared output-affecting settings produce a
fingerprint pinned by `ModelCalls` and required for report/triage cache reuse.
Changing Nova output mode or region fails before dispatch until the caller is
restarted. Credentials are excluded so expired keys can be refreshed. Adapters
must register future output-affecting settings and bump contract versions when
wire interpretation changes.

## Evidence and limits

The final suite ran **211 tests: 208 passed, 3 skipped**. Offline HTTP full-workflow
checks exercise both Nova modes through production routing, replacing only
external services. Frozen malformed replies, exact catalogue echoes, changed
entities, duplicate IDs, missing fields and unknown references have regressions.

Live checks invoke `concurrent_extraction.extract_chunk`, the production worker,
with real `ModelCalls`, validators, bounded repair and omission review. The
implementation was frozen before the three held-out documents were run:

| Population | Initial schema-valid extraction | Full worker accepted |
|---|---:|---:|
| Original development document | 1/1 | 1/1 |
| Held-out synthetic documents | 3/3 | 2/3 |

Each initial extraction retained two facts. All eight replies decoded, but one
held-out omission review referenced entity ID `1998`, absent from its catalogue.
Existing checks rejected the unsupported link, leaving that worker partial.
This is the strongest remaining counterevidence to broad reliability: valid
structure does not imply valid references or sound semantic modelling.

Earlier nullable-tool trials yielded missing fields or an empty result. They
remain recorded as failures, not selected away. This small synthetic population
proves only the observed routing/representation behavior; it is not a full live
research/report benchmark, model-quality comparison or deployment proof. No
running application was switched, restarted or remotely deployed.

See [the machine-readable evidence ledger](structured-output-verification.json)
and [the earlier historical failures](bedrock-live-verification.json).

## Reproduce bounded checks

Preview (no provider calls):

```bash
.venv/bin/python -m testing.provider_structure_check --cases heldout
```

Explicit live run using the existing private starter configuration:

```bash
.venv/bin/python -m testing.provider_structure_check --live \
  --env-file ../bedrock-python/.env --cases heldout
```

Each run creates a fresh private directory under `runs/provider-structures` with
implementation hashes and raw/normalized receipts. No automatic fixture retries
occur. A full-worker failure remains `passed_structural_check=false` even if JSON
parsing succeeded. Reusing these documents is regression testing, not new held-out
evidence.

## Documentation integrity

Functions document purpose, Args, meaningful Returns, side effects and failure
outcomes. Documentation-only passes compared executable ASTs, 44 canonical
Pydantic schemas and 16 prompt-file hashes before and after. Both passes were
unchanged; canonical schemas and prompt files also match the task-start snapshot.
Wire formatting instructions intentionally changed during implementation. See the
audit counts in the evidence ledger; unchanged prompt files do not mean unchanged
provider wire requests.

Design reference: AWS documents
[JSON prefilling and structured-output guidance](https://docs.aws.amazon.com/nova/latest/userguide/prompting-structured-output.html).
