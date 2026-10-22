"""Define the data contracts exchanged by the CLI, graph, and LLM adapter.

Purpose:
    Specify what information a request, source, plan, or saved run must contain.
    These are Python models used to reject missing, unexpected, or wrongly typed
    fields. This code tries to ensure the response we get back from an LLM is valid.
    For example, a source is either a web address or a local file path,
    and each proposed research step must point to goals that actually exist.
    ResearchPlan also supplies the JSON schema sent to the LLM: it is the actual
    definition of the expected answer, not just a description in the prompt.
Responsibilities:
    ResearchRequest/Budget describe user input and application-owned limits.
    ResearchPlan, Goal, and StrategyStep define the actual LLM output schema.
    Source, Usage, and ResearchSession describe local state and saved snapshots.
Typical use:
    command_line.py constructs a ResearchRequest; agentic_workflow.py creates and updates a
    ResearchSession. research_planner.py passes ResearchPlan to with_structured_output(),
    which generates the JSON schema sent separately alongside the prompt.
Boundary:
    Validation checks structure and goal links, not whether a plan is useful or
    true. Source-ID membership is checked against the session in agentic_workflow.py.
    This module performs no I/O, model calls, or research execution.

Maintenance gotcha:
    Pydantic includes model class docstrings and Field descriptions in generated
    JSON schemas. Changing those can change what the LLM receives; treat that as
    a contract/prompt change. Ordinary # comments are developer-only explanations.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError


DEFAULT_MODEL = "claude-haiku-5-5"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")]


class Contract(BaseModel):
    """Reject unknown fields and implicit type conversions at our boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class Budget(Contract):
    """Application-owned limits; the planner never writes or enlarges these."""

    model_config = ConfigDict(frozen=True)
    max_requests: int = Field(default=1, ge=0, le=30)
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)


class FetchLimits(Contract):
    """Operator-selected retrieval bounds, independent of LLM token budgets.

    CLI options can reduce these development ceilings, not bypass the parser's
    limits or enlarge a run without bound. These settings are saved with the
    request; the model cannot change them. Robots delays still take precedence.
    """

    model_config = ConfigDict(frozen=True)
    max_source_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024)
    max_total_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=25 * 1024 * 1024)
    max_http_requests: int = Field(default=50, ge=0, le=50)
    max_run_seconds: float = Field(default=180.0, gt=0, le=180.0)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=10.0)


class ResearchRequest(Contract):
    """Preserve the original question separately from the LLM's interpretation."""

    question: str = Field(min_length=1, max_length=4000)
    urls_file: str | None = None
    # These are user-supplied references, not document text. Initialization
    # resolves relative paths from the directory where the command was launched.
    document_paths: list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]] = Field(default_factory=list, max_length=25)
    # The CLI sets this from --plan-only. Python callers default to no retrieval.
    plan_only: bool = True
    # Explicit opt-in: retrieved text may be sent to the configured LLM only
    # when this is true. It does not authorize searches or accepted publication.
    extract_knowledge: bool = False
    # Review and discovery are separate permissions, not implied by extraction.
    review_claims: bool = False
    follow_links: bool = False
    max_followup_sources: int = Field(default=2, ge=1, le=3)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)
    budget: Budget = Field(default_factory=Budget)
    fetch_limits: FetchLimits = Field(default_factory=FetchLimits)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        """Reject whitespace-only input without rewriting the user's original wording."""
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value

    @model_validator(mode="after")
    def check_extraction_mode(self) -> Self:
        """Reject contradictory flags and an extraction request with no call allowance."""
        if self.extract_knowledge and self.plan_only:
            raise PydanticCustomError("extraction_plan_only", "Extraction cannot be combined with plan-only mode")
        if self.extract_knowledge and self.budget.max_requests < 2:
            raise PydanticCustomError("extraction_budget", "Extraction needs --max-requests of at least 2")
        if self.review_claims and (not self.extract_knowledge or self.budget.max_requests < 3):
            raise PydanticCustomError("review_budget", "Claim review needs --extract and --max-requests >= 3")
        if self.follow_links and (not self.review_claims or self.budget.max_requests < 6):
            raise PydanticCustomError("discovery_budget", "Link following needs --review-claims and --max-requests >= 6")
        return self


class URLSource(Contract):
    """A supplied web address; registration does not fetch or verify its content."""

    source_id: Identifier
    kind: Literal["url"] = "url"
    url: str = Field(min_length=1, max_length=2048)
    # Only Python adds discovery ancestry. A model selects link IDs, never URLs.
    discovered_from_link: str | None = None
    depth: int = Field(default=0, ge=0, le=1)


class FileSource(Contract):
    """A local document reference; its resolved path stays in local saved state."""

    source_id: Identifier
    kind: Literal["file"] = "file"
    path: str = Field(min_length=1, max_length=4096)


# The kind field tells Pydantic which record to validate. A file cannot quietly
# masquerade as a URL, and neither record may carry the other's location field.
# Path existence and URL syntax are checked during initialization, not here.
Source = Annotated[URLSource | FileSource, Field(discriminator="kind")]


class Goal(Contract):
    """One answerable research question and the evidence it would require."""

    goal_id: Identifier
    question: Text
    evidence_required: list[Text] = Field(min_length=1, max_length=6)


class StrategyStep(Contract):
    """Proposed work only: these descriptions and searches are never executed."""

    action: Text
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    source_ids: list[Identifier] = Field(max_length=25)
    proposed_searches: list[Text] = Field(max_length=5)


