# Evaluation and methodology

[Case-study overview](README.md) · [Three-slide presentation](presentation.pdf) · [Full cited report](report.html)

This supporting audit records the measurements, source lineage and review limits
behind the presentation. The report fields and research results are unchanged.

## The task

> List Canadian technology startups focused on building AI products and/or services.

The run took place on **4 November 2026**, using `claude-haiku-5-5`, two research
rounds and six concurrent model calls per processing stage. Its recorded allowances
were 750 model requests and 8 million tokens. Search used DuckDuckGo.

The workflow defined five answer requirements: company identity, Canadian location,
startup status, AI focus, and the company's AI product or service. It searched,
extracted assertions with source references, selected relevant evidence, reviewed
source support, investigated flagged claims, ranked links, and followed ten selected
destinations through a second round. Finally, it assembled the combined evidence
into a report. Round two followed links; it did **not** issue another search query.

## What the run produced

| Recorded measure | Round 1: initial search | Round 2: followed links | Total |
|---|---:|---:|---:|
| Source URLs attempted | 8 | 10 | 18 |
| Documents successfully parsed | 5 | 9 | 14 |
| **Retained extraction fact records** | **401** | **820** | **1,221** |
| Facts selected for relevance | 329 | 257 | 586 |
| Facts admitted to report evidence after review | 320 | 229 | 549 |

Extraction totals agree across the knowledge indexes, sums of their chunk counts,
per-source totals, and selection inputs. They count retained source/chunk-scoped
assertions, **not unique real-world facts or independently verified truths**.
Unresolved extraction proposals are tracked separately: four in round one and one
in round two. No assertion is promoted to a verified truth by inclusion here.

The final report contains **49 rows**, with approximately **37 name groups** after
editorial grouping of obvious aliases. That is not a count of qualified startups.
**2 of 49 rows** have every requested field marked supported by the workflow.
This is an all-fields-complete label, not a finding that only two useful or correct
companies were discovered. Cohere, for example, has Canadian-location and AI-offering
evidence even though its startup-status field is marked insufficient.

**Recorded model usage:** 326 requests and 2,850,151 tokens across the run, including
setup, review, recovery and reporting. The budget ledger reports no estimated-token
or unknown-usage remainder. Token totals include the ledger's input/output/cache
accounting; they are not dollar cost. The search-to-report interval was about ten
minutes; it excludes earlier question setup and is not a performance benchmark.

## Did the additional round help?

**Yes, but unevenly.** It contributed 229 additional report-evidence facts and six
new name groups: **Clay, Floqer, ORKA AI, Signal 1, Solink and WisdomTCS**. They are
leads, not six confirmed Canadian startups. The other 31 name groups have at least
one round-one citation; the six above have only round-two citations in this report.
This is retrospective report-lineage analysis, not a separately generated round-one
report or a randomized comparison.

### Useful discovery: the company behind a platform

