"""Create a research session and register the user's starting source references.

This is the first processing step for a new run. It assigns the run identifier,
loads URLs and local document paths, creates the output directory and starts
monitoring. Registering a source assigns an identifier for later use; it does not
download its contents or establish what it says. The workflow uses the returned
session for optional answer design and research planning.
"""
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from projectclarity.workflow_state import ResearchState
from projectclarity.research_schemas import ResearchSession, URLSource
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.nodes.initialize_research.source_reference_loader import load_sources
from projectclarity.session_storage import save_session


def initialize_research(state: ResearchState, output_root: Path, monitor=None) -> dict:
    """Register source references, create a unique run folder and start monitoring.

    The node adapter supplies typed request data here. Only the URL-list file and local path
    metadata are read; actual document contents wait until after planning/review. The
    initial saved session makes early failures inspectable. Optional catalogue seeds
    are merged with supplied URLs; domain policy and the total-source cap are checked
    before planning can spend a model request. Designed runs record their controller
    version here; loading an older run never silently opts it into a new policy.

    Args:
        state: Graph-state dictionary containing the validated ResearchRequest under
            request; no session exists yet.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    request = state["request"]
    sources = load_sources(request.urls_file, request.document_paths)
    from projectclarity.discovery.observed_link_inventory import canonical_url
    from projectclarity.discovery.url_policy import url_denial
    known = {canonical_url(s.url) for s in sources if s.kind == "url"}
    for url in request.default_source_urls:
        normalized = canonical_url(url)
        if not normalized:
            raise ResearchError("Invalid default source URL")
        if normalized not in known:
            sources.append(URLSource(source_id=f"S{len(sources) + 1}", url=normalized))
            known.add(normalized)
    if len(sources) > request.max_sources:
        raise ResearchError("Supplied sources exceed --max-sources")
    for source in sources:
        if source.kind == "url" and (reason := url_denial(source.url, request.url_policy)):
            raise ResearchError(f"Source {source.source_id} blocked by URL policy: {reason}")
    session = ResearchSession(
        session_id=state.get("run_id") or uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        request=state["request"],
        mode="plan_only" if request.plan_only else "plan_retrieve_extract" if request.extract_knowledge else "plan_and_retrieve",
        sources=sources,
        visited_nodes=["initialize_research"],
    )
    if request.design_answer:
        from projectclarity.nodes.schedule_work.answer_controller import POLICY
        session.workflow['controller_policy'] = POLICY
    # A unique directory isolates runs; no existing output is overwritten.
    (output_root / session.session_id).mkdir(parents=True, exist_ok=False)
    save_session(session, output_root)
    if monitor:
        monitor.start(session)
    return {"session": session}

