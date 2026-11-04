"""Describe the order of the research steps and when the application should stop.

After the user asks a question, the application defines what an answer needs,
optionally asks for clarification, prepares searches, collects documents, extracts
facts, selects relevant evidence, reviews doubtful claims and prepares a report.
This file connects those jobs using LangGraph, the library that runs the steps in
order. The code that performs each individual job lives in its own file.

research_sessions.py starts these workflows. Setup can pause while waiting for
an answer from the user. Later steps continue after completed or partly successful
work, but stop after failure or cancellation. All workflow definitions are kept
here so a reader can find the sequence without following calls across web pages.
Restarting the application does not automatically repeat interrupted research.
"""
import json
import os
from typing import TypedDict

# Keep research content out of tracing services inherited from the environment.
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict

from projectclarity.model_access.model_client import request_for
from projectclarity.model_access.model_ledger import checked_call
from projectclarity.question_definition.requirements_definition import InformationItem, validate_plan, planning_request

class Clarifications(BaseModel):
    """Only questions for the human; an empty list needs no pause."""
    model_config = ConfigDict(extra='forbid', strict=True)
    questions: list[str]


class Definition(BaseModel):
    """Updated question and requirements compatible with the extraction planner."""
    model_config = ConfigDict(extra='forbid', strict=True)
    research_question: str
    information_needed: list[InformationItem]


class SetupState(TypedDict, total=False):
    """Persisted graph state for one question, including exact question-answer pairs."""
    session_id: str
    original_question: str
    research_question: str
    information_needed: list[dict]
    questions: list[str]
    answers: list[dict]


def build_setup_graph(caller, checkpointer):
    """Compile the actual sequential workflow and its one human-input branch.

    Args:
        caller: Callable accepting stage, request and session ID; exposes model.
        checkpointer: LangGraph saver that persists state across the human pause.
    Returns:
        Compiled graph. Definition and clarification calls happen once before a
        side-effect-free interrupt node; resume runs only the refinement call.
    """
    def define(state):
        """Use the existing approved planner without changing its contract.

        Args:
            state: Original question and owning session ID.
        Returns:
            Initial question and validated requirements; model/validation failures propagate.
        """
        request = planning_request(state['original_question'], caller.model, 4096)
        result = checked_call(caller,'define',request,state['session_id'],validate_plan).model_dump()
        return {'research_question': state['original_question'], **result}

    def clarify(state):
        """Identify material ambiguities and phrase at most two user questions.

        Args:
            state: Question and validated initial requirements.
        Returns:
            Validated questions. Excessive or blank questions fail rather than
            silently truncating user decisions. Performs one model call.
        """
        request = request_for('research_clarifications.txt',
                              {k: state[k] for k in ('research_question', 'information_needed')}, Clarifications, caller.model)
        def validate(text):
            """Validate both the schema and the at-most-two clarification contract.

            Args: text: Raw model output.
            Returns: Valid questions or ValueError; no list truncation or repairs.
            """
            result=Clarifications.model_validate_json(text)
            if len(result.questions)>2 or any(not q.strip() or len(q)>2000 for q in result.questions):
                raise ValueError('Expected at most two nonblank clarification questions.')
            return result
        result=checked_call(caller,'clarify',request,state['session_id'],validate)
        return result.model_dump()

    def route(state):
        """Choose the only optional branch without estimating research completeness.

        Args:
            state: Validated clarification list.
        Returns:
            Human-pause node name when needed, otherwise the graph end marker.
        """
        return 'answer' if state['questions'] else END

    def answer(state):
        """Pause without side effects, then bind each submitted answer to its question.

        Args:
            state: Validated, saved clarification questions.
        Returns:
            Exact question-answer pairs after resume. Invalid counts or blank
            answers raise ValueError before any refinement call.
        """
        answers = interrupt({'questions': state['questions']})
        if not isinstance(answers, list) or len(answers) != len(state['questions']) or any(
                not isinstance(a, str) or not a.strip() or len(a) > 4000 for a in answers):
            raise ValueError('Please answer each clarification question.')
        return {'answers': [{'question': q, 'answer': a} for q, a in zip(state['questions'], answers)]}

    def refine(state):
        """Update the question and requirements once using the actual user's answers.

        Args:
            state: Original and effective questions, requirements and bound answers.
        Returns:
            Validated revised definition; no additional clarification loop. Invalid
            responses fail without replacing a previously accepted definition.
        """
        data = {k: state[k] for k in ('original_question', 'research_question', 'information_needed', 'answers')}
        request = request_for('refine_research_definition.txt', data, Definition, caller.model)
        def validate(text):
            """Check the refined question and every finalized requirement together.

            Args: text: Raw model response.
            Returns: Complete Definition or raises before ledger acceptance.
            """
            result=Definition.model_validate_json(text)
            if not result.research_question.strip():raise ValueError('The updated research question was blank.')
            validate_plan(json.dumps({'information_needed':[i.model_dump() for i in result.information_needed]}))
            return result
        result=checked_call(caller,'refine',request,state['session_id'],validate)
        return result.model_dump()

    graph = StateGraph(SetupState)
    for name, node in [('define', define), ('clarify', clarify), ('answer', answer), ('refine', refine)]:
        graph.add_node(name, node)
    graph.add_edge(START, 'define')
    graph.add_edge('define', 'clarify')
    graph.add_conditional_edges('clarify', route)
    graph.add_edge('answer', 'refine')
    graph.add_edge('refine', END)
    return graph.compile(checkpointer=checkpointer)


