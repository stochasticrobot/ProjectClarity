"""Connect the pages the user sees to the application's research functions.

This is the web server's starting point. It delivers the setup form, progress
screens and report page; receives actions such as submitting a question or stopping
research; and returns saved progress so the screens can update. It checks incoming
values before handing those actions to research_sessions.py.

The research itself runs elsewhere. This file does not decide which facts matter
or which research step comes next. Keeping page access separate from the work
means opening or refreshing a page can show existing results without repeating
searches or making new AI requests. The server is intended for local use.
"""
import argparse
from pathlib import Path
from uuid import UUID
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from projectclarity.model_access.model_client import ROOT
from projectclarity.workflow.research_sessions import Sessions, ACTIVE_STATUSES
from projectclarity.model_access.model_ledger import read_ledger
from projectclarity.model_access.research_budget import DEFAULT_MODEL_REQUEST_BUDGET, DEFAULT_MODEL_TOKEN_BUDGET


class StartInput(BaseModel):
    """Bounded user input and client-generated ID to make repeat submissions idempotent."""
    model_config = ConfigDict(extra='forbid')
    session_id: UUID
    question: str = Field(min_length=1, max_length=8000)
    auto_advance: bool = False
    model_request_budget: int = Field(default=DEFAULT_MODEL_REQUEST_BUDGET, ge=1, le=1_000_000, strict=True)
    model_token_budget: int = Field(default=DEFAULT_MODEL_TOKEN_BUDGET, ge=1, le=1_000_000_000, strict=True)
    research_rounds: int = Field(default=2, ge=1, le=5, strict=True)
    extraction_concurrency: int = Field(default=6, ge=1, le=16, strict=True)
    selection_concurrency: int = Field(default=6, ge=1, le=16, strict=True)
    triage_concurrency: int = Field(default=6, ge=1, le=16, strict=True)
    investigation_concurrency: int = Field(default=6, ge=1, le=16, strict=True)
    link_ranking_concurrency: int = Field(default=6, ge=1, le=16, strict=True)
    report_selection_concurrency: int = Field(default=6, ge=1, le=16, strict=True)


class BudgetDecisionInput(BaseModel):
    """Bind an explicit budget choice to the pause displayed to the user."""
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: int = Field(ge=1)
    action: Literal['continue', 'quit']


class RoundBudgetDecisionInput(BaseModel):
    """Bind the next-round choice to a durable, displayed pre-ranking checkpoint."""
    model_config = ConfigDict(extra='forbid', strict=True)
    decision_id: str = Field(min_length=1, max_length=64)
    action: Literal['continue', 'report']


class SelectionResumeInput(BaseModel):
    """Bind a selection resume to the displayed attempt and chosen concurrency."""
    model_config = ConfigDict(extra='forbid', strict=True)
    previous_artifact: str = Field(min_length=1, max_length=1000)
    concurrency: int = Field(default=6, ge=1, le=16)


class InvestigationResumeInput(BaseModel):
    """Bind an investigation resume to the displayed attempt and worker cap."""
    model_config = ConfigDict(extra='forbid', strict=True)
    previous_artifact: str = Field(min_length=1, max_length=1000)
    concurrency: int = Field(default=6, ge=1, le=16)


class AnswerInput(BaseModel):
    """At most two free-text answers, validated again against the actual saved questions."""
    model_config = ConfigDict(extra='forbid', strict=True)
    answers: list[str] = Field(min_length=1, max_length=2)


class ResumeInput(BaseModel):
    """Bind an explicit resume click to one stopped triage attempt."""
    model_config=ConfigDict(extra='forbid',strict=True)
    artifact_directory:str


class ReportRetryInput(BaseModel):
    """Explicit retry bound to the exact failed report attempt."""
    model_config=ConfigDict(extra='forbid',strict=True)
    previous_started_at:str


class RetryRunInput(BaseModel):
    """Explicit retry bound to a previous attempt, with an explicitly chosen engine."""
    model_config = ConfigDict(extra='forbid', strict=True)
    previous_started_at: str
    provider: Literal['duckduckgo','brave'] = 'duckduckgo'




