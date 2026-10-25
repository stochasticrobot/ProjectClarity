"""Define the data contracts exchanged by the CLI, graph, and LLM adapter.

Purpose:
    Specify what information a request, source, plan, or saved run must contain.
    These are Python models used to reject missing, unexpected, or wrongly typed
    fields. This code tries to ensure the response we get back from an LLM is valid.
    For example, a source is either a web address or a local file path,
    and each proposed research step must point to goals that actually exist.
    ResearchPlanOutput supplies the planner's JSON schema, including required
    completion criteria with schema-enforced count/basis branches, and
    InitialScopeEstimate with an optional numeric range.
    Its to_plan() conversion supplies evidence metadata before workflow use;
    the model does not provide basis or candidate_ids during initial planning.
    ResearchPlan is the persisted form and can still read
    older saved plans without criteria. The schema defines actual fields rather
    than relying on instructions in the prompt to create them.
Responsibilities:
    ResearchRequest/Budget describe user input and application-owned limits.
    ResearchPlanOutput, Goal, StrategyStep and CompletionCriterion define the
    structured planning reply and how the proposed research will measure coverage.
    Source, Usage, and ResearchSession describe local state and saved snapshots.
Typical use:
    command_line.py constructs a ResearchRequest; agentic_workflow.py creates and updates a
    ResearchSession. research_planner.py configures the adapter with ResearchPlanOutput,
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
# Model-local labels are dictionary references, never paths or database IDs.
LocalReference = Annotated[str, StringConstraints(min_length=1)]


class Contract(BaseModel):
    """Reject unknown fields and implicit type conversions at our boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class Budget(Contract):
    """Application-owned limits; the planner never writes or enlarges these."""

    model_config = ConfigDict(frozen=True)
    # CLI research modes choose a larger default; saved/API requests retain their
    # explicit allowance. No node may enlarge this immutable budget during work.
    max_requests: int = Field(default=1, ge=0, le=1000)
    max_output_tokens: int = Field(default=2048, ge=128, le=8192)


class FetchLimits(Contract):
    """Operator-selected retrieval bounds, independent of LLM token budgets.

    CLI options select crawl-scale allowances within hard ceilings. Legacy/API
    defaults remain unchanged. These settings are saved with the request; the
    model cannot change them. Per-file parser bounds and robots delays remain.
    """

    model_config = ConfigDict(frozen=True)
    max_source_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024)
    max_total_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    max_http_requests: int = Field(default=50, ge=0, le=10000)
    # Preserve saved/API defaults; new CLI runs choose 1800 seconds because the
    # acquisition clock includes model work between retrieval rounds.
    max_run_seconds: float = Field(default=180.0, gt=0, le=7200.0)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=10.0)