class SearchQuery(BaseModel):
    """One proposed search and its intended contribution, not an executed search."""
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str
    purpose: str


class SearchQueries(BaseModel):
    """Two model-proposed complementary searches; count and duplicates checked locally."""
    model_config = ConfigDict(extra='forbid', strict=True)
    searches: list[SearchQuery]


class SearchState(SetupState, total=False):
    """Finalized setup context plus proposed searches, checkpointed independently."""
    searches: list[dict]


def validate_searches(text):
    """Reject missing, excessive or superficially duplicated query proposals.

    Args:
        text: Exact model JSON response with two searches and purpose explanations.
    Returns:
        Validated proposals, or ValueError for malformed or duplicate output.
        Word-set comparisons catch case, punctuation and reordering, not semantic
        overlap; meaningful diversity and coverage still require review/results.
    """
    import re
    result = SearchQueries.model_validate_json(text)
    if len(result.searches) != 2:
        raise ValueError('Expected exactly two searches.')
    signatures = []
    for item in result.searches:
        if not item.query.strip() or len(item.query) > 500 or not item.purpose.strip() or len(item.purpose) > 2000:
            raise ValueError('Each search needs a concise query and an explanation.')
        signature = frozenset(re.findall(r'\w+', item.query.casefold()))
        if not signature or signature in signatures:
            raise ValueError('The two searches must not duplicate or reorder the same words.')
        signatures.append(signature)
    return result


def build_search_graph(caller, checkpointer):
    """Compile the next stage without rerunning completed setup or clarification.

    Args:
        caller: Same provider transport used by setup, with model identifier.
        checkpointer: Saver used with a separate per-session search thread ID.
    Returns:
        One-node LangGraph that makes one model call from the saved final context.
        It proposes queries only; no search provider, URL fetching or retries.
    """
    def queries(state):
        """Generate the pair from the final question, requirements and actual answers.

        Args:
            state: Ready definition loaded by the server, never supplied by browser.
        Returns:
            Two validated query/purpose records; errors prevent completed status.
        """
        data = {k: state.get(k, [] if k == 'answers' else None)
                for k in ('research_question', 'information_needed', 'answers')}
        request = request_for('search_queries.txt', data, SearchQueries, caller.model)
        return checked_call(caller,'search_queries',request,state['session_id'],validate_searches).model_dump()

    graph = StateGraph(SearchState)
    graph.add_node('queries', queries)
    graph.add_edge(START, 'queries')
    graph.add_edge('queries', END)
    return graph.compile(checkpointer=checkpointer)


class ResearchState(TypedDict):
    """Identity carried by graphs; durable stage results remain in session artifacts."""
    session_id: str


def build_document_graph(pipeline):
    """Connect reading and extraction for an already supplied document pool.

    Args:
        pipeline: DocumentPipeline instance owning source files, progress, external
            I/O and cancellation. Its methods implement reading and extraction.
    Returns:
        A compiled graph, not an executed run. DocumentPipeline.execute invokes
        it and records the terminal outcome, including failures and cancellation.
    """
    graph = StateGraph(ResearchState)
    graph.add_node('download', pipeline.acquire)
    graph.add_node('extract', pipeline.extract)
    graph.add_edge(START, 'download')
    graph.add_edge('download', 'extract')
    graph.add_edge('extract', END)
    return graph.compile()



