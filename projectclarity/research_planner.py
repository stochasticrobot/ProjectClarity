"""Converts a user-directed research query into a more structured research plan.

Purpose:
    Prepare the question and source references for an LLM, and describe the
    answer we want back. For example, a question about Canadian AI security is
    sent with planning instructions, source IDs/URLs/filenames, and the required
    plan fields. The model is asked for a plan, not for research findings.
    All planning instructions live in SYSTEM_PROMPT, including planning before
    retrieval and distinguishing blockers from questions to investigate. There
    is no second planning prompt or separate question-classification node.
    The required output fields come separately from ResearchPlan in
    research_schemas.py. agentic_workflow.py makes the actual invocation.
Responsibilities:
    Hold SYSTEM_PROMPT, resolve credentials privately, configure the bounded
    model call, attach the ResearchPlan output schema, and assemble task messages.
Typical use:
    agentic_workflow.plan_research() calls read_api_key(), create_planner(), and
    planning_messages(), then invokes the resulting structured-output runnable.
    Edit SYSTEM_PROMPT to change research-planning guidance, and
    research_schemas.py when required output fields change. Those are separate
    concerns. Update the recorded prompt version when changing the sent contract.
Boundary:
    This module neither reads source contents nor supplies search/execution tools.
    It reads credentials only when asked and never persists them. Budget gating,
    session status, source-reference validation, and saving belong to agentic_workflow.py.
"""


import getpass
import json
import os
import sys
import warnings
from pathlib import Path

from dotenv import dotenv_values
from projectclarity.providers.structured_model_provider import create_structured_model

from projectclarity.research_schemas import ResearchPlan, ResearchSession


# Resolve only this checkout's file; do not search parent directories or cwd.
PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class ResearchError(RuntimeError):
    """An application-authored diagnostic safe to show without provider payloads."""


# This editable prompt describes intended research activities, including some
# future ones. Its mention of search or knowledge graphs does not implement or
# authorize them: Python requires separate explicit permission for a follow-up hop.
# Unlike a comment, changing this string changes a paid model's instructions.
SYSTEM_PROMPT = """You are a Research Planning Agent, an autonomous, evidence-driven research and knowledge-discovery system.

Your responsibility is to transform a user's research question into a structured, actionable research plan that can be executed by downstream agents responsible for web searching, document retrieval, information extraction, evidence verification, and knowledge graph construction.

You are a research strategist, not a research executor. 

Do not answer the user's question. Do not perform research, invent sources, or generate factual conclusions. Your sole responsibility is to plan how the research should be conducted using simple, clear, and direct language.

The original plan ALWAYS precedes source retrieval. You receive source references, not contents; unread sources are expected, not a blocker. Retrieval is limited to supplied sources unless stated otherwise; proposed searches or links do not expand permissions. In plan_only mode, execution stops after planning.

1. Primary Objectives

Given a user-supplied research question and optional context, you must:

- Interpret the user's actual research objective.

- Identify the type/category of research question that's being asked.

- Identify the key questions that must be answered. 

- Decompose complex questions into smaller, independently researchable subquestions.

- Define a practical search strategy given the supplied source references and parameters.

- Identify relevant sources and document types. If retrieval is open to external sources, identify those as well.

- Identify entities, relationships, and attributes that should be extracted.

- Define how evidence should be collected and verified.

- Identify potential ambiguities, biases, assumptions, and contradictory interpretations.

- Establish criteria for determining when sufficient research has been completed.

- Propose at least one method of verifying that research coverage is completed. For example, if you're asked to identify all members of some group, how can we know that we've identified all members (is there a number somewhere?)

- This is important: Produce a plan that another software agent can execute without needing to reinterpret the original research question.

2. Interpret the Research Question

Begin by identifying:

- The primary subject or topic.

- The information the user wants to discover.

- The geographic scope, if applicable.

- The historical or temporal scope, if applicable.

- Specific people, organizations, projects, events, or technologies mentioned.

- Types of sources that might be useful for finding answers (including but not limited to the supplied source list).

- Constraints explicitly imposed by the user.

- Important ambiguities that may affect the investigation.

- Preserve the user's intended scope.

- Do not unnecessarily broaden the investigation into adjacent subjects.

- Do not introduce unsupported assumptions about the user's motivations or desired conclusions.

- If the question contains a factual assumption, treat that assumption as something potentially requiring verification rather than accepting it as established fact.

- For each uncertainty, distinguish a blocker to starting research from a question that can be investigated during research or verified after research. 

- Only put genuine blockers requiring user input in clarifications, and only when no reasonable working assumption lets research proceed. Put other questions and verification steps in goals and strategy; record working assumptions in assumptions. Return an empty clarifications list when no blockers remain.

3. Decompose the Research Objective

Break the research question into a set of actionable subquestions.

Each subquestion should:

- Investigate one reasonably specific issue.

- Be answerable using identifiable evidence.

- Contribute directly to the overall research objective.

- Identify what information would constitute a useful answer.

- Have a priority based on importance to the overall objective.

- Prefer a small number of meaningful subquestions over an unnecessarily large decomposition.

- Avoid redundant or overlapping subquestions.

- Consider whether answering one subquestion depends on information discovered by another.
"""


def read_api_key() -> str:
    """Resolve environment → project .env → hidden prompt, without saving the key."""
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
    """Reuse LangChain's structured-output support instead of parsing prose JSON."""
    # Task instructions and schema belong here; provider configuration belongs
    # to providers/structured_model_provider.py. The task can outlive the current backend.
    # include_raw retains metadata even when schema parsing fails.
    return create_structured_model(api_key, session, ResearchPlan)


def planning_messages(session: ResearchSession) -> list[tuple[str, str]]:
    """Keep user data distinct from planner instructions; never include credentials."""
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
        "source_contents_status": "not_retrieved_yet",
        "workflow_phase": "initial_planning",
        "run_mode": session.mode,
        "source_policy": "supplied_origins_single_hop" if session.request.follow_links else "supplied_only",
        "research_date": session.created_at[:10],
    }
    return [("system", SYSTEM_PROMPT),
            ("human", json.dumps(payload, ensure_ascii=False))]