class URLPolicy(Contract):
    """Frozen operator domain rules; deny wins and subdomains inherit their parent rule."""

    blocked_domains: list[str] = Field(default_factory=list)
    # A catalogue domain is not permission to bypass denies/public-network checks.
    # This list constrains acquisition only when whitelist_only is enabled.
    allowed_domains: list[str] = Field(default_factory=list)
    whitelist_only: bool = False


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
    # Old saved requests default off, so resuming them never silently adds calls.
    # New CLI runs require an explicit auxiliary capture allowance.
    accumulate_knowledge: bool = False
    # None preserves historical shared-budget behavior; new CLI requests use zero
    # unless an auxiliary allowance is explicitly selected. Never raises max_requests.
    knowledge_max_requests: int | None = Field(default=None, ge=0, le=1000)
    # None preserves exhaustive capture for saved/API runs. New discovery runs
    # choose a scheduling quantum, not a total capture allowance.
    knowledge_calls_per_round: int | None = Field(default=None, ge=1)
    # Review and discovery are separate permissions, not implied by extraction.
    review_claims: bool = False
    follow_links: bool = False
    # Batch size per selection round, not total links or allowed traversal depth.
    max_followup_sources: int = Field(default=2, ge=1)
    # Saved/API requests retain old scope; the CLI explicitly selects open recursive links.
    max_link_depth: int | None = Field(default=1, ge=1, le=100)
    # Legacy/API runs keep their recorded traversal policy; new CLI runs choose 3.
    max_unproductive_depth: int | None = Field(default=None, ge=1, le=100)
    # New CLI runs explore two relative link levels before search. Saved/API
    # requests without this field keep their original scheduling policy.
    explore_before_search_depth: int | None = Field(default=None, ge=1, le=100)
    same_origin_links: bool = True
    # Counts registered pages/documents, not domains. New CLI default is 250;
    # saved/API requests retain 25 unless explicitly configured otherwise.
    max_sources: int = Field(default=25, ge=1, le=1000)
    # The CLI loads repository policy files before constructing this snapshot.
    # Bare API/legacy requests do not implicitly read a filesystem blacklist.
    url_policy: URLPolicy = Field(default_factory=URLPolicy)
    # Only populated by CLI --use-default-sources; these merge with supplied URLs.
    default_source_urls: list[str] = Field(default_factory=list, max_length=25)
    search_provider: Literal["duckduckgo", "brave", "google", "yahoo", "mojeek", "startpage", "wikipedia"] = "duckduckgo"
    # Legacy/API requests remain single-provider with no new delayed calls.
    search_fallbacks: list[Literal["duckduckgo", "brave", "google", "yahoo", "mojeek", "startpage", "wikipedia"]] = Field(default_factory=list, max_length=7)
    search_cooldown_seconds: float = Field(default=0, ge=0, le=3600)
    search_recovery_rounds: int = Field(default=0, ge=0, le=3)
    search_min_interval_seconds: float = Field(default=3.0, ge=0, le=60)
    search_retries: int = Field(default=1, ge=0, le=3)
    web_search: bool = False
    assess_coverage: bool = False
    # Preserve legacy/API defaults. New CLI runs can share run budgets without
    # an extra query-count partition; explicit caps remain cumulative.
    max_search_queries: int | None = Field(default=4, ge=1)
    # None shares max_sources; explicit historical/operator caps remain binding.
    max_search_sources: int | None = Field(default=5, ge=1, le=1000)
    search_results_per_query: int = Field(default=5, ge=1, le=10)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)
    budget: Budget = Field(default_factory=Budget)
    fetch_limits: FetchLimits = Field(default_factory=FetchLimits)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        """Reject whitespace-only input without rewriting the user's original wording.

        Args:
            cls: ResearchRequest class supplied by Pydantic when validating the question field.
            value: Question text supplied when constructing a research request.

        Returns:
            The original question string when nonblank; whitespace-only input raises ValueError.
        """
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value

    @model_validator(mode="after")
    def check_extraction_mode(self) -> Self:
        """Reject incompatible stage flags and insufficient request budgets before the graph
        starts, avoiding runs that cannot perform their requested stages.

        Check search selection, extraction, claim review, link discovery and coverage
        requirements together. Plan-only mode cannot also extract documents.

        Args:
            self: Request model being validated for compatible execution flags and resource
                limits.

        Returns:
            This request after cross-option validation; incompatible stage flags or limits raise
            ValueError.
        """
        if self.accumulate_knowledge and not self.extract_knowledge:
            raise ValueError("Knowledge accumulation requires extraction mode")
        if self.web_search and not self.plan_only and self.budget.max_requests < 2:
            raise ValueError("Web search needs --max-requests >= 2 for planning and source selection")
        if self.assess_coverage and (not self.extract_knowledge or self.budget.max_requests < 3):
            raise ValueError("Coverage assessment needs --extract and --max-requests >= 3")
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
    discovered_from_search: str | None = None
    depth: int = Field(default=0, ge=0, le=1000)


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
    """A research action with goal references, supplied source IDs and proposed queries."""

    action: Text
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    source_ids: list[Identifier] = Field(max_length=25)
    # Proposals are queued work, not network requests. Execution budgets govern
    # how many run; a sixth useful query must not invalidate the entire plan.
    proposed_searches: list[Text]