def build_source_collection_graph(collection):
    """Connect independent search and document processing for web research.

    Args:
        collection: SourceCollection whose search returns paths and whose document
            worker consumes that supplied pool. The workers have separate jobs.
    Returns:
        Compiled graph with an explicit search-to-processing edge. Building it
        performs no work; SourceCollection.execute invokes it and saves its outcome.
    """
    def ready(state):
        """Enter document processing only after paths have been supplied successfully.

        Args: state: Graph identity; outcome belongs to the collection worker.
        Returns: Processing node name, or END after search cancellation/failure.
        """
        return 'process_documents' if collection.search_ready else END

    graph = StateGraph(ResearchState)
    graph.add_node('search', collection.run_search)
    graph.add_node('process_documents', collection.process_documents)
    graph.add_edge(START, 'search')
    graph.add_conditional_edges('search', ready, ['process_documents', END])
    graph.add_edge('process_documents', END)
    return graph.compile()


def build_research_graph(root, snapshot, publish, caller, stop,
                         start_at='search', document_runner=None, runner_factory=None):
    """Build the autonomous workflow, including explicit downstream failure gates.

    Search feeds the first round. Later rounds follow selected links through the
    same document/review stages; only the last round advances to reporting.
    Each node owns one stage attempt. LangGraph, not the HTTP/session layer, decides
    whether its saved outcome permits the next stage. A completed or partial stage
    may continue; failure or cancellation ends this invocation without replay.

    Setup is intentionally a separate checkpointed graph because it can pause for
    a human answer. This graph has no automatic crash replay: only explicit actions
    may re-enter at a saved stage, and triage owns its validated receipt recovery.

    Args:
        root: Path to the owning session directory, not the global runs directory.
        snapshot: Worker-owned mutable public session state. Stage workers retain
            and update it; saved source artifacts remain unchanged.
        publish: Callback persisting the current session snapshot atomically.
        caller: Shared audited model transport, with a model identifier.
        stop: Cancellation event shared by the stage workers and routing gates.
        start_at: First authorized stage: search, documents, selection, triage,
            investigation, link_ranking or report. Earlier stages are not invoked.
        document_runner: Initialized SourceCollection for a new/retried web research
            run. Required for search or documents; external I/O can be
            injected there for offline production-entry-point tests.
        runner_factory: Optional SourceCollection factory reused for later rounds;
            production uses the same acquisition policy and offline tests inject I/O.
    Returns:
        Compiled LangGraph. Construction makes no model calls. Invocation runs
        stage workers and publishes failures without promoting failed evidence.
        Invalid entry points or a missing document runner raise ValueError.
    """
    from projectclarity.evidence_selection.evidence_selection_stage import EvidenceSelectionStage
    from projectclarity.claim_review.claim_triage import TriageRun
    from projectclarity.claim_review.claim_investigation import InvestigationRun
    from projectclarity.reporting.report_generation import ReportRun
    from projectclarity.link_ranking.link_ranking_stage import LinkRankingStage
    from projectclarity.workflow.research_rounds import next_round_paths, begin_round
    from projectclarity.workflow.source_collection import SourceCollection

    runner_factory = runner_factory or SourceCollection

    stage_names = ('search', 'documents', 'selection', 'triage', 'investigation', 'link_ranking', 'report')
    if start_at not in stage_names:
        raise ValueError('Unknown research graph entry point: ' + start_at)
    if start_at in ('search', 'documents') and document_runner is None:
        raise ValueError('Document research requires an initialized document pipeline.')

    def search(state):
        """Prepare candidate URLs before the independent document step begins.

        Args: state: Owning session identity supplied by LangGraph.
        Returns: Unchanged identity with search results or a recorded stop/failure.
        """
        return document_runner.run_search(state)

    def after_search(state):
        """Stop when search could not hand over paths, otherwise process the pool.

        Args: state: Graph identity; the collection worker holds the handoff result.
        Returns: documents after a successful handoff, otherwise END. Individual
            failed queries are recorded; remaining successful query results can proceed.
        """
        return 'documents' if document_runner.search_ready else END

    def documents(state):
        """Process the supplied paths without performing another search.

        Args: state: Owning session identity supplied by LangGraph.
        Returns: Unchanged identity after document and combined outcomes are saved.
        """
        return document_runner.process_documents(state)

    def run_stage(state, key, factory, error_message, **options):
        """Run one stage and keep initialization failures visible to the router.

        Args:
            state: Graph identity returned unchanged to LangGraph.
            key: Saved session bucket name for this stage's progress.
            factory: Stage class accepting root, snapshot, publish, caller and stop.
            error_message: Safe public explanation for an initialization failure.
            options: Stage-specific constructor arguments, such as resume_from.
        Returns:
            Unchanged graph identity. Writes running progress before execution;
            unexpected stage errors become failed progress. Does not call the next
            stage, retry a model request, or alter earlier source records.
        """
        try:
            worker = factory(root, snapshot, publish, caller, stop, **options)
            snapshot['status'] = key + '_running'
            publish(snapshot)
            worker.execute()
        except Exception:
            snapshot['status'] = key + '_failed'
            snapshot.setdefault(key, {}).update(status='failed', error=error_message)
            publish(snapshot)
        return state

    def selection(state):
        """Select potentially relevant originals from saved extraction chunks.

        Args:
            state: Owning session identity supplied by LangGraph.
        Returns:
            Unchanged identity after selection. The active handoff flag prevents
            another request from entering between this node and triage.
        """
        snapshot['auto_triage'] = True
        return run_stage(state, 'selection', EvidenceSelectionStage,
                         'Could not process the saved knowledge index. Original extraction artifacts are retained.')

    def triage(state):
        """Check source support, optionally recovering a stopped triage attempt.

        Args:
            state: Owning session identity supplied by LangGraph.
        Returns:
            Unchanged identity. Consumes a validated resume directory if the session
            action supplied one; recovery checks and reuse remain inside TriageRun.
        """
        snapshot['auto_investigate'] = True
        return run_stage(state, 'triage', TriageRun,
                         'Could not check the saved selected knowledge. Original records are retained.',
                         resume_from=snapshot.pop('resume_triage_from', None))

    def investigation(state):
        """Review flagged claims using stored passages, without new web research.

        Args:
            state: Owning session identity supplied by LangGraph.
        Returns:
            Unchanged identity after decisions and the report knowledge store are
            saved. The graph next ranks links before choosing reporting.
        """
        snapshot['auto_report'] = True
        return run_stage(state, 'investigation', InvestigationRun,
                         'Could not initialize investigation. Original triage records are retained.')

    def round_checkpoint(state):
        """Save a pre-ranking budget choice without holding a worker or spending.

        Args: state: Owning graph identity.
        Returns: Unchanged identity; persists a pending human decision or a skip.
            Stop takes precedence, and no ranking constructor runs while waiting.
        """
        if not stop.is_set():
            from projectclarity.workflow.round_budget_checkpoint import before_link_ranking
            decision = before_link_ranking(snapshot, caller)
            if decision == 'report':
                snapshot['link_ranking'] = {'status': 'skipped',
                    'reason': 'Ranking skipped to preserve budget for reporting.'}
                snapshot.setdefault('rounds_finished_reason', 'User chose reporting without another round.')
            publish(snapshot)
        return state

    def after_checkpoint(state):
        """Route only after the persisted choice; never infer approval from time.

        Args: state: Owning graph identity, unchanged by routing.
        Returns: Ranking, reporting or END for a pending choice/cancellation.
        """
        if stop.is_set() or snapshot.get('status') == 'link_ranking_waiting':
            return END
        return 'report' if snapshot.get('link_ranking', {}).get('status') == 'skipped' else 'link_ranking'

    def link_ranking(state):
        """Score saved outbound links without adding them to report evidence.

        Args: state: Owning graph identity.
        Returns: Unchanged identity after ranking or a visible stage failure.
            The graph decides whether selected destinations start another round.
        """
        return run_stage(state, 'link_ranking', LinkRankingStage,
                         'Could not initialize link ranking. Report evidence is retained.')

    def after_links(state):
        """Choose another bounded round or reporting after successful ranking.

        Args: state: Graph identity; the snapshot contains actual stage outcomes.
        Returns: Next round, report, or END on failure/stop. Records why exploration
            ended; no URLs are fetched by this routing decision.
        """
        if after_stage('link_ranking', 'report')(state) == END:
            return END
        if next_round_paths(snapshot):
            return 'next_round'
        snapshot['rounds_finished_reason'] = ('Round limit reached.' if
            snapshot.get('current_round', 1) >= snapshot.get('research_rounds', 1)
            else 'No unvisited selected links remain.')
        publish(snapshot)
        return 'report'

    def next_round(state):
        """Start selected-link acquisition while retaining earlier round evidence.

        Args: state: Owning graph identity.
        Returns: Unchanged identity. Publishes new inputs before downloads; failed
            initialization is visible and stops the graph. A late stop cannot
            silently advance the round or trigger another search.
        """
        nonlocal document_runner
        if stop.is_set():
            after_stage('link_ranking', 'report')(state)
            return state
        try:
            begin_round(snapshot, next_round_paths(snapshot))
            document_runner = runner_factory(root, snapshot, publish, caller, stop)
            document_runner.supply_links(snapshot['round_paths'])
            publish(snapshot)
        except Exception:
            snapshot['status'] = 'run_failed'
            snapshot.setdefault('run', {}).update(status='failed',
                error='Could not initialize the selected-link round. Previous evidence is retained.')
            publish(snapshot)
        return state

    def after_round_start(state):
        """Enter documents only after the new round is ready and not stopped.

        Args: state: Graph identity.
        Returns: documents or END; initialization failures never reach extraction.
        """
        if stop.is_set() and snapshot.get('run', {}).get('status') == 'running':
            snapshot['run']['status'] = 'cancelled'
            snapshot['status'] = 'run_cancelled'
            publish(snapshot)
        return 'documents' if not stop.is_set() and snapshot.get('run', {}).get('status') == 'running' else END

    def report(state):
        """Generate the final report from the saved report knowledge store.

        Args:
            state: Owning session identity supplied by LangGraph.
        Returns:
            Unchanged identity after success or recorded failure. Report generation
            performs no further collection and this is the graph's final work node.
        """
        return run_stage(state, 'report', ReportRun,
                         'Could not prepare report inputs. Original knowledge is retained.')

    def after_stage(key, next_stage):
        """Build a gate that reads the actual saved outcome rather than assuming success.

        Args:
            key: Saved progress bucket to inspect (documents uses the legacy run key).
            next_stage: Named graph node eligible after completed or partial work.
        Returns:
            Routing callback. Failure and cancellation end the graph; no provider
            request is retried. Both branches are declared explicitly in the graph.
        """
        def route(state):
            """Choose the next node or stop after the just-completed stage.

            Args:
                state: Graph identity; detailed outcome is in the owning snapshot.
            Returns:
                The next node name only for completed/partial work with no stop
                request; otherwise END. A late stop also clears a held running
                status so the UI does not wait for a node that will not start.
            """
            outcome = snapshot.get(key, {}).get('status')
            if not stop.is_set() and outcome in ('completed', 'partial'):
                return next_stage
            if snapshot.get('status') == key + '_running' and outcome != 'running':
                snapshot['status'] = key + '_' + (outcome or 'failed')
                publish(snapshot)
            return END
        return route

    # The autonomous workflow is visible in one place. Search supplies paths to
    # the independent document subgraph; other stages contain their own loops
    # over facts/batches, but never select a downstream workflow stage themselves.
    graph = StateGraph(ResearchState)
    graph.add_node('search', search)
    graph.add_node('documents', documents)
    graph.add_node('selection', selection)
    graph.add_node('triage', triage)
    graph.add_node('investigation', investigation)
    graph.add_node('round_checkpoint', round_checkpoint)
    graph.add_node('link_ranking', link_ranking)
    graph.add_node('report', report)
    graph.add_node('next_round', next_round)
    graph.add_edge(START, 'round_checkpoint' if start_at == 'link_ranking' else start_at)
    graph.add_conditional_edges('search', after_search, ['documents', END])
    graph.add_conditional_edges('documents', after_stage('run', 'selection'), ['selection', END])
    graph.add_conditional_edges('selection', after_stage('selection', 'triage'), ['triage', END])
    graph.add_conditional_edges('triage', after_stage('triage', 'investigation'), ['investigation', END])
    graph.add_conditional_edges('investigation', after_stage('investigation', 'round_checkpoint'), ['round_checkpoint', END])
    graph.add_conditional_edges('round_checkpoint', after_checkpoint, ['link_ranking', 'report', END])
    graph.add_conditional_edges('link_ranking', after_links, ['next_round', 'report', END])
    graph.add_conditional_edges('next_round', after_round_start, ['documents', END])
    graph.add_edge('report', END)
    return graph.compile()
