# Report types: intent first, evidence second

## Acceptance contract

Before searches begin, the production setup workflow asks the model to choose an
answer format from the question and requirements. Clarification answers may revise
that choice. The saved choice must reach search preparation and reporting unchanged;
returned evidence must not choose a replacement format. A single-entity question
must produce a source-linked profile through the actual HTTP/graph reporting path,
while directory questions preserve the existing directory behavior. Unsupported
formats must stop explicitly, not silently fall back.

## Current formats

| Type | Available | Output |
|---|---|---|
| List/directory | Yes | Existing cited items, requested fields, cards/table and CSV |
| Entity profile | Yes | One requested company, person or organization; cited overview, requirement-driven sections, identity uncertainty and section-oriented CSV |
| Comparison | No | Recognized intent; setup explains that this format is unavailable |
| Timeline | No | Recognized intent; setup explains that this format is unavailable |
| Topic overview | No | Recognized intent; setup explains that this format is unavailable |

## Planning and persistence

The existing clarification response now includes a required `report_plan`, alongside
its zero-to-two clarification questions. This adds no model call. It records
`report_type`, `reason`, and optional target metadata: `subject`, `subject_kind`,
`subject_scope`. Non-profile plans may retain population metadata; only
`report_type` selects the implementation.
For profiles, `subject_scope` may be null when no additional identity qualifier was
supplied; a named subject and kind remain required. Display/export explains the
absence without inventing facts. The existing refinement call updates both requirements and plan using the actual
bound user answers. Material identity/intent ambiguities belong in clarification;
users are not asked to choose internal schema names or supply discoverable facts.

The plan is checkpointed and saved in the session before search. Search preparation
receives it with the finalized question, requirements and clarification answers.
Report manifests and artifacts retain it; complete report requests include it in
their cache identity. Changing a plan cannot reuse a different plan's task reply.
The browser shows the choice and reason. No new format-confirmation screen is added.

New model responses missing the required plan fail validation. Historical sessions
without the key keep their original directory behavior. Existing paused clarification
sessions can resume into the new refinement contract. Explicit null or malformed
saved plans do not receive the historical default. Historical narrative fixtures
remain a separate legacy mode and cannot override a profile plan.

## Architecture and ownership

- `reporting/contracts.py`: typed saved intent and historical-session compatibility.
- `reporting/registry.py`: supported builders, prompt manifests and export routing;
  no evidence-dependent format choice.
- `reporting/directory/`: the existing builder, selection schema/validator and exact-
  label assembly, moved without changing their algorithm. Old modules re-export
  their public names for compatibility.
- `reporting/entity_profile/`: independent contribution schema, identity/citation
  validation, deterministic assembly, builder and section-oriented CSV export.
- Shared reporting files remain at the package root: bounded dispatch, concurrency,
  lossless payload compaction, report lifecycle, receipts and historical validators.
  Both builders execute that same dispatcher; this is not a fixture-only path.
- `ux/scripts/entity_profile_report.js`: literal-text profile presentation using the
  existing source-evidence controls. Directory filtering/table controls do not apply.

Prompts remain centrally editable in `prompts/`: `report_planning.txt` is appended
to clarification/refinement instructions; `prepare_entity_profile_batch.txt` is the
profile contribution task. Provider schemas handle formatting. No provider-specific
research prompt or new transport is introduced.

## Entity identity and evidence boundaries

Every assessed fact receives a subject-identity status: match, ambiguous or not_target.
Matching requires a nonblank reason and a literal contiguous quote from that fact's
supplied source evidence. Findings may cite only facts assessed as matches. Unknown
references, fabricated excerpts and citations to ambiguous/nonmatching identities
are withheld without discarding independently valid neighbours. Missing assessments
remain unreviewed and enter bounded recovery when the reply contains usable work.

Ambiguous identities remain visible in the profile and export but cannot contribute
claims to sections. Conflicting identity decisions across retained fragments also
withhold affected findings. Subject labels and scope are explicitly **requested
identity**, not source-backed findings. Namesakes and aliases are not merged by code.

This does **not** prove real-world identity or semantic entailment. The model still
judges whether an actual excerpt establishes the requested identity and supports a
statement. An exact quote can be misinterpreted. Tests establish rejection rules,
not reliable real-model ambiguity detection or cross-source entity resolution.

Each original requirement becomes a section. A section with no validated finding is
unknown; none disappears to make the report look complete. Findings retain individual
citations and supported/conflicting statuses. Distinct statements are retained rather
than flattened into a single value. Code does not independently detect semantic
contradictions across batches; conflicting statements can remain separately visible.
A supported section means it has supported contributions, not exhaustive coverage.

Assembly uses no final unbounded model synthesis. Overview contributions are
source-cited and exact duplicates combine their references; large evidence sets can
therefore yield a longer overview. Improving narrative concision without losing
qualifications is a separate task.

## Recovery, exports and verification

Profiles use the shared input preflight, recursive whole-record splitting, worker
limit, call/token budget, cancellation, single rejected-output retry and one targeted
omitted-fact recovery pass. Failed independent batches retain successful siblings.
Partial reports name unreviewed fact counts; explicit retry revalidates matching
saved profile tasks without repeating upstream research. Unknown sections and
identity uncertainty are evidence gaps, not automatic grounds for further calls.

The profile CSV includes scope, overview, section findings/statuses, unknown sections,
identity uncertainty, gaps, fact IDs and source URLs. Every cell is spreadsheet-formula
protected. The existing directory CSV remains unchanged.

Permanent coverage:

