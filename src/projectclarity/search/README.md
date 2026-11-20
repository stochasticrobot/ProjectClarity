# Search for candidate documents

Runs prepared queries and returns candidate URLs, titles, and discovery notes.
A search snippet is a lead, not accepted report evidence.

- [search_execution.py](search_execution.py) executes prepared queries, records
  outcomes, and applies the bounded fallback policy.
- [public_search.py](public_search.py) reads account-free Mwmbl JSON and Bing RSS
  results with bounded requests and validated response formats.
- [web_search.py](web_search.py) communicates with search services and handles
  search-specific pacing and result normalization.

The first round uses `web_query_count` prepared queries (default 2, range 1–10),
chosen in research Settings. The planner must return exactly that count without
trivial duplicates. Each query still requests up to five results; search receipts
and displayed totals use the chosen count.
The default order is **DuckDuckGo → Mwmbl → Bing RSS → Wikipedia**. HTTP errors,
provider-reported errors, malformed replies and zero usable results advance to the
next provider. A recognized DuckDuckGo image CAPTCHA instead pauses for the
human: Clarity displays a dialog with Submit answer, Try next search provider and
Cancel research. The isolated search worker retains its original client, cookies
and form tokens while waiting. Only cached raster images and a one-use challenge
ID reach the browser; no model examines or answers the puzzle. Skipping continues
the fallback chain. A successful provider stops the chain; cancellation stops all work.
Each provider is attempted at most once per query with the same search text and up
to five accepted leads. All attempts have separate receipts and visible diagnostics.
Same-provider requests are paced ten seconds apart in this application process.
No account or API key is required by these adapters; provider quotas still apply.
Availability and relevance are not guaranteed, particularly for Bing's RSS interface.
No result snippet becomes accepted evidence until the document pipeline reads it.

Search does not download result documents or own their processing pool.
[SourceCollection](../workflow/source_collection.py) hands its results to the
[document pipeline](../documents/README.md). Additional research rounds use ranked
links and do not repeat this search step.

## Human verification lifecycle

- [duckduckgo_session.py](duckduckgo_session.py) retains the worker's client,
  validates provider-directed targets and parses actual result links after an answer.
- [captcha_handoff.py](captcha_handoff.py) holds per-session, one-use human decisions
  in memory; [captcha_routes.py](../ux/captcha_routes.py) serves tiles and actions.
- [research_captcha.js](../ux/scripts/research_captcha.js) displays the dialog and
  preserves selections while polling. Renewed puzzles receive new IDs and clear them.

After **30 seconds without interaction**, the server automatically skips the
challenge and tries the next provider, even if no browser is connected. Clicking
or using the keyboard inside the dialog resets that inactivity deadline. Status
polling, automatic focus and page refreshes do not extend it. The dialog displays
the remaining time on each status refresh. Expired challenges cannot be revived
by a late activity notice or answer.

Another research page for the same session can show a still-pending challenge. Stale/double submissions are
rejected. Stop and shutdown cancel the worker; restart marks unfinished research
interrupted rather than restoring cookies or replaying a search. Temporary image
handoff files are private and removed on normal completion/cancellation. Challenge
material is not stored in research evidence or committed to the repository.
Unsupported puzzles, missing images or repeated challenges beyond five human
submissions fail that provider and allow the next fallback. This is not a promise
of permanent unblocking: only the provider decides whether to accept an answer.
