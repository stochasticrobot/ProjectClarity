# Browser interface and HTTP application

Shows research progress and saved evidence, and accepts explicit user actions.
It does not decide which facts matter or which research stage runs next.

## Reading order

- [web_app.py](web_app.py): application entry point, request validation, local-only
  access checks, pages, and API routes.
- [pages](pages/): screen templates.
- [scripts](scripts/): page behavior, polling, evidence inspection, and controls.
- [styles](styles/): shared appearance and responsive layout.

[workflow_navigation.js](scripts/workflow_navigation.js) draws the stage flow,
round labels, and shared activity pill. It sits below the flow and above the
question: green while working, yellow for interruptions or unavailable progress,
and red for stage failures. Individual source or extraction issues stay in their
detail panels. Reduced-motion preferences disable the activity animation.

Initial Settings exposes run-wide request/token budgets, total research rounds
and separate concurrency controls
for each model-processing stage. Pages follow live round transitions; explicit
saved views remain read-only. Report generation has a progress bar, and the final
item table supports filtering, citation inspection, and CSV export.

[research_budget.js](scripts/research_budget.js) displays the shared pause dialog
on all screens. It explains the limit and the exact extension offered by Continue;
Quit preserves saved results. Polling or closing a browser cannot grant allowance.
The activity pill turns yellow while waiting for this decision.

[model_usage.js](scripts/model_usage.js) reads recorded call/token usage and budgets. Status
polling and opening pages do not start research. Start, stop, resume, and retry
controls call the [session service](../workflow/research_sessions.py), which checks
whether each action is valid. This is a loopback-only app, not an authenticated
public deployment.