class CompletionCriterion(Contract):
    """A planned test of sufficiency, not a claim that the answer is already known."""

    criterion_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    description: Text
    kind: Literal["enumeration", "constraints", "evidence_sufficiency"]
    measurement_method: Text
    target_basis: Literal["research", "user_request", "not_applicable"]
    target_count: int | None = Field(default=None, ge=0, le=1000000)
    limitations: list[Text] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def check_target(self) -> Self:
        """A planner may copy a requested count, never invent a researched denominator.

        Args:
            self: Criterion model being validated for a consistent target basis and count.

        Returns:
            This criterion after checking target basis and count; researched totals cannot be
            invented in the plan. Invalid combinations raise the safe diagnostic codes
            unverified_completion_target or missing_requested_target.
        """
        if self.target_basis != "user_request" and self.target_count is not None:
            raise PydanticCustomError("unverified_completion_target", "Only a user-requested count can be set before research")
        if self.target_basis == "user_request" and self.target_count is None:
            raise PydanticCustomError("missing_requested_target", "A user-requested target needs a count")
        return self


class RequestedCompletionTarget(Contract):
    """Copy a numerical completion target explicitly requested by the user, not an estimated total."""

    requested_count: int = Field(ge=0, le=1000000,
        description="The count explicitly requested in the question or user guidance. Do not invent a quota or copy a provisional scope estimate.")


class InitialCompletionCriterion(Contract):
    """Define an evidence sufficiency test and, only if requested, a numerical target."""

    criterion_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    description: Text
    kind: Literal["enumeration", "constraints", "evidence_sufficiency"]
    measurement_method: Text
    target: Literal["research", "not_applicable"] | RequestedCompletionTarget = Field(
        description="Use research when the total must be established from evidence; not_applicable for nonnumeric sufficiency. Supply a requested_count object only for an explicit user-requested count.")
    limitations: list[Text] = Field(max_length=6)

    def to_criterion(self) -> CompletionCriterion:
        """Derive stored target metadata from the single model judgment.

        Args:
            self: Validated initial criterion with a nonnumeric target or requested count.

        Returns:
            A stored CompletionCriterion preserving content and the requested count.
            Python supplies the corresponding basis and null count when applicable;
            stored validators still apply. No I/O or model calls occur.
        """
        requested = isinstance(self.target, RequestedCompletionTarget)
        return CompletionCriterion(**self.model_dump(exclude={"target"}),
            target_basis="user_request" if requested else self.target,
            target_count=self.target.requested_count if requested else None)


class ScopeEstimate(Contract):
    """A revisable ballpark, not a verified denominator or permission to stop research."""

    estimate_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    unit: Text
    lower: int | None = Field(ge=0, le=1000000000000)
    upper: int | None = Field(ge=0, le=1000000000000)
    scale: Literal["small", "medium", "large", "very_large", "unknown"]
    basis: Literal["provisional_prior", "evidence_informed", "unknown"]
    confidence: Literal["low", "medium", "high"]
    method: Text
    rationale: Text
    assumptions: list[Text] = Field(max_length=8)
    limitations: list[Text] = Field(max_length=8)
    candidate_ids: list[str] = Field(max_length=40)

    @model_validator(mode="after")
    def check_range(self) -> Self:
        """Reject inconsistent ranges and unsupported evidence labels before saving estimates.

        Args:
            self: Proposed scope range, method, basis and evidence references.

        Returns:
            This estimate if both endpoints are absent or ordered and its evidence label
            matches its references; otherwise raise a validation error with a distinct
            range, evidence or unknown-status diagnostic code. No calibration is implied.
        """
        if (self.lower is None) != (self.upper is None):
            raise PydanticCustomError("incomplete_scope_range", "Scope range needs both endpoints or neither")
        if self.lower is not None and self.lower > self.upper:
            raise PydanticCustomError("reversed_scope_range", "Scope range endpoints are reversed")
        if self.basis == "evidence_informed" and not self.candidate_ids:
            raise PydanticCustomError("missing_scope_evidence", "Evidence-informed scope requires candidate references")
        if self.basis != "evidence_informed" and self.candidate_ids:
            raise PydanticCustomError("unexpected_scope_evidence", "Referenced scope evidence must be labelled evidence_informed")
        if self.basis == "unknown" and (self.lower is not None or self.scale != "unknown"):
            raise PydanticCustomError("inconsistent_unknown_scope", "Unknown scope cannot assert a numeric range or size band")
        return self


