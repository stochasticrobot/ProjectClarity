# Rank discovered links for further research

`link_ranking_stage.py` is invoked by the research graph after investigation and
before another document round or reporting. It reads the current round’s exact
`report_knowledge.json` evidence bucket.
Each unique original fact contributes once to its original source document:
source points = 100 × source fact count / all report-evidence facts. Supporting
quotes do not multiply that contribution. With zero facts, source points are zero.

[document_links.py](../documents/document_links.py) reads saved HTML anchors and PDF URI annotations.
The ranking stage fetches no new pages. HTML anchor text is the link title being
rated, not the destination page’s title. The originating page’s actual HTML title takes precedence over search
titles. Plain text has no hyperlink metadata; missing shortlisted link titles get a zero
bonus without an LLM call. Downloaded source pages, except recognized duplicate destinations, are eligible, including
pages contributing zero report facts. Invalid/credential-bearing destinations and
same-page fragments are excluded using the existing URL normalizer.

## Shortlist before model calls

`link_shortlisting.py` combines normalized destinations **before** model work,
retaining all source-page/title associations. The occurrence with the highest
source contribution supplies the score and title context; ties prefer a title,
then source URL, title and occurrence ID. No score is summed across duplicates.

Already-downloaded original/final URLs, exact account/legal path names and known
non-document asset extensions are excluded. The complete conservative lists live
in `link_shortlisting.py`; About, Contact, PDFs and ambiguous paths remain eligible.
These are URL rules, not judgments about the destination's actual content.

Remaining destinations sort by source score, then URL/ID. At most **500** enter the
shortlist, with at most **50 per final source-page URL**. Duplicate ownership is
not reassigned to fill quotas. The rest are saved as **not assessed**, not irrelevant.
Lower-contributing pages can therefore lose useful links: this is an explicit
cost/coverage trade-off, not exhaustive title assessment.

`link_scoring.py` validates exact ID coverage in batches of 25 shortlisted titles.
That means **at most twenty title-ranking calls per attempt**, fewer when some
shortlisted links have no title. Failures never cause automatic backfill or retries.
The editable [rank_link_titles.txt](../prompts/rank_link_titles.txt) asks for none/low/medium/high ratings,
worth 0/5/10/25 points. Source scores are not sent to the title-ranking model.

## Selection and recovery

Scores are additive. Selection takes up to ten ranked shortlisted destinations,
with at most three per final source-page URL (redirect aliases share a quota).
Ties use URL then occurrence ID. The representative source chosen before ranking
owns its quota; it is not reassigned to evade the per-page cap. No minimum score
threshold is imposed, so zero-score links can fill remaining positions.

Every attempt saves input hashes, all candidates, batch receipts and selected
links. Failed batches stay unranked. Partial results can advance to another round or the report;
a fully failed attempt stops before reporting. Stop requests and server restarts
never replay calls automatically. An explicit retry starts a new retained attempt;
a report-only retry reuses completed ranking. Earlier completed reports are not
backfilled automatically. `ux/pages/link_ranking.html` and its matching script
show both scores, all source associations, issues and selected links. Counts
distinguish discovered occurrences, unique destinations, duplicates combined,
excluded, shortlisted, not assessed and ranked. Candidate filters and a 50-at-a-time
display avoid rendering thousands of cards at once. All candidates remain saved.

Ranking predicts usefulness; it does not establish destination content or add
facts to the report. The [research graph](../workflow/research_graph.py) follows selected links through
the ordinary document pipeline when another round is allowed. It excludes visited
destinations across rounds; final-round selections remain saved but are not fetched.
See [round limits](../workflow/README.md).