def create_app(sessions):
    """Serve research setup, monitoring views and their small same-origin API.

    Args:
        sessions: Local session manager used by all endpoint handlers.
    Returns:
        FastAPI application. Loopback host/origin checks block cross-site writes;
        all responses are no-store. The server is not intended for public hosting.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        """Constrain this unauthenticated local app to its own loopback origin.

        Args:
            request: Incoming HTTP request.
            call_next: Framework callback for the next handler.
        Returns:
            No-store response or HTTP 403 for untrusted host/origin or fetch context.
        """
        from fastapi.responses import JSONResponse
        host = request.url.hostname
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.headers.get("host")}'
        if host not in ('127.0.0.1', 'localhost', 'testserver') or (
            request.method == 'POST' and ((origin and origin != expected) or
                request.headers.get('sec-fetch-site') == 'cross-site')):
            return JSONResponse({'detail': 'Use this app from its local URL.'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.get('/')
    def home():
        """Serve the single research-setup page.

        Args: None.
        Returns: Local HTML response; does not contact a model provider.
        """
        return FileResponse(ROOT / 'ux/pages/research_setup.html')

    @app.get('/stage/{sid}')
    def stage_review(sid: UUID):
        """Serve a read-only stage review; navigation cannot enqueue work.

        Args:
            sid: Existing research session UUID.
        Returns:
            Review HTML or 404 for an unknown session, with no graph/model calls.
        """
        sessions.read(str(sid))
        return FileResponse(ROOT / 'ux/pages/stage_review.html')

    @app.get('/assets/{name}')
    def asset(name: str):
        """Serve only explicitly allowed static assets.

        Args:
            name: Allowed application JavaScript or research_styles.css, not a filesystem path.
        Returns:
            Static response, or HTTP 404 for every other filename.
        """
        if name not in ('research_setup.js', 'research_styles.css', 'document_pipeline.js', 'evidence_selection.js', 'claim_triage.js', 'workflow_navigation.js', 'model_usage.js', 'research_budget.js', 'stage_review.js', 'claim_investigation.js', 'research_report.js', 'link_ranking.js'):
            raise HTTPException(404)
        return FileResponse(ROOT / 'ux' / ('styles' if name.endswith('.css') else 'scripts') / name)

    @app.post('/api/setup', status_code=202)
    def start(data: StartInput):
        """Begin question definition from validated browser input.

        Args:
            data: Bounded question and request ID.
        Returns:
            Public session status; graph work continues in the background.
        """
        return sessions.start(data)

    @app.get('/api/setup/{sid}')
    def status(sid: UUID):
        """Restore a session after polling or browser refresh.

        Args:
            sid: Validated session UUID.
        Returns:
            Saved public question, requirements and clarification state.
        """
        return sessions.read(str(sid))

    @app.post('/api/setup/{sid}/budget')
    def budget_decision(sid: UUID, data: BudgetDecisionInput):
        """Apply an explicit allowance extension or quit without replaying work.

        Args: sid: Session UUID; data: Displayed pause and user's decision.
        Returns: Current public snapshot, or HTTP409 when no workflow is active.
        """
        return sessions.budget_decision(str(sid), data.revision, data.action)

    @app.post('/api/setup/{sid}/round-budget', status_code=202)
    def round_budget_decision(sid: UUID, data: RoundBudgetDecisionInput):
        """Apply a clicked next-round choice without changing spending limits.

        Args: sid: Session UUID; data: Displayed checkpoint ID and explicit action.
        Returns: Queued or unchanged snapshot; another active run yields HTTP409.
        """
        return sessions.round_budget_decision(str(sid), data.decision_id, data.action)

    @app.post('/api/setup/{sid}/answers', status_code=202)
    def answers(sid: UUID, data: AnswerInput):
        """Apply one round of answers without restarting the original planning calls.

        Args:
            sid: Saved paused session UUID.
            data: Ordered clarification answers.
        Returns:
            Working public snapshot, or validation/conflict error before calls.
        """
        return sessions.answer(str(sid), data.answers)

    @app.post('/api/setup/{sid}/searches', status_code=202)
    def searches(sid: UUID):
        """Prepare two searches from the saved definition without executing them.

        Args:
            sid: Completed setup session UUID.
        Returns:
            Public query-planning status; the browser cannot replace saved context.
        """
        return sessions.prepare_searches(str(sid))

    @app.get('/run/{sid}')
    def run_page(sid: UUID):
        """Serve the separate autonomous monitor without starting or repeating work.

        Args:
            sid: UUID of the run to inspect.
        Returns:
            Monitor HTML, or404 if no owning session exists.
        """
        sessions.read(str(sid))
        return FileResponse(ROOT / 'ux/pages/document_pipeline.html')

    @app.post('/api/setup/{sid}/run', status_code=202)
    def run_start(sid: UUID):
        """Queue search, robots-aware download and extraction for prepared queries.

        Args:
            sid: Session UUID whose saved query pair will be executed.
        Returns:
            Public progress snapshot; duplicate calls are idempotent.
        """
        return sessions.start_run(str(sid))

    @app.get('/evidence/{sid}')
    def evidence_page(sid: UUID):
        """Serve the separate relevance monitor without starting work.

        Args: sid: Saved session UUID.
        Returns: HTML for inspecting progress, facts, reasons and source quotes.
        """
        sessions.read(str(sid))
        return FileResponse(ROOT / 'ux/pages/evidence_selection.html')

    @app.post('/api/setup/{sid}/selection', status_code=202)
    def selection_start(sid: UUID):
        """Start relevance selection for saved extraction knowledge exactly once.

        Args: sid: Saved session UUID.
        Returns: Public status; no upstream search/download/extraction is repeated.
        """
        return sessions.start_selection(str(sid))

    @app.post('/api/setup/{sid}/selection/resume', status_code=202)
    def selection_resume(sid: UUID, data: SelectionResumeInput):
        """Resume only the displayed stopped selection attempt.

        Args: sid: Session UUID; data: Attempt token and bounded call limit.
        Returns: Public queued or stale-action state; no upstream work is repeated.
        """
        return sessions.resume_selection(str(sid), data.previous_artifact, data.concurrency)

    @app.get('/api/setup/{sid}/usage')
    def usage(sid: UUID):
        """Expose safe aggregate/per-call accounting, including historical receipts.

        Args: sid: Existing session UUID.
        Returns: Known tokens, unknown usage, outcomes and validation; no secrets.
        """
        state=sessions.read(str(sid))
        return read_ledger(sessions.root/str(sid),state['status'] in ACTIVE_STATUSES)

    @app.get('/links/{sid}')
    def link_ranking_page(sid: UUID):
        """Serve the ranking view without starting work.

        Args: sid: Existing research UUID.
        Returns: HTML page; missing sessions produce the normal not-found error.
        """
        sessions.read(str(sid))
        return FileResponse(ROOT / 'ux/pages/link_ranking.html')

    @app.get('/api/setup/{sid}/link-ranking-data')
    def link_ranking_data(sid: UUID):
        """Expose scores and provenance, never private provider receipts.

        Args: sid: Existing research UUID.
        Returns: Saved candidates, selected destinations and operational issues.
        """
        return sessions.link_ranking_data(str(sid))

    @app.get('/report/{sid}')
    def report_page(sid: UUID):
        """Serve the report page without starting generation.

        Args: sid: Existing session UUID.
        Returns: HTML or HTTP404; GET cannot start model work.
        """
        sessions.read(str(sid));return FileResponse(ROOT/'ux/pages/research_report.html')

    @app.post('/api/setup/{sid}/report', status_code=202)
    def report_start(sid: UUID):
        """Explicitly start a historical report once.

        Args: sid: Existing session UUID.
        Returns: Queued/existing state or prerequisite HTTP409.
        """
        return sessions.start_report(str(sid))

    @app.post('/api/setup/{sid}/resume',status_code=202)
    def resume(sid:UUID,data:ResumeInput):
        """Queue a user-requested stopped-stage recovery, never a page-load action.

        Args: sid: Validated UUID; data: Exact stopped attempt identifier.
        Returns: Existing/queued state or409 when recovery is not applicable.
        """
        return sessions.resume_research(str(sid),data.artifact_directory)

    @app.post('/api/setup/{sid}/report/retry', status_code=202)
    def report_retry(sid: UUID, data: ReportRetryInput):
        """Explicitly retry a failed attempt while preserving its receipts and usage.

        Args: sid: Existing session; data: Exact prior report start timestamp.
        Returns: Queued/current state. Duplicate stale requests never replay work.
        """
        return sessions.start_report(str(sid),data.previous_started_at)

    @app.get('/api/setup/{sid}/report-data')
    def report_data(sid: UUID):
        """Read a fully validated report for rendering.

        Args: sid: Existing session UUID.
        Returns: Report or null; no model calls or raw private transport receipts.
        """
        return sessions.report_data(str(sid))

    @app.get('/api/setup/{sid}/report.csv')
    def report_csv_download(sid: UUID):
        """Download the same validated report as a spreadsheet-safe CSV.

        Args: sid: Existing session UUID.
        Returns: UTF8 CSV download, or HTTP409 if no completed report exists.
        """
        if sessions.report_data(str(sid)) is None:raise HTTPException(409,'No completed report is available.')
        state=sessions.read(str(sid))
        return FileResponse(sessions.root/str(sid)/state['report']['artifact_directory']/'report.csv',media_type='text/csv',filename='research-report.csv')

    @app.get('/investigation/{sid}')
    def investigation_page(sid: UUID):
        """Serve the monitor without starting a review.

        Args: sid: Existing session UUID.
        Returns: Investigation HTML or HTTP404; no work is queued.
        """
        sessions.read(str(sid))
        return FileResponse(ROOT/'ux/pages/claim_investigation.html')

    @app.post('/api/setup/{sid}/investigation', status_code=202)
    def investigation_start(sid: UUID):
        """Explicitly queue the saved triage once.

        Args: sid: Existing session UUID.
        Returns: Existing or queued state; invalid prerequisites produce HTTP409.
        """
        return sessions.start_investigation(str(sid))

    @app.post('/api/setup/{sid}/investigation/resume', status_code=202)
    def investigation_resume(sid: UUID, data: InvestigationResumeInput):
        """Resume an explicitly selected review attempt with saved-result reuse.

        Args: sid: Session UUID; data: Attempt token and bounded concurrency.
        Returns: Queued or stale-action snapshot; earlier stages are not replayed.
        """
        return sessions.resume_investigation(str(sid),data.previous_artifact,data.concurrency)

    @app.get('/api/setup/{sid}/investigation-records')
    def investigation_records(sid: UUID):
        """Read review packets, reasons and report records.

        Args: sid: Existing session UUID.
        Returns: Saved source-linked records, with no graph or model calls.
        """
        return sessions.investigation_records(str(sid))

    @app.get('/triage/{sid}')
    def triage_page(sid: UUID):
        """Serve the focused completeness/quote-support view without starting work.

        Args: sid: Existing session UUID.
        Returns: Triage HTML or404 for an unknown session; no model calls.
        """
        sessions.read(str(sid))
        return FileResponse(ROOT / 'ux/pages/claim_triage.html')

    @app.post('/api/setup/{sid}/triage', status_code=202)
    def triage_start(sid: UUID):
        """Start source-support triage once on saved selected facts.

        Args: sid: Existing session UUID.
        Returns: Public progress; never repeats upstream research stages.
        """
        return sessions.start_triage(str(sid))

    @app.get('/api/setup/{sid}/triage-records')
    def triage_records(sid: UUID):
        """Read bucket records and reasons for the triage screen.

        Args: sid: Existing session UUID.
        Returns: Saved original records with decisions, not provider receipts.
        """
        return sessions.triage_records(str(sid))

    @app.get('/api/setup/{sid}/evidence')
    def evidence_data(sid: UUID):
        """Expose original facts and validated inclusion reasons for inspection.

        Args: sid: Saved session UUID.
        Returns: Read-only batch decisions with source provenance; no model call.
        """
        return sessions.evidence(str(sid))

    @app.post('/api/setup/{sid}/retry', status_code=202)
    def run_retry(sid: UUID, data: RetryRunInput):
        """Start an explicit new attempt with its own retained source/extraction receipts.

        Args:
            sid: Session UUID.
            data: Prior attempt identity and chosen search provider.
        Returns:
            Public snapshot; stale duplicate retries do not repeat work.
        """
        return sessions.retry_run(str(sid),data)

    @app.post('/api/setup/{sid}/stop')
    def run_stop(sid: UUID):
        """Signal the running workflow to stop at its next safe boundary.

        Args:
            sid: Session UUID.
        Returns:
            Public stop-request state without deleting completed artifacts.
        """
        return sessions.stop_run(str(sid))

    @app.get('/api/setup/{sid}/knowledge/{document_id}')
    def knowledge(sid: UUID, document_id: int):
        """Read saved extracted assertions and source text for the monitor inspector.

        Args:
            sid: Session UUID.
            document_id: Recorded document identifier.
        Returns:
            Inspectable knowledge and provenance, never provider credentials.
        """
        return sessions.knowledge(str(sid), document_id)

    return app


def main():
    """Launch the new UI separately from the old application.

    Args:
        None. CLI accepts --port, --env-file and --data-dir. Credentials and
        saved data default to .env and runs/web under the working directory;
        packaged pages and scripts are located independently of that directory.
    Returns:
        None after shutdown. Binds loopback only; does not open a browser or call
        the provider until the user submits a question through the UI.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8775)
    parser.add_argument('--env-file', type=Path, default=Path.cwd() / '.env')
    parser.add_argument('--data-dir', type=Path, default=Path.cwd() / 'runs/web')
    args = parser.parse_args()
    sessions = Sessions(args.data_dir, env_file=args.env_file)
    try:
        uvicorn.run(create_app(sessions), host='127.0.0.1', port=args.port)
    finally:
        sessions.close()


if __name__ == '__main__':
    main()