class PlanningScopeRange(Contract):
    """A numeric estimate with both endpoints; lower must not exceed upper."""

    lower: int = Field(ge=0, le=1000000000000, description="Lower end of the estimated range, no greater than upper.")
    upper: int = Field(ge=0, le=1000000000000, description="Upper end of the estimated range, no smaller than lower.")

    @model_validator(mode="after")
    def check_order(self) -> Self:
        """Reject reversed estimates rather than guessing which endpoint was intended.

        Args:
            self: Parsed numeric range proposed during initial planning.

        Returns:
            This range unchanged when ordered; otherwise raise a validation error
            with the safe reversed_scope_range code. No values are repaired.
        """
        if self.lower > self.upper:
            raise PydanticCustomError("reversed_scope_range", "Scope range endpoints are reversed")
        return self


class InitialScopeEstimate(Contract):
    """A provisional estimate of research size, not a finding or completion target."""

    estimate_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    unit: Text
    range: PlanningScopeRange | None = Field(description="Estimated numeric range, or null when no defensible numeric range is available.")
    scale: Literal["small", "medium", "large", "very_large", "unknown"]
    confidence: Literal["low", "medium", "high"]
    method: Text
    rationale: Text
    assumptions: list[Text] = Field(max_length=8)
    limitations: list[Text] = Field(max_length=8)

    def to_scope_estimate(self) -> ScopeEstimate:
        """Add application-owned evidence metadata to an initial planning estimate.

        Args:
            self: Model-proposed estimate with an optional complete numeric range
                and an independently judged size band.

        Returns:
            A new persisted ScopeEstimate with empty candidate_ids. Its basis is
            unknown only when both the numeric range and size band are unknown;
            otherwise it is provisional_prior, never evidence_informed. Preserve
            all model judgments without inventing bounds or changing the scale.
        """
        return ScopeEstimate(**self.model_dump(exclude={"range"}),
            lower=self.range.lower if self.range is not None else None,
            upper=self.range.upper if self.range is not None else None,
            basis="unknown" if self.range is None and self.scale == "unknown" else "provisional_prior",
            candidate_ids=[])


class _ResearchPlanFields(Contract):
    """A scoped research plan with goals, evidence strategies and completion tests."""

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
    # Empty is reserved for loading old records. New planning calls reject it.
    completion_criteria: list[CompletionCriterion] = Field(default_factory=list, max_length=12,
        description="Explicit tests for sufficient research. Exact researched totals require evidence; provisional ballparks belong separately in scope_estimates.")
    scope_estimates: list[ScopeEstimate] = Field(default_factory=list, max_length=12)
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
        """Check relationships between plan sections so structurally valid JSON cannot send later
        nodes to nonexistent goals or omit planned work.

        Require unique goal and criterion IDs, strategy coverage of every goal, and valid goal
        references from strategies, completion criteria and initial scope estimates.
        Both initial model replies and persisted plans share these reference checks;
        their scope-estimate fields differ and are validated by their own contracts.

        Args:
            self: Plan model whose goals, strategies and completion-criterion references are
                being validated.

        Returns:
            This plan after validating goal IDs, strategy coverage and criterion links;
            duplicate IDs, missing strategy coverage or unknown goal references raise
            distinct safe validation codes. No research content is repaired or discarded.
        """
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
        criteria = [criterion.criterion_id for criterion in self.completion_criteria]
        if len(criteria) != len(set(criteria)):
            raise PydanticCustomError("duplicate_criterion_ids", "Completion criterion IDs must be unique")
        if any(set(c.goal_ids) - goal_ids for c in self.completion_criteria):
            raise PydanticCustomError("criterion_unknown_goals", "Completion criteria must reference existing goals")
        estimates = [e.estimate_id for e in self.scope_estimates]
        if len(estimates) != len(set(estimates)):
            raise PydanticCustomError("duplicate_estimate_ids", "Scope estimate IDs must be unique")
        if any(set(e.goal_ids) - goal_ids for e in self.scope_estimates):
            raise PydanticCustomError("estimate_unknown_goals", "Scope estimates must reference existing goals")
        return self