The first round encountered Folkly. Following its profile supplied evidence that
**ORKA AI is based in Calgary and built the cultural-archiving platform**. See saved
report evidence [F437](report.html#evidence-F437), [F443](report.html#evidence-F443)
and [F539](report.html#evidence-F539), with links to the
[profile used by the run](https://aimagazine.ca/canadian-ai-spotlight/folkly-ai-and-the-quiet-revolution-in-cultural-archiving/).

This is a useful expansion of the research: moving from a product mention to its
creator and location. The screenshot and the cited creator–product connection do
not establish that Folkly itself uses AI: an AI consulting description of its
creator is not product-level evidence. The earlier implication that this example
proved AI-product qualification was overstated and has been withdrawn. The example
is retained as evidence of autonomous follow-up discovery. It also does not establish
that ORKA satisfies the requested early-stage startup criterion.

The requested Folkly URL and its citation URL differ because the saved download
recorded a 301 redirect. The initial case-study export omitted the final URL.
[Provenance audit](PROVENANCE.md) now exposes that distinction and verifies the
three cited facts against the saved document, independently of today’s routing.

### Unhelpful exploration: geographic drift

The same link-selection process followed an India-focused directory and an AI
glossary. The India directory produced **244 retained extraction records**; the
glossary produced **104**. Together they account for 348 of round two's 820 records,
illustrating why a larger extraction count alone is not evidence of a better answer.

WisdomTCS entered the report with AI-service evidence but no Canadian qualification.
See [F370](report.html#evidence-F370) and the
[directory used by the run](https://techshark.io/blog/best-ai-companies-in-india/).
This is a scope-filtering weakness, not evidence that the company is Canadian.

## Evaluation: what is established, and what is not

| Dimension | Observation | Interpretation |
|---|---|---|
| Autonomous continuation | Round-one ranking assessed 134 destinations and selected ten to follow | The workflow made and executed bounded follow-up choices |
| Useful evidence | The Folkly–ORKA creator connection and Calgary location appear in the saved evidence | A concrete follow-up discovery, not proof of Folkly's AI functionality |
| Missing information | 47 rows lack at least one supported requested field | A review list, not a fully qualified company directory |
| Unsupported conclusions | WisdomTCS has no Canadian qualification; ORKA's startup status is unknown; the displayed creator/product statements do not establish Folkly's AI functionality | Do not infer product capabilities from its creator's consulting description or from a report field label |
| Identity handling | 49 rows reduce to 37 editorial name groups | Exact-label assembly leaves duplicates and product/company ambiguity |
| Source access | Two URLs blocked by robots rules, one exceeded redirects, one had an HTTP error | 18 attempted sources does not mean 18 usable documents |
| Processing gaps | Five unresolved extraction proposals; ten unreviewed triage facts | Partial upstream work is disclosed, not counted as successful coverage |
| Report robustness | 17 report batches; zero failed batches; all 549 input facts accounted for | Successful report assembly, not proof that every useful fact was captured |

**Accuracy, recall and total unsupported-claim rates were not measured.** This is a
spot-check and artifact audit, not an independent review of every assertion. There
is no authoritative answer key for this broad query, so the number of Canadian
startups missed is unknown. The case study does not claim exhaustive coverage.

The run also ranked 366 destinations at the end of round two, although no third
round followed. That is an opportunity to examine whether final-round ranking is
worth its model usage. More autonomy is not automatically more useful work.

## Engineering demonstrated

- **Bounded orchestration:** two rounds, per-stage concurrency, explicit usage
  allowances and recorded source outcomes. [Workflow](../../../src/projectclarity/workflow/research_graph.py)
- **Evidence lineage:** saved citations identify the assertions and round behind
  each report field. [Round accumulation](../../../src/projectclarity/workflow/research_rounds.py)
- **Recovery without discarding all progress:** extraction recorded four chunk-split
  events across the two rounds; partial outcomes remained visible rather than
  preventing the report. [Adaptive extraction](../../../src/projectclarity/knowledge_extraction/adaptive_chunk_sizing.py)
- **Bounded answer assembly:** model batches filter/restructure evidence; code
  assembles the report without a giant final merge request.
  [Reporting](../../../src/projectclarity/reporting/item_reporting.py)

The next quality improvements suggested by this run are tighter geographic and
entity-type screening of links, alias-aware company grouping, and a clearer split
between qualified matches and leads requiring further evidence. **Those are future
improvements, not corrections silently applied to this demonstration.**

## Audit and publication notes

- [Sources](sources.json) lists all 18 attempted URLs, their outcomes and extraction
  counts. Full third-party source documents are not republished; web pages can change.
- [Report data](report.json) preserves all original labels, fields, status labels and
  fact references. Saved assertion text and source links support inspection. It omits
  raw source quotations, provider payloads, local paths and credentials. The static
  report is a presentation export, not a byte-for-byte application export.
- [Measurements](metrics.json) records the counting definitions and original report
  SHA-256. Name grouping splits labels at ` — `, ignores case, and explicitly groups
  `FOLKLY.ai` with `Folkly`, and `Layer 6 AI` with `Layer 6`. These editorial groups
  are not canonical resolved identities. The underlying 49 rows were not merged.
- The current screenshot was captured from the completed application in its
  default card view, not a fabricated progress screen. The presentation
  uses a labelled crop of the ORKA card’s AI-focus and offering fields,
  linking to the full card; no field text or qualification status was altered. The
  caption explicitly distinguishes creator discovery from AI-product qualification.
- This example was chosen from an existing run for relevance to the project story;
  it is not a held-out benchmark. Saved outputs can be inspected without credentials;
  repeating live research requires API access and will not produce identical results.
- Application code reference: [`0910702`](https://github.com/stochasticrobot/ProjectClarity/commit/09107027f9c3c77918019ce40366eee0376b92c6),
  the published snapshot associated with these improvements. The saved run does not
  itself record a Git commit, so exact historical code identity is not asserted.

**Takeaway:** the agent expanded a broad question into a cited review list, found
additional leads by following links, and retained evidence of its own limitations.
The result is useful input to a person's research—not a substitute for that review.
