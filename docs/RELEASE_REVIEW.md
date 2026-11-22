# Release review — 2026-11-22

## Scope

Reviewed the pending Bedrock/provider abstraction, structured-output handling,
Nova recovery, knowledge-session selection, investigation-page transition, and
associated tests and documentation against the previous commit (`94f92b1`).
The review focused on provider isolation, unchanged evidence validation, bounded
retries, saved-result reuse, budget accounting, and credential/export boundaries.
It is not a comprehensive security audit or a model-quality benchmark.

## Corrections made during review

- Preserved the author's new README introduction; corrected spelling, grammar,
  trailing whitespace, and the obsolete model-configuration pointer.
- Clarified that Ollama requires a future adapter and is not implemented.
- Replaced a workspace-specific credential-file example with local `.env` setup.
- Distinguished historical adapter qualification from the subsequent user-reported
  successful fresh Nova research run. Historical verification JSON stays unchanged.
- Distinguished single-attempt SDK transport from bounded stage-level output retry,
  provider counts from Nova estimates, and requested from effective output caps.
- Corrected the investigation comment to describe audited whitespace restoration.

No runtime changes were introduced by this review. The pre-existing release changes
remain intentional behavioral changes and are covered by the regression suite.

## Verification

- `python -m unittest discover -s tests -t . -q`: 230 tests run,
  **226 passed, 4 skipped**. Skips retain their existing optional-dependency/service
  conditions; this result does not claim live Neo4j or live provider coverage.
- Executable Python ASTs unchanged from the review-start snapshot after removing
  documentation strings; **44 generated application schemas unchanged**.
- Prompt files and provider adapter files byte-identical to the review-start snapshot.
- All checked local Markdown links resolve to release files or directories.
- CLI help and the offline live-model-test preview succeeded. No paid model calls,
  research retries, app restarts, or deployments were performed by this review.
- `git diff --check` passed after whitespace corrections.

## Secret checks

The initial proposed release contained **249 files**. All were copied to an
isolated scanner snapshot, including new untracked release files. **1,076 unique
blobs across all 11 reachable Git commits** were also scanned, not just the latest
checkout. The review record itself was included in the final pre-commit scan.

Checks used `detect-secrets` with network verification disabled, supplemental
unfiltered credential/private-key patterns, and exact matching against the three
nonempty credential values available in the application's private configuration
and process environment. None found a credential. A synthetic private-key marker
confirmed the scanner could detect a positive control. Values were not printed,
placed in this report, or sent to a credential-verification service.

Only `.env.example` is tracked; its credential slots are empty. The real `.env`,
private runs, virtual environment, and local-only editorial/history notes remain
excluded. Existing ignore rules already cover dotenv variants and `.pem`/`.key`
files. No credentials were removed from history because none were detected.

These are text/byte-level checks, not a guarantee that every possible secret format
is recognized or that text embedded in images is absent. They cover reachable Git
history, not unreachable objects, other repositories, or remote GitHub artifacts.