- `tests/integration/test_entity_profile_reporting.py`: full HTTP setup→research→profile,
  plan propagation, clarification-driven type changes, unavailable formats, restart
  reads, split-leaf isolation, cached retries, absent historical versus invalid new
  plans, unknown sections, identity restrictions, cancellation and safe exports.
- Native Nova JSON-prefill and experimental nullable-list wire modes are exercised
  through the production caller, codecs, ledger and budgets with AWS replies mocked.
- `tests/unit/test_entity_profile_page.py`: opt-in real Chromium check of actual page
  scripts, literal text, unknown/conflicting sections, citation interactions and a
  narrow viewport. API replies are fixtures and all network is intercepted.

Run the normal suite as documented in [tests](../tests/README.md). To include the
browser check, set `PROJECTCLARITY_TEST_BROWSER` to your installed Chromium executable.
These checks are offline architectural evidence. No live-model profile-quality
benchmark or user approval of generated profile content is claimed.

## Implementation verification — 2026-11-22

The full suite with Chromium enabled ran 239 tests: 235 passed and 4 existing
optional checks skipped. A subsequently added held-out production-entry test also
passed, covering distinct company, person and organization sources: **236 passed,
4 skipped across those runs**. The earlier development pass exposed three outdated
clarification fixtures and a missing static-asset allowlist entry; these were fixed
and the affected tests rerun successfully. Tests never used live model credentials.

The directory implementation matches its previous source exactly except for import
relocation and the prompt-file path adjustment. Saved new plans additionally enter
shared request payloads. Python function documentation checks, local Markdown links
and `git diff --check` passed.

The idle local application was restarted after checking for active research/graph
work. HTTP checks confirmed it serves the current setup page and both profile/report
scripts. Browser interaction was validated with intercepted fixture API data, not a
new live-model profile. No new user research was started and no Git push was made.

### First live setup failure: nullable identity qualifier

The first observed Solink setup chose entity_profile correctly but returned
`subject_scope: null`. The advertised schema allowed null while local validation
required text, so setup failed. This invalidated the earlier assumption that the
offline coverage exercised optional identity scope. The validator now preserves
null as an absent qualifier; subject and kind remain mandatory and blank text is
rejected. UI/CSV explain absence without inventing identity facts. The original
saved reply passes replay unchanged. Eighteen setup/profile tests and the Chromium
check passed after this correction. No live model retry or original-artifact edit
was performed. Prompt guidance now also discourages redundant profile-versus-
overview and topic-priority questions; that guidance is not live-quality proven.

### Live Solink report audit: missing customers and incorrect attribution

The 2026-11-22 run 8ac102f5-85bb-402c-b7bd-4a0eff7aef34 invalidates
any interpretation of the offline checks as proof of reliable live subject
attribution or complete profile coverage. Its report accounts for 46/160 facts;
114 remain unreviewed, including customer facts F44–F46 (Tim Hortons, Five Guys,
Moose Knuckles), F141 and F146/F152 (customer testimonials). Saved customer-batch
tasks failed status-schema or identity-assessment validation after bounded retry.
The customer section's unknown state is therefore not evidence that customers
could not be identified in the collected sources.

More seriously, the accepted report attributes Claygent AI and other Clay
products to Solink, citing F131–F138. F131 explicitly says Clay offers Claygent AI
and cites clay.com/customers/coverflex. Existing quote-membership and
model-assigned identity checks did not prevent this semantic misattribution.
Production routing and mechanical citation checks remain implemented; reliable
semantic attribution is NOT established. This audit did not alter the saved run,
retry model calls, or implement a repair.


### Completeness recovery correction

Profile batches now validate assessments and findings independently. Invalid
fields are quarantined with saved diagnostics; independently checked findings
survive. Unassessed IDs cannot be silently excluded through unused_fact_ids.
Unresolved facts enter the existing one-pass targeted recovery, for partially usable replies. Invalid envelopes and wholly unusable replies
retain the existing one-retry policy. An unsuccessful recovery retains its valid parent findings.

The real ReportRun entry point was replayed against an isolated copy of the
Solink artifacts with a saved-response-only transport. It accounted for 93/160
facts, versus 46/160 originally, and populated customer findings; 67 remained
explicitly unresolved because no valid saved answer was available. All original
artifact hashes remained unchanged. This proves preservation and routing, not
live Nova recovery quality or semantic correctness. The Clay attribution issue
and the oversized unrelated Clay record remain unresolved. No user run was retried.


Recovery verification: full suite ran 244 tests (239 passed, 5 skipped). After
final quarantine-accounting and zero-coverage retry refinements, all 13 profile
integration tests passed and the production saved-response replay was repeated.
Regressions cover partial-output retention, unresolved-only requests, failed
recovery retention, duplicate assessments/JSON keys, invented quotes and invalid
statuses. No live quality or complete coverage claim follows from these tests.

### Directory setup: populated target metadata

The live setup 83f90145-f917-459e-a3c6-62e6778eeb08 correctly chose directory
but supplied subject="Canadian companies" and subject_kind="company". The old
non-profile-null-only validation rejected the otherwise valid setup. Validation
now retains schema-valid target metadata on non-profile plans rather than
discarding possible scope or inferring a profile. report_type alone selects the
builder. Entity profiles still require a nonblank named subject and kind;
malformed plans and unsupported formats do not silently become directories.

Exact saved define/clarify replies pass the real HTTP setup path unchanged and
reach clarification. Original artifact hashes remain unchanged. A permanent
full HTTP workflow regression with populated directory subject, kind and scope
reaches directory report/CSV and verifies metadata in search/report requests.
All 22 profile/setup tests passed. No live model calls or user-run retries occurred.