class ResearchPlan(Contract):
    """The schema requested from the LLM; excludes budgets and executable tools."""

    # These fields are the complete output contract, not a sample JSON layout.
    # Prompt requests for extra structured fields require a schema change here.
    objective: Text
    # The prompt already asks what kind of question this is. A required field
    # gives that answer a place in JSON and the readable plan. Keep the label
    # open-ended while we learn which categories are useful; it is not a tool
    # selector, permission, or verified fact about the research topic.
    question_category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] = Field(
        description="A short label describing the kind of research question, not its topic; for example, entity discovery, comparison, explanation, or trend analysis. This is the planner's interpretation for human review, not an execution instruction."
    )
    scope_in: list[Text] = Field(min_length=1, max_length=8)
    scope_out: list[Text] = Field(max_length=8)
    goals: list[Goal] = Field(min_length=1, max_length=8)
    strategy: list[StrategyStep] = Field(min_length=1, max_length=10)
    assumptions: list[Text] = Field(max_length=8)
    # In execution mode, a nonempty list pauses the graph for human review.
    # Plan-only mode just displays the questions. "Does this person's work fit?"
    # is research to perform, not a question the user must answer before we start.
    # This description guides the model; Python cannot verify that distinction
    # from a plain string and does not silently discard questions by wording.
    clarifications: list[Text] = Field(max_length=8, description=(
        "Questions requiring user input before research can start, with no reasonable working "
        "assumption. Questions answerable during or after research belong in goals and strategy. "
        "Return an empty list when no blockers remain."
    ))

    @model_validator(mode="after")
    def check_goal_links(self) -> Self:
        """Well-formed JSON can still contain broken links between plan sections."""
        goal_ids = {goal.goal_id for goal in self.goals}
        if len(goal_ids) != len(self.goals):
            # Named error codes make a failed reply diagnosable without copying
            # its private contents. These enforce exactly the same link rules.
            raise PydanticCustomError("duplicate_goal_ids", "Goal identifiers must be unique")
        referenced = {goal_id for step in self.strategy for goal_id in step.goal_ids}
        if referenced - goal_ids:
            raise PydanticCustomError("strategy_unknown_goals", "Strategy must reference existing goals")
        if goal_ids - referenced:
            raise PydanticCustomError("strategy_uncovered_goals", "Strategy must cover every goal")
        return self


class Usage(Contract):
    """Attempt counts are local; token counts are provider-reported, when known."""

    requests_attempted: int = Field(default=0, ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    # Token totals cover all attempted model tasks when usage is known, but
    # these model/stop fields describe the most recent received response.
    response_model: str | None = None
    stop_reason: str | None = None
    # Legacy field: planning time only, NOT total run time. Extraction durations
    # live on each ExtractionAttempt; operational monitoring has its own clock.
    elapsed_seconds: float | None = None


class RetrievedDocument(Contract):
    """The receipt for one source: saved text and where it came from, or a failure.

    A successful receipt means text was acquired, not that its claims are true.
    Hashes identify the exact raw bytes and normalized text saved for inspection.
    Paths below are relative to the run directory, so a run can be moved intact.
    """

    source_id: Identifier
    source_kind: Literal["url", "file"]
    original_location: str
    status: Literal["retrieved", "failed", "cancelled"] = "failed"
    attempted_at: str
    acquired_at: str | None = None
    final_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    content_type: str | None = None
    raw_sha256: str | None = None
    text_sha256: str | None = None
    byte_count: int = 0
    character_count: int = 0
    raw_path: str | None = None
    text_path: str | None = None
    parser: str | None = None
    # Observed HTML anchors, not approved fetch targets or verified evidence.
    links: list[dict[str, str]] = Field(default_factory=list)
    links_truncated: bool = False
    elapsed_seconds: float | None = None
    error_code: str | None = None
    error: str | None = None


class ResearchSession(Contract):
    """Inspectable run state; LangGraph checkpoints separately persist execution."""

    # 1.7 adds opt-in review/discovery and ancestry/call receipts. Older snapshots
    # remain readable and do not opt into any new model calls or link following.
    schema_version: Literal["1.5", "1.6", "1.7"] = "1.7"
    session_id: str
    created_at: str
    request: ResearchRequest
    sources: list[Source] = Field(max_length=25)
    mode: Literal["plan_only", "plan_and_retrieve", "plan_retrieve_extract"] = "plan_only"
    # v4 consolidates the guidance into SYSTEM_PROMPT; v2/v3 used an appended
    # context block. Keep old labels when loading earlier review checkpoints.
    # This is a manually maintained contract revision, not an automatic prompt hash.
    prompt_version: Literal["research-plan-v1", "research-plan-v2", "research-plan-v3", "research-plan-v4"] = "research-plan-v4"
    status: Literal["initialized", "planning", "plan_ready", "needs_clarification", "awaiting_review", "rejected", "retrieving", "sources_retrieved", "retrieval_partial", "retrieval_failed", "extracting", "candidates_saved", "extraction_partial", "extraction_failed", "failed", "cancelled"] = "initialized"
    usage: Usage = Field(default_factory=Usage)
    visited_nodes: list[str] = Field(default_factory=list)
    plan: ResearchPlan | None = None
    # JSON-shaped copies avoid a circular dependency with ResearchPlan. Their
    # full typed contracts are validated by plan_review_contracts/service.
    pending_review: dict | None = None
    review_decision: dict | None = None
    review_resolved_at: str | None = None
    documents: list[RetrievedDocument] = Field(default_factory=list)
    retrieval_usage: dict[str, int | float] = Field(default_factory=dict)
    # JSON-shaped at the checkpoint boundary, fully validated as ExtractionRecord
    # inside extraction/. Never contains an SDK response or credential.
    extraction: dict | None = None
    # Typed by claim_review/discovery at node boundaries; no provider objects.
    claim_reviews: dict = Field(default_factory=dict)
    processing_calls: list[dict] = Field(default_factory=list)
    discovery: dict = Field(default_factory=dict)
    error: str | None = None
