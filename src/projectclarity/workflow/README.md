# Research order and saved sessions

[research_graph.py](research_graph.py) owns stage order and stopping gates. [research_sessions.py](research_sessions.py)
saves questions and schedules explicit user actions. [source_collection.py](source_collection.py)
connects prepared searches or selected URLs to the same document processor.

## Execution and persistence

The graph owns stage order; workers save outcomes rather than invoke downstream
stages themselves. Setup and search preparation retain SQLite checkpoints. The
autonomous research graph uses explicit stage recovery, not crash replay.
[The code guide](../../../CODE_GUIDE.md) explains the graph boundaries and request flow.

## Research rounds

The initial question's **Settings → Research rounds** control saves
`research_rounds` (strict integer 1–5, default **2**, including the initial search).
A value of 1 preserves the single-pass workflow. Legacy sessions lacking the
setting remain single-pass; deploying this feature does not restart them.

1. Search once, download/extract, select evidence, triage, investigate, rank links.
2. If more rounds are allowed, follow up to 10 selected unique destinations (the
   existing maximum 3 per source page still applies). Use their anchor text as the
   input title. Do not run new searches.
3. Repeat the same extraction/review stages with the saved concurrency limits.
4. After the last round, or when no unvisited selected destinations remain,
   generate one report from the combined reviewed evidence.

[research_rounds.py](research_rounds.py) archives each completed round's stage references in
`round_history`, retains the active `current_round` and immutable `round_paths`,
excludes previously attempted original/final URLs, and builds content-addressed
cumulative report inputs. New round source scores use **that round's** reviewed
facts and source pages. Older pages are not re-ranked; the final round normally
produces a saved link selection, but does not fetch it beyond the configured limit.
When over the budget checkpoint threshold, final-round ranking is skipped.
Known destination aliases are excluded before acquisition. Unknown redirects may
require another GET to discover the final destination; their receipts are kept
but duplicate destination text is not extracted again.

Only facts in each investigation's `report_knowledge.json` enter the combined
bucket. IDs are qualified by round, with original fact IDs and source-run paths
retained. No original investigation store is overwritten. Review within each round
uses that round's saved document corpus; this feature does **not** reinvestigate
previously rejected claims against later documents or perform fuzzy fact deduplication.
The final item assembler can combine complementary accepted evidence across rounds.

## Stopping and recovery

Stops finish the current in-flight requests and prevent the next round. An explicit
retry of linked-round documents uses its saved selected URLs, never the initial
search. Existing stage resume policies remain unchanged. Report retry enters only
reporting, reuses the same cumulative inputs, and never repeats research rounds.
Server restart marks active work interrupted and never silently replays it.

All original round artifacts remain on disk. Live stage screens describe the
current round; the report coverage lists every round. The round limit is a bound,
not a promise of adequate coverage, a provider call budget or a spending cap.

## Budget checkpoint before link ranking

[round_budget_checkpoint.py](round_budget_checkpoint.py) checks settled run-wide
usage before constructing a ranking worker. If **more than 50% of either current
call or token allowance** has been used, it saves a choice: prepare the report now,
or rank links and research another round. Exactly 50% proceeds normally. Estimated
usage counts; unconfirmed usage also requires a choice. Continuing does not increase
the hard budget. Reports still consume budget and may reach its ordinary pause.

The checkpoint survives server restarts and page reloads without spending. Its
single-use decision ID prevents duplicate clicks from granting multiple rounds.
On the last configured round, high usage skips ranking and goes to reporting without
asking to approve a nonexistent next round. Existing report-only retries stay
report-only and do not replay the checkpoint or upstream research.
