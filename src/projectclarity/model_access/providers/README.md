# Provider adapters and output codecs

Read in this order:

1. `contracts.py`: neutral requests, capabilities, prepared requests and response
   receipts. `completed_text()` is the common completion/structure gate.
2. `registry.py`: factories, settings precedence and pinned contract identities.
3. `structured_output.py`: identity, exact JSON-prefill and experimental nullable
   list codecs; explicit conversion receipts, never semantic repairs.
4. `anthropic_provider.py` and `bedrock_provider.py`: SDK-specific wire translation,
   authentication, counting and response-envelope handling.
5. `../model_client.py`: shared admission, single-attempt dispatch and settlement.

Research stages own canonical schemas and evidence validation. Exact catalogue
versus additions normalization belongs in
`knowledge_extraction/entity_output_normalization.py`, not in an SDK adapter.

See [configuration and provider extension](../../../../docs/MODEL_PROVIDERS.md)
and [structured-output architecture and evidence](../../../../docs/STRUCTURED_OUTPUT.md).