class ResearchPlan(_ResearchPlanFields):
    """A scoped research plan with goals, evidence strategies and completion tests."""

    @model_validator(mode="after")
    def check_initial_scope_metadata(self) -> Self:
        """Keep the stored original plan provisional even after later coverage revisions.

        Args:
            self: Persisted or human-reviewed research plan, including legacy plans.

        Returns:
            This plan unchanged unless an initial estimate claims evidence-informed
            status, in which case raise initial_scope_has_evidence. Coverage uses
            its separate assessment contract and may legitimately cite evidence.
        """
        if any(e.basis == "evidence_informed" for e in self.scope_estimates):
            raise PydanticCustomError("initial_scope_has_evidence", "Initial scope estimates precede retrieved evidence")
        return self


class ResearchPlanOutput(_ResearchPlanFields):
    """A research plan including explicit completion criteria and provisional scope estimates."""

    model_config = ConfigDict(title="ResearchPlan")
    # Branch only the small target value: duplicating the entire criterion made
    # the provider's compiled grammar exceed its internal complexity limit.
    completion_criteria: list[InitialCompletionCriterion] = Field(min_length=1, max_length=12)
    scope_estimates: list[InitialScopeEstimate] = Field(min_length=1, max_length=12)

    def to_plan(self) -> ResearchPlan:
        """Convert a planning reply to the stable saved-plan format before workflow use.

        Args:
            self: Validated initial planner reply; no source evidence has been read.

        Returns:
            A new ResearchPlan retaining all proposed research content, with Python
            supplying completion-target and scope evidence metadata, and flattening ranges.
            The saved-plan validators still apply; no file writes or model calls occur.
        """
        return ResearchPlan(**self.model_dump(exclude={"scope_estimates", "completion_criteria"}),
            completion_criteria=[criterion.to_criterion() for criterion in self.completion_criteria],
            scope_estimates=[estimate.to_scope_estimate() for estimate in self.scope_estimates])


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
    retry_after_seconds: float | None = None
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
    sources: list[Source] = Field(max_length=1000)
    mode: Literal["plan_only", "plan_and_retrieve", "plan_retrieve_extract"] = "plan_only"
    # v4 consolidates the guidance into SYSTEM_PROMPT; v2/v3 used an appended
    # context block. Keep old labels when loading earlier review checkpoints.
    # v8 separates initial planning estimates from evidence-based scope metadata.
    # v11 branches only the completion target to keep the provider grammar small.
    # This is a manually maintained contract revision, not an automatic prompt hash.
    prompt_version: Literal["research-plan-v1", "research-plan-v2", "research-plan-v3", "research-plan-v4", "research-plan-v5", "research-plan-v6", "research-plan-v7", "research-plan-v8", "research-plan-v9", "research-plan-v10", "research-plan-v11"] = "research-plan-v11"
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
    search: dict = Field(default_factory=dict)
    coverage: dict = Field(default_factory=dict)
    knowledge: dict = Field(default_factory=dict)
    source_value: dict = Field(default_factory=dict)
    # Application-only durable scheduler; not part of any model response schema.
    workflow: dict = Field(default_factory=dict)
    error: str | None = None
