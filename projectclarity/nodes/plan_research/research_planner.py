"""Converts a user-directed research query into a more structured research plan.

Purpose:
    Prepare the question and source references for an LLM, and describe the
    answer we want back. For example, a question about Canadian AI security is
    sent with planning instructions, source IDs/URLs/filenames, and the required
    plan fields. The model is asked for a plan, not for research findings.
    Planning instructions live in prompts/research_planner.txt and are loaded as
    SYSTEM_PROMPT, including planning before
    retrieval and distinguishing blockers from questions to investigate. There
    is no second planning prompt or separate question-classification node.
    The required output fields come separately from ResearchPlanOutput in
    research_schemas.py, including explicit completion criteria. ResearchPlan
    remains the persisted representation and accepts older plans without those
    criteria. node.py makes the actual planning invocation.
Responsibilities:
    Load the user-owned SYSTEM_PROMPT, resolve credentials privately, configure the bounded
    model call, attach the ResearchPlanOutput schema, and assemble task messages.
Typical use:
    nodes.plan_research.node.plan_research() calls read_api_key(), create_planner(), and
    planning_messages(), then invokes the resulting structured-output runnable.
    The user edits prompts/research_planner.txt to change planning guidance;
    research_schemas.py defines required output fields. Those are separate
    concerns. Update the recorded prompt version when changing the sent contract.
Boundary:
    This module neither reads source contents nor supplies search/execution tools.
    It reads credentials only when asked and never persists them. Budget gating,
    session status and saving belong to node.py; initial source validation belongs to the initialize_research node.
"""


import getpass
import json
import os
import sys
import warnings
from pathlib import Path

from dotenv import dotenv_values
from projectclarity.providers.structured_model_provider import create_structured_model

from projectclarity.research_schemas import ResearchPlan, ResearchPlanOutput, ResearchSession


# Resolve only this checkout's file; do not search parent directories or cwd.
PROJECT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class ResearchError(RuntimeError):
    """An application-authored diagnostic safe to show without provider payloads."""


# The user-owned file describes a planning task, not an autonomous executor.
# Source permissions are supplied as task constraints; Python enforces them.
# Load verbatim; application repairs must not rewrite its instructions.
from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('research_planner.txt')


def read_api_key() -> str:
    """Resolve environment → project .env → hidden prompt, without saving the key.

    Args:
        None.

    Returns:
        The nonempty API-key string from the environment, local .env or hidden prompt;
        missing/unsafe input raises an error.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    # Parse data, never source/execute a shell file. Read only the required key,
    # do not export other settings, and keep ${...} values literal. A secret
    # injected by the runtime takes precedence over this local development file.
    try:
        if PROJECT_ENV_FILE.exists():
            if not PROJECT_ENV_FILE.is_file():
                raise ResearchError("Project .env must be a regular text file.")
            values = dotenv_values(PROJECT_ENV_FILE, interpolate=False, encoding="utf-8")
            key = (values.get("ANTHROPIC_API_KEY") or "").strip()
            if key:
                return key
    except (OSError, UnicodeError):
        raise ResearchError("Cannot read the project .env file. Check its permissions and UTF-8 encoding.") from None
    if not sys.stdin.isatty():
        raise ResearchError("Set ANTHROPIC_API_KEY in the project .env or environment, or use an interactive terminal for hidden key entry.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Anthropic API key (hidden; not saved): ").strip()
    except getpass.GetPassWarning:
        raise ResearchError("This terminal cannot hide input; no key was read.") from None
    if not key:
        raise ResearchError("No API key supplied.")
    return key


def create_planner(api_key: str, session: ResearchSession):
    """Configure the planning adapter to require ResearchPlanOutput so downstream nodes
    receive structured goals, strategies and completion criteria rather than prose.

    Reuse the shared provider adapter; the graph owns request reservation, invocation
    and failure handling. Constructing this runnable does not spend a model request.

    Args:
        api_key: Transient model-provider credential; used to configure the client, not
            saved as research data.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        A configured runnable expecting ResearchPlanOutput, including explicit completion
        criteria; it has not been invoked.
    """
    # Task instructions and schema belong here; provider configuration belongs
    # to providers/structured_model_provider.py. The task can outlive the current backend.
    # include_raw retains metadata even when schema parsing fails.
    return create_structured_model(api_key, session, ResearchPlanOutput)


def planning_messages(session: ResearchSession) -> list[tuple[str, str]]:
    """Build the planner's system and task messages so it can turn the original question
    into a scoped plan using only the registered source references and allowed stages.

    Keep user data separate from instructions. Include source policy, concrete source
    constraints, planning ceilings and the research date, but not execution-mode flags,
    credentials or unread document bodies.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        Ordered system and human message pairs containing the question, input references and
        planning constraints, not source bodies.
    """
    # The planner needs references it can assign to a strategy, not access to
    # local files. Send only the filename for a document; the absolute path in
    # session.json is for the later retrieval node. S1/S2 disambiguate equal names.
    source_descriptions = []
    for source in session.sources:
        description = {"source_id": source.source_id, "kind": source.kind}
        if source.kind == "url":
            description["url"] = source.url
        else:
            description["label"] = Path(source.path).name
        source_descriptions.append(description)
    payload = {
        "original_question": session.request.question,
        "supplied_sources": source_descriptions,
        "source_contents_available": False,
        "source_policy": "web_search_and_observed_links" if session.request.web_search and session.request.follow_links else "web_search" if session.request.web_search else "observed_links" if session.request.follow_links else "supplied_only",
        "source_constraints": {"total_sources": session.request.max_sources,
            "observed_links_same_origin_only": session.request.same_origin_links if session.request.follow_links else None,
            "allowed_domains": session.request.url_policy.allowed_domains if session.request.url_policy.whitelist_only else None},
        "search_limits": {"queries": session.request.max_search_queries,
                          "new_sources": min(max(0, session.request.max_sources - len(session.sources)),
                              session.request.max_search_sources or session.request.max_sources)} if session.request.web_search else None,
        "research_date": session.created_at[:10],
    }
    return [("system", SYSTEM_PROMPT),
            ("human", json.dumps(payload, ensure_ascii=False))]
