# Reference-answer verification — 2026-10-22

**Result: all five expected people and all ten membership/representation facts
are confirmed within the three stated geographic scopes.** The assistant did
the source review; no review is outstanding for the user. This is not a claim
that a human labelled these answers or that the research agent passed an evaluation.

## What was checked?

For each person, the answer key expects one `MEMBER_OF` chamber relationship and
one `REPRESENTS` constituency/state relationship. Current official rosters establish
who holds the seats; Wikipedia supplies a second check and historical context.
Former officeholders, election candidacies and state/provincial offices do not
establish current federal membership.

| Expected person | Chamber and represented area | Official evidence | Cross-check |
| --- | --- | --- | --- |
| Brendan Hanley | Canadian House of Commons; Yukon | [Current profile, ID 111109](https://www.ourcommons.ca/Members/en/111109); [Yukon roster](https://www.ourcommons.ca/Members/en/search?province=YT) | [Biography](https://en.wikipedia.org/wiki/Brendan_Hanley) |
| Lori Idlout | Canadian House of Commons; Nunavut | [Current profile, ID 111116](https://www.ourcommons.ca/Members/en/111116); [Nunavut roster](https://www.ourcommons.ca/Members/en/search?province=NU) | [Biography](https://en.wikipedia.org/wiki/Lori_Idlout) |
| Becca Balint | US House of Representatives; Vermont at-large | [House Clerk profile, ID B001318](https://clerk.house.gov/members/B001318); [current House directory](https://clerk.house.gov/Members/ViewMemberList) | [Current Vermont delegation](https://en.wikipedia.org/wiki/Vermont%27s_congressional_delegations) |
| Bernie Sanders | US Senate; Vermont | [Current Vermont senators](https://www.senate.gov/states/VT/intro.htm); [official biography, ID S000033](https://history.house.gov/People/Listing/S/SANDERS,-Bernard-(S000033)/) | [Current Vermont delegation](https://en.wikipedia.org/wiki/Vermont%27s_congressional_delegations) |
| Peter Welch | US Senate; Vermont | [Current Vermont senators](https://www.senate.gov/states/VT/intro.htm); [official biography linking ID W000800](https://history.house.gov/People/Detail/24183) | [Current Vermont delegation](https://en.wikipedia.org/wiki/Vermont%27s_congressional_delegations) |

The Canadian territory-filtered rosters each return exactly one current member.
The House directory contains one Vermont member, Balint. The Senate's Vermont
page lists two current senators, Sanders and Welch. These checks establish
completeness within these small scopes, **not across either country**.

## Names and exclusions

- The official Senate roster uses **Bernard Sanders**; the Wikipedia delegation
  uses **Bernie Sanders**. These are explicitly mapped to one identity.
- [Welch's biography](https://en.wikipedia.org/wiki/Peter_Welch) records **Peter
  Francis Welch**. [Balint's biography](https://en.wikipedia.org/wiki/Becca_Balint)
  records **Rebecca A. Balint**. That attested spelling was added to the alias list;
  **Rebecca Balint** remains an explicit middle-initial-omitted variant.
- Chamber abbreviations are explicit labels for the same institutions. Vermont
  at-large is normalized to Vermont because that district covers the whole state.
  The separate chamber relationship distinguishes House and Senate membership;
  this geographic equivalence is not a general rule for other districts.
- **Larry Bagnell** and **Mumilaaq Qaqqaq** are predecessors in the Canadian
  biographies, not the current MPs in the checked rosters.
- **Welch and Sanders** are excluded from the current House case, not from Congress:
  they are current senators. **Balint** is excluded from the Senate case because
  she is the House representative.
- **Patrick Leahy** is excluded from both current-office cases. The official
  Senate page lists him historically through 2023, not among the current senators.

## Evidence trail and limitations

The answer-key JSON embeds this verification's date, reviewer, per-person source
links, exclusions and additional snapshot hashes. Each new experiment freezes
that metadata alongside its expected answers. Older experiment copies are not
rewritten or retroactively marked verified.

Local acquisition evidence is under
`.projectclarity/evaluation-source-check-20261022/` (original case inputs) and
`.projectclarity/reference-review-20261022/` (additional rosters and biographies).
The latter contains five successfully retrieved documents with raw and normalized
text hashes. These private snapshots are not needed to run the offline evaluator;
the checked-in metadata provides the source trail without checking downloaded
websites into Git.

The Senate page was inspected through web research, but **ProjectClarity's own
retriever could not obtain its robots file**. The case retains `robots_unavailable`;
verification of the answer key does not turn that failed acquisition into a
successful one or bypass crawler policy.

No names, chambers or represented areas needed correction. One attested alias
was added. Party affiliation, service start dates and semantic faithfulness of
future model outputs remain outside this reference comparison. Membership can
change; the date matters. No paid model evaluation was run for this verification.

## Reuse in the combined test

`combined_five_members` combines these same five verified identities and ten
relationships into one four-source run. Its `reference_case_ids` point to the
three original audited cases. This adds an aggregation/deduplication test, not
new independent reference facts or a new live-model accuracy result.
