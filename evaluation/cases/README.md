# Questions and answer keys

[legislature_membership.json](legislature_membership.json) contains three small,
dated scenarios: Canadian MPs for Yukon/Nunavut, Vermont's US House member,
and Vermont's US senators, plus a fourth combined five-person acceptance case.
The combined case reuses those verified facts and four of their source URLs;
it requires cross-source evidence and counts overlapping names once.
Each includes sources, expected people/chambers/regions,
explicit aliases, historical distractors and interpretation notes.

The source checks record acquisition outcomes and content fingerprints, not a
promise that a URL will always work. Official records are the primary reference;
Wikipedia is a second source with useful historical distractors. Source conflicts
must be investigated, not resolved by majority vote or by trusting the model.

The assistant independently verified all five people and ten reference relationships
on 2026-10-22; no user review is required. [reference_validation.md](reference_validation.md)
explains the evidence, exclusions and access limitations. The JSON embeds the dated
verification record so experiment copies retain it. This is source-verified, not
human-labelled, data. Party affiliation and service
start dates are deliberately outside this first scorer's scope.

Names, source choices and expected facts belong here, **not in agent prompts or
production extraction rules**. Adding a case should not require a new graph node.
The evaluation runner passes only the question and sources to the agent.

[Back to the evaluation guide](../README.md)
