"""Remember each research question and manage the work requested by the user.

A research question can take time to process, pause for clarification, or be
reopened after the browser closes. This file saves the question, answers and
progress, checks whether actions such as start or resume are allowed, and schedules
work without making the web page wait for the research to finish. It also passes
stop requests to the running work and prevents duplicate starts.

web_app.py calls this service when the user takes an action or requests progress.
This service starts the workflows defined in research_graph.py; the graph decides
the next research step. Saved results stay available for inspection even when a
later step fails. Recovery of stopped triage is explicit, not an automatic replay.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import os
import sqlite3
from threading import Lock, Event
from uuid import UUID

from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from projectclarity.model_access.model_client import ROOT, ModelCalls
from projectclarity.workflow.research_graph import build_setup_graph, build_search_graph, build_research_graph
from projectclarity.workflow.source_collection import SourceCollection

ACTIVE_STATUSES = ('working', 'search_working', 'run_running', 'selection_running', 'triage_running', 'investigation_running', 'link_ranking_running', 'report_running')


class Sessions:
    """Durable local sessions and serialized graph work; not a research worker system."""

    def __init__(self, root, caller=None, env_file=Path.cwd() / '.env', runner_factory=SourceCollection):
        """Initialize private session files and persistent LangGraph checkpoints.

        Args:
            root: Directory dedicated to this UI, separate from research_benchmark results.
            caller: Optional provider replacement for deterministic offline tests.
            env_file: Existing credential file for real requests.
            runner_factory: Real autonomous runner, replaceable for offline API tests.
        Returns:
            None. Creates SQLite checkpoint storage and one background worker.
            Interrupted in-flight sessions are marked failed, never auto-replayed.
        """
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = Lock()
        self.stops = {}
        self.runner_factory = runner_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='research-setup')
        self.connection = sqlite3.connect(root / 'checkpoints.sqlite', check_same_thread=False)
        (root / 'checkpoints.sqlite').chmod(0o600)
        transport = caller or ModelCalls(root, env_file)
        self.transport = transport
        saver = SqliteSaver(self.connection)
        self.graph = build_setup_graph(transport, saver)
        self.search_graph = build_search_graph(transport, saver)
        for path in root.glob('*/session.json'):
            state = json.loads(path.read_text())
            if state['status'] == 'link_ranking_running':
                state['status'] = 'link_ranking_interrupted'
                state['link_ranking'].update(status='interrupted', error='Server stopped; saved ranking retained. No automatic replay.')
                self.write(state)
            if state['status'] == 'report_running':
                state['status']='report_interrupted'
                state['report'].update(status='interrupted',error='Server stopped. No automatic replay; original knowledge retained.')
                self.write(state)
            if state['status'] == 'investigation_running':
                state['status'] = 'investigation_interrupted'
                state['investigation'].update(status='interrupted', error='Server stopped. Saved reviews remain; no automatic replay.')
                self.write(state)
            if state['status'] == 'triage_running':
                state['status'] = 'triage_interrupted'
                state['triage'].update(status='interrupted', error='Server stopped. Saved decisions remain; no automatic replay.')
                self.write(state)
            if state['status'] == 'selection_running':
                state['status'] = 'selection_interrupted'
                state['selection'].update(status='interrupted', error='Server stopped. Saved selections remain; no automatic replay.')
                self.write(state)
            if state['status'] == 'run_running':
                state['status'] = 'run_interrupted'
                state['run'].update(status='interrupted', stage='finished', error='Server stopped. Saved work is retained; no automatic replay.')
                self.write(state)
            if state['status'] in ('working', 'search_working'):
                state.update(status='search_failed' if state['status'] == 'search_working' else 'failed', error='The server stopped during this request. No model calls were retried. Start a new question.')
                self.write(state)

    def read(self, session_id):
        """Read a public session snapshot using a strictly validated identifier.

        Args:
            session_id: UUID string, never an arbitrary filesystem path.
        Returns:
            Snapshot dictionary; missing or invalid IDs raise HTTP 404.
        """
        try:
            path = self.root / str(UUID(str(session_id))) / 'session.json'
            state = json.loads(path.read_text())
            state['stop_requested'] = self.stops.get(str(session_id), Event()).is_set()
            return state
        except (ValueError, OSError):
            raise HTTPException(404, 'Research setup not found.')

    def write(self, state):
        """Atomically publish the latest UI state without exposing raw provider receipts.

        Args:
            state: Session dictionary with validated ID and user-facing fields.
        Returns:
            None. Writes owner-only JSON via atomic replacement; errors propagate.
        """
        directory = self.root / state['session_id']
        directory.mkdir(exist_ok=True, mode=0o700)
        temporary = directory / 'session.tmp'
        with temporary.open('w') as stream:
            os.chmod(temporary, 0o600)
            json.dump(state, stream, ensure_ascii=False, indent=2)
        temporary.replace(directory / 'session.json')

    def work(self, state, payload, search=False):
        """Execute setup or query planning and publish its saved status.

        Args:
            state: Public snapshot captured before background execution.
            payload: New graph input or Command carrying exact clarification answers.
            search: Use the isolated query graph; never rerun the setup graph.
        Returns:
            None. Updates saved public status and graph checkpoint; exceptions become
            sanitized failures with no automatic provider retry. Opted-in web sessions
            advance ready definitions to query preparation and prepared queries to
            acquisition; clarification still pauses and reopening pages never starts work.
        """
        try:
            graph = self.search_graph if search else self.graph
            config = {'configurable': {'thread_id': state['session_id'] + (':searches' if search else '')}}
            graph.invoke(payload, config)
            snapshot = graph.get_state(config)
            state.update(snapshot.values)
            state['status'] = 'queries_ready' if search else ('clarification' if snapshot.next else 'ready')
        except Exception as error:
            state['status'] = 'search_failed' if search else 'failed'
            state['error'] = (str(error) if isinstance(error, RuntimeError) else
                              'The response did not match the required format. No definition was accepted. Start a new question to try again.')
            if search:
                state['error'] = 'Search-query generation failed. Your research definition is preserved. No searches were run or retried.'
        with self.lock:
            self.write(state)
        if state.get('auto_advance') and state['status'] in ('ready','queries_ready'):
            try:
                if state['status']=='ready':self.prepare_searches(state['session_id'])
                else:self.start_run(state['session_id'])
            except Exception:
                with self.lock:
                    current=self.read(state['session_id'])
                    if current['status'] in ('ready','queries_ready'):
                        current.update(status='launch_failed',error='Could not advance to the next stage. Saved inputs are retained; no automatic retry was made.')
                        self.write(current)

    def start(self, data):
        """Queue a new definition once and return immediately for a responsive UI.

        Args:
            data: Validated question and client-generated UUID.
        Returns:
            Working snapshot or existing identical request snapshot. Conflicting
            reuse, empty questions or an already active request raise HTTP errors.
            auto_advance persists the requested automatic downstream handoffs;
            research_rounds bounds total collection/review passes (one to five).
            extraction_concurrency, selection_concurrency and triage_concurrency fix per-run
            call limits for their respective stages.
        """
        sid, question = str(data.session_id), data.question.strip()
        if not question:
            raise HTTPException(422, 'Enter a research question.')
        with self.lock:
            if (self.root / sid / 'session.json').exists():
                existing = self.read(sid)
                if existing['original_question'] != question:
                    raise HTTPException(409, 'This request ID belongs to a different question.')
                return existing
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409, 'Another setup is still running. Please wait for it to finish.')
            state = {'session_id': sid, 'original_question': question, 'status': 'working', 'auto_advance': data.auto_advance,
                     'research_rounds': data.research_rounds, 'current_round': 1,
                     'extraction_concurrency': data.extraction_concurrency,
                     'selection_concurrency': data.selection_concurrency,
                     'triage_concurrency': data.triage_concurrency,
                     'investigation_concurrency': data.investigation_concurrency,
                     'link_ranking_concurrency': data.link_ranking_concurrency,
                     'report_selection_concurrency': data.report_selection_concurrency}
            self.write(state)
            self.executor.submit(self.work, state.copy(), {'session_id': sid, 'original_question': question})
            return state

    def answer(self, sid, answers):
        """Resume only a pending clarification with the exact number of nonblank answers.

        Args:
            sid: UUID of the paused session.
            answers: Ordered free-text answers corresponding to saved questions.
        Returns:
            Working snapshot. Repeated, stale or invalid submissions fail before
            model calls; the original pending form remains intact on validation errors.
        """
        with self.lock:
            state = self.read(sid)
            if state['status'] != 'clarification':
                raise HTTPException(409, 'This setup is not waiting for clarification.')
            if len(answers) != len(state['questions']) or any(not a.strip() or len(a) > 4000 for a in answers):
                raise HTTPException(422, 'Answer each question (up to 4,000 characters each).')
            state['status'] = 'working'
            self.write(state)
            self.executor.submit(self.work, state.copy(), Command(resume=answers))
            return state

    def prepare_searches(self, sid):
        """Generate one pair for a ready saved definition, idempotently.

        Args:
            sid: UUID of an existing completed setup session.
        Returns:
            Working or previously completed search snapshot. Rejects unfinished
            definitions and failed attempts; never repeats calls on double clicks.
            Existing setup checkpoints and all original user answers are preserved.
        """
        with self.lock:
            state = self.read(sid)
            if state['status'] in ('search_working', 'queries_ready'):
                return state
            if state['status'] != 'ready':
                raise HTTPException(409, 'Complete the research definition before preparing searches.')
            state['status'] = 'search_working'
            self.write(state)
            self.executor.submit(self.work, state.copy(), state.copy(), True)
            return state

    def publish_run(self, state):
        """Publish one run snapshot under the session-file writer lock.

        Args:
            state: Worker-owned public session state including observed progress.
        Returns:
            None. Atomically replaces the public snapshot without starting work.
        """
        with self.lock:
            self.write(state)

    def start_run(self, sid):
        """Start the saved two-search pool once, returning a real-time monitor state.

        Args:
            sid: Session UUID whose query pair is ready.
        Returns:
            Running or prior terminal snapshot. Incompatible stages and another
            active run raise HTTP409. Duplicate clicks cannot repeat web_acquisition.
        """
        with self.lock:
            state = self.read(sid)
            if 'run' in state:
                return state
            if state['status'] != 'queries_ready':
                raise HTTPException(409, 'Prepare the two searches before starting research.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409, 'Another request is still running. Please wait for it to finish.')
            stop = Event()
            self.stops[sid] = stop
            state['status'] = 'run_running'
            state['auto_select'] = True
            runner = self.runner_factory(self.root / sid, state, self.publish_run, self.transport, stop)
            self.write(state)
            self.executor.submit(self.execute_research, state, stop, 'search', runner)
            return state

    def execute_research(self, state, stop, start_at, runner=None):
        """Invoke the shared research graph from an explicitly authorized stage.

        The graph owns downstream transitions. The session service only schedules
        this invocation; it must not call one stage and then choose another itself.

        Args:
            state: Worker-owned saved question and stage-progress dictionary.
            stop: Cancellation event shared by this invocation's stages.
            start_at: search, documents, selection, triage, investigation, link_ranking
                or report.
            runner: Optional initialized document pipeline from start/retry.
        Returns:
            None. Stage implementations publish progress and retain receipts. This
            does not replay earlier stages when starting from saved evidence.
            Unexpected graph infrastructure errors propagate to the worker future.
        """
        graph = build_research_graph(
            self.root / state['session_id'], state, self.publish_run,
            self.transport, stop, start_at=start_at, document_runner=runner, runner_factory=self.runner_factory)
        graph.invoke({'session_id': state['session_id']}, {'recursion_limit': 64})

    def start_report(self, sid, previous_started_at=None):
        """Queue ranking then reporting on saved investigation evidence.

        Args: sid: Existing session UUID; previous_started_at: Optional explicit
            failed-attempt timestamp for a user-requested retry.
        Returns: Queued/existing state; HTTP409 for active work or absent inputs.
            Reopening or duplicate clicks never generate another report. Explicit
            report retries always enter reporting directly, even for legacy runs
            lacking ranking metadata. On first preparation, unfinished ranking gets a new
            retained attempt only after this explicit action.
        """
        with self.lock:
            state=self.read(sid)
            prior=state.get('report')
            if prior and (previous_started_at is None or prior.get('started_at')!=previous_started_at or prior['status'] not in ('failed','cancelled','interrupted')):return state
            if previous_started_at is not None and prior is None:raise HTTPException(409,'No matching failed report exists.')
            if state.get('investigation',{}).get('status') not in ('completed','partial','cancelled','interrupted'):
                raise HTTPException(409,'Finish investigation before preparing a report.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409,'Another request is running.')
            folder=state['investigation'].get('artifact_directory')
            if not folder or not (self.root/sid/folder/'report_knowledge.json').exists():raise HTTPException(409,'Report Knowledge is unavailable.')
            stop=Event();self.stops[sid]=stop
            if prior:state.setdefault('report_history',[]).append(state.pop('report'))
            entry = 'report' if prior or state.get('link_ranking', {}).get('status') in ('completed', 'partial') else 'link_ranking'
            if entry == 'link_ranking' and state.get('link_ranking'):
                state.setdefault('link_ranking_history', []).append(state.pop('link_ranking'))
            state.update(status=entry + '_running', stop_requested=False)
            state[entry] = {'status': 'running'}
            self.write(state)
            self.executor.submit(self.execute_research, state, stop, entry);return state

    def link_ranking_data(self, sid):
        """Read saved candidates without starting ranking or following any link.

        Args: sid: Existing session UUID.
        Returns: Candidates, selected links and issues, or empty lists before the
            artifact exists. Reads only the current attempt within its session.
        """
        from projectclarity.claim_review.claim_evidence_collection import checked_path
        state = self.read(sid)
        folder = state.get('link_ranking', {}).get('artifact_directory')
        if folder:
            path = checked_path(self.root / sid, folder) / 'ranked_links.json'
            if path.exists():
                return json.loads(path.read_text())
        return {'candidates': [], 'selected': [], 'issues': []}

    def report_data(self, sid):
        """Read only a fully validated report, never an incomplete model response.

        Args: sid: Existing session UUID.
        Returns: Report object or None while missing/failed/interrupted; no calls.
        """
        state=self.read(sid);report=state.get('report',{})
        if report.get('status')!='completed':return None
        return json.loads((self.root/sid/report['artifact_directory']/'report.json').read_text())

    def start_investigation(self, sid):
        """Start a historical triage's investigation once, without replaying upstream.

        Args: sid: Existing session UUID.
        Returns: Queued/existing state; HTTP409 for active work or absent triage.
            Repeated clicks cannot duplicate requests. GET never starts this action.
        """
        with self.lock:
            state=self.read(sid)
            if 'investigation' in state:return state
            if state.get('triage',{}).get('status') not in ('completed','partial','cancelled','interrupted'):
                raise HTTPException(409,'Finish triage before investigating claims.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409,'Another request is running.')
            folder=state['triage'].get('artifact_directory')
            if not folder or not (self.root/sid/folder/'decisions.json').exists():
                raise HTTPException(409,'No saved triage decisions are available.')
            stop=Event();self.stops[sid]=stop
            state.update(status='investigation_running',investigation={'status':'running'})
            self.write(state);self.executor.submit(self.execute_research, state, stop, 'investigation')
            return state

    def resume_investigation(self, sid, previous_artifact, concurrency):
        """Resume a terminal claim review without replaying upstream research.

        Args: sid: Session UUID; previous_artifact: Exact displayed attempt token;
            concurrency: Validated integer worker limit from one to sixteen.
        Returns: Queued snapshot or unchanged state for stale duplicate actions.
            Archives old reviews and revalidates completed replies; only failed or
            unfinished claims require calls. Rejects active or downstream work.
        """
        with self.lock:
            state = self.read(sid); prior = state.get('investigation',{})
            if prior.get('artifact_directory') != previous_artifact:
                return state
            if prior.get('status') not in ('cancelled','interrupted','failed','partial'):
                raise HTTPException(409,'Stop investigation before resuming it.')
            if state.get('link_ranking') or state.get('report'):
                raise HTTPException(409,'Investigation already has downstream results.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409,'Another request is running.')
            state.setdefault('investigation_history',[]).append(prior)
            state.update(resume_investigation_from=previous_artifact,investigation_concurrency=concurrency,
                status='investigation_running',stop_requested=False,investigation={'status':'running'})
            stop = Event(); self.stops[sid] = stop
            self.write(state)
            self.executor.submit(self.execute_research,state,stop,'investigation')
            return state

    def investigation_records(self, sid):
        """Read saved review packets and outcomes, never private provider receipts.

        Args: sid: Existing session UUID.
        Returns: Review list and report records; before initialization, read-only
            previews of flagged and already approved triage records.
            On interruption, unsettled cards remain pending rather than reviewing.
        """
        state=self.read(sid);progress=state.get('investigation',{})
        folder=progress.get('artifact_directory')
        if not folder:
            triaged=self.triage_records(sid)['facts']
            return {'records':[{'id':i+1,'record':r,'status':'pending'} for i,r in enumerate(triaged) if r['triage_bucket']=='needs_verification'],
                    'report_facts':[r for r in triaged if r['triage_bucket']=='report_knowledge']}
        path=self.root/sid/folder
        result=json.loads((path/'reviews.json').read_text())
        if progress.get('status')!='running':
            for row in result['records']:
                if row['status'] in ('collecting','reviewing'):row['status']='pending'
        result['report_facts']=json.loads((path/'report_knowledge.json').read_text())['facts']
        return result

    def resume_research(self, sid, artifact_directory):
        """Queue recovery of stopped triage without replaying earlier research stages.
        
        The saved attempt directory also acts as a token for the button click. This
        prevents a stale tab from resuming a different attempt and makes repeated
        clicks for the same accepted token return the already queued work.
        
        Args:
            sid: Validated session UUID identifying the saved research question.
            artifact_directory: Exact relative directory of the stopped triage attempt,
                copied from its public state; not a user-selected filesystem path.
        Returns:
            The current or newly queued session snapshot. Under the session lock,
            saves the old triage state in history, clears cancellation and submits
            the research graph to the background worker. That worker validates recovery
            inputs and reuses available responses; this method does not call a model.
            Raises HTTP 409 for an ineligible/stale attempt or other active research.
            Only interrupted/cancelled triage without later stages is resumable.
            Read/write or queue errors propagate; old evidence files are not replaced.
        """
        with self.lock:
            state=self.read(sid)
            if state.get('last_resume_token')==artifact_directory:return state
            triage=state.get('triage',{})
            if state['status'] not in ('triage_interrupted','triage_cancelled') or triage.get('artifact_directory')!=artifact_directory or state.get('investigation') or state.get('report'):
                raise HTTPException(409,'Only stopped triage can be resumed from this attempt.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409,'Another request is running. Wait before resuming.')
            state.setdefault('triage_history',[]).append(dict(triage))
            state.update(resume_triage_from=artifact_directory,last_resume_token=artifact_directory,status='triage_running',stop_requested=False)
            state['triage']={**triage,'status':'running'};state['triage'].pop('error',None)
            stop=Event();self.stops[sid]=stop;self.write(state)
            self.executor.submit(self.execute_research, state, stop, 'triage')
            return state

    def start_triage(self, sid):
        """Queue triage once for an existing saved selection.

        Args: sid: Existing session UUID.
        Returns:
            Queued or existing triage snapshot. HTTP409 rejects active work or
            absent selection artifacts. Double clicks and refresh do not replay.
        """
        with self.lock:
            state = self.read(sid)
            if 'triage' in state: return state
            if state.get('selection', {}).get('status') not in ('completed','partial','cancelled','interrupted'):
                raise HTTPException(409, 'Finish evidence selection before triage.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409, 'Another request is running.')
            folder = state['selection'].get('artifact_directory')
            if not folder or not (self.root / sid / folder / 'relevant_knowledge.json').exists():
                raise HTTPException(409, 'No saved selected knowledge is available.')
            stop = Event(); self.stops[sid] = stop
            state.update(status='triage_running', triage={'status':'running'})
            self.write(state)
            self.executor.submit(self.execute_research, state, stop, 'triage')
            return state

    def triage_records(self, sid):
        """Expose saved bucket decisions without private model transport receipts.

        Args: sid: Session UUID validated by read.
        Returns:
            Original facts, quotes, routing reasons and unreviewed IDs; no calls.
            Before initialization returns empty lists, not invented decisions.
        """
        state = self.read(sid)
        folder = state.get('triage', {}).get('artifact_directory')
        if not folder: return {'facts':[], 'unreviewed_fact_ids':[]}
        return json.loads((self.root / sid / folder / 'decisions.json').read_text())

    def start_selection(self, sid):
        """Select a historical run's saved knowledge once, without new web_acquisition.

        Args:
            sid: Existing session UUID with a terminal extraction run.
        Returns:
            Queued or existing selection snapshot. Duplicate clicks/refresh cannot
            repeat model calls. Active runs and missing knowledge return HTTP409.
        """
        with self.lock:
            state = self.read(sid)
            if 'selection' in state:
                return state
            if state.get('run', {}).get('status') not in ('completed', 'partial', 'cancelled', 'interrupted'):
                raise HTTPException(409, 'Wait for knowledge extraction to finish.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409, 'Another request is running.')
            if not (self.root / sid / state['run'].get('artifact_directory', 'run') / 'knowledge.json').exists():
                raise HTTPException(409, 'This run has no saved knowledge index.')
            stop = Event(); self.stops[sid] = stop
            state.update(status='selection_running', selection={'status': 'running', 'events': []})
            self.write(state)
            self.executor.submit(self.execute_research, state, stop, 'selection')
            return state

    def resume_selection(self, sid, previous_artifact, concurrency):
        """Continue stopped selection with reusable receipts and an explicit call cap.

        Args: sid: Session UUID; previous_artifact: Exact displayed attempt path;
            concurrency: Validated selection worker limit, from one to sixteen.
        Returns: Queued snapshot or unchanged state for stale/duplicate actions.
            Rejects active work or downstream decisions. Archives the old attempt,
            never repeats search/extraction, and revalidates cached batch responses.
        """
        with self.lock:
            state = self.read(sid)
            prior = state.get('selection', {})
            if prior.get('artifact_directory') != previous_artifact:
                return state
            if prior.get('status') not in ('cancelled','interrupted','failed','partial'):
                raise HTTPException(409, 'Stop selection before resuming it.')
            if state.get('triage'):
                raise HTTPException(409, 'Selection already has downstream claim reviews.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409, 'Another request is running.')
            state.setdefault('selection_history', []).append(prior)
            state.update(resume_selection_from=previous_artifact, selection_concurrency=concurrency,
                         status='selection_running', stop_requested=False,
                         selection={'status':'running','events':[]})
            stop = Event(); self.stops[sid] = stop
            self.write(state)
            self.executor.submit(self.execute_research, state, stop, 'selection')
            return state

    def retry_run(self, sid, data):
        """Create an explicit new attempt while retaining the complete previous run.

        Args:
            sid: Saved session UUID.
            data: Previous start timestamp and explicit search provider selection.
        Returns:
            New working snapshot or current snapshot for stale duplicate requests.
            Only terminal runs can be retried; existing receipts are never overwritten.
            Linked rounds retry their saved destinations, never first-round search.
        """
        with self.lock:
            state = self.read(sid)
            if state.get('run',{}).get('started_at') != data.previous_started_at:
                return state
            if state.get('run',{}).get('status') not in ('failed','partial','cancelled','interrupted'):
                raise HTTPException(409,'Only a stopped or incomplete run can be retried.')
            if any(json.loads(p.read_text())['status'] in ACTIVE_STATUSES for p in self.root.glob('*/session.json')):
                raise HTTPException(409,'Another request is running.')
            state.setdefault('run_history',[]).append(state['run'])
            if 'selection' in state:
                state.setdefault('selection_history', []).append(state.pop('selection'))
            if 'triage' in state:
                state.setdefault('triage_history', []).append(state.pop('triage'))
            if 'investigation' in state:
                state.setdefault('investigation_history', []).append(state.pop('investigation'))
            if 'link_ranking' in state:
                state.setdefault('link_ranking_history', []).append(state.pop('link_ranking'))
            if 'report' in state:
                state.setdefault('report_history', []).append(state.pop('report'))
            state['search_provider']=data.provider
            state['status']='run_running'
            state['auto_select']=True
            stop=Event();self.stops[sid]=stop
            runner=self.runner_factory(self.root/sid,state,self.publish_run,self.transport,stop)
            entry = 'search'
            if state.get('current_round', 1) > 1:
                runner.supply_links(state['round_paths'])
                entry = 'documents'
            self.write(state)
            self.executor.submit(self.execute_research, state, stop, entry, runner)
            return state

    def stop_run(self, sid):
        """Request cancellation without discarding acquired or extracted records.

        Args:
            sid: Existing session UUID.
        Returns:
            Current snapshot with stop_requested. Work stops at the next boundary;
            an in-flight model request can take up to its configured timeout.
        """
        with self.lock:
            state = self.read(sid)
            if state['status'] in ('run_running', 'selection_running', 'triage_running', 'investigation_running', 'link_ranking_running', 'report_running') and sid in self.stops:
                self.stops[sid].set()
                state['stop_requested'] = True
            return state

    def evidence(self, sid):
        """Read source-linked decisions and exact facts for the selection screen.

        Args:
            sid: Session UUID validated by read.
        Returns:
            Original prepared facts with selected/unselected decisions for successful
            batches only. Failed/pending batches remain in the status snapshot;
            no fabricated exclusion reasons or provider credentials are exposed.
        """
        state = self.read(sid)
        selection = state.get('selection', {})
        if not selection.get('artifact_directory'):
            return {'batches': []}
        folder = self.root / sid / selection['artifact_directory']
        batches = []
        for batch in selection.get('batches', []):
            if batch['status'] != 'completed':
                continue
            receipt = json.loads((folder / f'batch-{batch["id"]:04d}.json').read_text())
            payload = json.loads(receipt['request']['messages'][0]['content'])
            decisions = {x['fact_id']: x['reason'] for x in receipt['parsed']['selected_facts']}
            batches.append({'source': receipt['source_artifact'], 'entities': payload['entities'],
                'facts': [{**fact, 'selected': fact['fact_id'] in decisions,
                           'reason': decisions.get(fact['fact_id'])} for fact in payload['facts']]})
        return {'batches': batches}

    def knowledge(self, sid, document_id):
        """Expose saved text and accepted facts for inspecting one document's provenance.

        Args:
            sid: Existing session UUID, validated by read.
            document_id: Positive ID belonging to the recorded run URL pool.
        Returns:
            Source metadata, parsed document text, accepted chunk projections and
            unresolved/withdrawn proposal details;
            unknown IDs raise HTTP404. This read performs no acquisition/model calls.
        """
        state = self.read(sid)
        source = next((s for s in state.get('run',{}).get('sources',[]) if s['id'] == document_id), None)
        if source is None:
            raise HTTPException(404, 'Source not found.')
        folder = self.root / sid / state['run'].get('artifact_directory','run') / f'document-{document_id:03d}'
        records=[]
        for path in sorted(folder.glob('chunk-*.json')):
            receipt=json.loads(path.read_text())
            if receipt['status'] in ('checks_pass','checks_partial'):
                records.append({'chunk_index':receipt['chunk_index'],'pages':receipt.get('pages',[]),'grounding':receipt.get('grounding',[]),'unresolved':receipt.get('unresolved',[]),'withdrawn':receipt.get('withdrawn',[]),**receipt['parsed']})
        text_path=folder/'document.txt'
        return {'source':source,'document_text':text_path.read_text() if text_path.exists() else None,'knowledge':records}

    def close(self):
        """Finish queued work and release the SQLite connection during shutdown/tests.

        Args:
            None.
        Returns:
            None. Waits for active work rather than abandoning an in-flight model call.
        """
        self.executor.shutdown(wait=True)
        self.connection.close()
