"""Apply relevance selection to all the extracted information in a research run.

Goes through all of the information we've extracted and selects items that seem
relevent to the users research question. The actual "is this relevant" happens
outside of this code, but the calls are managed here.

After documents have been processed, the application may have many saved groups of
facts. This file opens those groups, checks that their saved contents have not
changed and uses relevance_selection.py to ask which facts could help answer the
question. It saves the chosen originals, their sources and the reasons for choosing
them so the next step can check their support.

research_graph.py runs this stage after document processing. It updates the
selection screen after each group and keeps completed selections if work stops.
Facts from a failed or unfinished request remain unreviewed; they are not silently
treated as irrelevant. This stage does not download new documents or prove claims.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Queue, Empty
from projectclarity.evidence_selection.concurrent_selection import select_batch


from projectclarity.knowledge_extraction.knowledge_extraction import save


class EvidenceSelectionStage:
    """Durable chunk-level selection with independently visible upstream completeness."""

    def __init__(self, root, state, publish, caller, stop):
        """Snapshot the saved knowledge index without modifying extraction artifacts.

        Args:
            root: Session directory containing the completed acquisition run.
            state: Session snapshot with finalized question and requirements.
            publish: Atomic public-state writer.
            caller: Existing model transport exposing model and retaining receipts.
            stop: Cooperative cancellation event.
        Returns:
            None. Creates private selection artifacts; invalid indexes raise before
            model calls. No input-size cap or fact filtering is introduced.
        """
        self.state, self.publish, self.caller, self.stop = state, publish, caller, stop
        self.source_root = root / state['run'].get('artifact_directory', 'run')
        raw = (self.source_root / 'knowledge.json').read_bytes()
        index = json.loads(raw)
        if index['research_question'] != state['research_question'] or index['information_needed'] != state['information_needed']:
            raise ValueError('Saved knowledge context differs from the research definition.')
        refs = index['chunks']
        if len({ref['artifact'] for ref in refs}) != len(refs):
            raise ValueError('Duplicate chunk references in knowledge index.')
        self.root = self.source_root / ('evidence-' + uuid4().hex[:12])
        self.root.mkdir(mode=0o700)
        self.records = []
        self.cached_batches = {}
        prior = state.pop('resume_selection_from', None)
        if prior:
            previous = (root / prior).resolve()
            if not previous.is_relative_to(root.resolve()):
                raise ValueError('Resume selection path is outside the session.')
            manifest = json.loads((previous/'manifest.json').read_text())
            if (manifest['knowledge_index_sha256'] != hashlib.sha256(raw).hexdigest()
                    or manifest['research_question'] != state['research_question']
                    or manifest['information_needed'] != state['information_needed']):
                raise ValueError('Resume selection context differs from saved evidence.')
            for i in range(len(refs)):
                path = previous / f'batch-{i+1:04d}.json'
                if path.exists():
                    receipt = json.loads(path.read_text())
                    if receipt.get('status') == 'checks_pass':
                        self.cached_batches[i+1] = {**receipt, '_reused_from':str(path.relative_to(root.resolve()))}

        self.selection = {'status': 'running', 'artifact_directory': str(self.root.relative_to(root)),
            'started_at': datetime.now(timezone.utc).isoformat(), 'upstream_status': state['run']['status'],
            'concurrency': state.get('selection_concurrency',6), 'batches_total': len(refs), 'batches_finished': 0, 'batches_passed': 0,
            'facts_total': sum(ref['facts'] for ref in refs), 'facts_reviewed': 0,
            'facts_selected': 0, 'model_calls': 0, 'batches_reused':0, 'events': [],
            'batches': [{**ref, 'id': i + 1, 'status': 'pending', 'selected': None} for i, ref in enumerate(refs)]}
        state['selection'] = self.selection
        save(self.root / 'manifest.json', {'research_question': state['research_question'],
            'information_needed': state['information_needed'], 'knowledge_index_sha256': hashlib.sha256(raw).hexdigest(),
            'knowledge_index': index, 'upstream_status': state['run']['status'],
            'model': caller.model, 'max_output_tokens': 8192, 'selection_concurrency': self.selection['concurrency'],
            'prompt_sha256': hashlib.sha256((Path(__file__).resolve().parents[1] / 'prompts/select_relevant_evidence.txt').read_bytes()).hexdigest()}, '')
        self.write_store()

    def event(self, message):
        """Save an operational event and publish observed progress.

        Args:
            message: Credential-free human-readable activity.
        Returns:
            None. Appends a private log and atomically publishes the latest state.
        """
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'message': message}
        self.selection['events'] = (self.selection['events'] + [entry])[-100:]
        with (self.root / 'events.jsonl').open('a') as stream:
            (self.root / 'events.jsonl').chmod(0o600)
            stream.write(json.dumps(entry) + '\n')
        self.publish(self.state)

    def write_store(self):
        """Persist selected originals incrementally so stopping retains completed work.

        Args: None.
        Returns:
            None. Atomically replaces only this attempt's Relevant Knowledge store;
            original extraction files remain untouched. Write failures propagate.
        """
        data = {'research_question': self.state['research_question'],
                'requested_information': self.state['information_needed'],
                'status': self.selection['status'], 'upstream_status': self.selection['upstream_status'],
                'facts': self.records, 'scope': 'Potentially relevant source assertions; not verified answers.'}
        temporary = self.root / 'relevant_knowledge.tmp'
        save(temporary, data, '')
        temporary.replace(self.root / 'relevant_knowledge.json')

    def select(self, graph_state):
        """Select independent batches concurrently with one state-writing coordinator.

        Args: graph_state: Owning graph identity; input snapshots are fixed at init.
        Returns: Unchanged graph state. Saves all finished receipts in stable batch
            order regardless of completion order, retains failures as unreviewed,
            and drains in-flight calls before raising on stop. Empty batches make
            no model calls. No new work is scheduled after cancellation.
        """
        limit = self.selection['concurrency']
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('Selection concurrency must be an integer from 1 to 16.')
        batches = self.selection['batches']
        inbox, results, active, next_batch = Queue(), {}, {}, 0

        def updates():
            """Count dispatches on the only thread that publishes session state.

            Args: None; drains this selection attempt's private queue.
            Returns: None; publishes exact attempted model-call totals.
            """
            changed = False
            while True:
                try:
                    inbox.get_nowait()
                except Empty:
                    break
                self.selection['model_calls'] += 1; changed = True
            if changed:
                self.publish(self.state)

        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='clarity-selection') as pool:
            while active or (next_batch < len(batches) and not self.stop.is_set()):
                while len(active) < limit and next_batch < len(batches) and not self.stop.is_set():
                    batch = batches[next_batch]; next_batch += 1
                    batch['status'] = 'selecting'
                    future = pool.submit(select_batch, dict(batch), self.source_root,
                        self.state['research_question'], self.state['information_needed'],
                        self.caller, self.state['session_id'], self.root.name, self.stop, inbox,
                        self.cached_batches.get(batch['id']))
                    active[future] = batch
                    self.event(f'Selecting evidence: source {batch["document_id"]}, chunk {batch["chunk_index"]+1} ({batch["facts"]} facts).')
                if not active:
                    break
                done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                updates()
                for future in done:
                    batch = active.pop(future); receipt = future.result(); updates()
                    if receipt is None:
                        batch['status'] = 'cancelled'
                        continue
                    if receipt['status'] == 'checks_pass':
                        if receipt.get('reused_from'):
                            self.selection['batches_reused'] += 1
                        store = receipt['relevant_knowledge']
                        results[batch['id']] = [{**record, 'source':receipt['source_artifact'],
                            'entities':store['entities']} for record in store['facts']]
                        self.records = [record for key in sorted(results) for record in results[key]]
                        batch.update(status='completed', selected=len(store['facts']))
                        self.selection['batches_passed'] += 1
                        self.selection['facts_reviewed'] += batch['facts']
                        self.selection['facts_selected'] += batch['selected']
                    else:
                        batch.update(status='failed', error=receipt['error']['message'])
                    save(self.root/f'batch-{batch["id"]:04d}.json', receipt, '')
                    self.selection['batches_finished'] += 1
                    self.write_store()
                    detail = f'{batch["selected"]}/{batch["facts"]} selected' if batch['status']=='completed' else 'unreviewed; selection failed'
                    self.event(f'Source {batch["document_id"]}, chunk {batch["chunk_index"]+1}: {detail}.')
        updates()
        if self.stop.is_set():
            raise InterruptedError('Stopped by user; in-flight selection results saved.')
        return graph_state

    def execute(self):
        """Run selection as a research stage with explicit terminal status.

        Args: None.
        Returns:
            None. Publishes completed/partial/failed/cancelled status separately
            from web_acquisition completeness. No verification, deeper search or report
            generation is performed here; no automatic retry occurs. For automatic
            triage, keep the session active until the research graph takes over.
        """
        try:
            self.event(f'Evidence selection: {self.selection["facts_total"]} facts in {self.selection["batches_total"]} chunks.')
            self.select({'session_id': self.state['session_id']})
            passed, total = self.selection['batches_passed'], self.selection['batches_total']
            self.selection['status'] = 'completed' if passed == total else ('partial' if passed else 'failed')
        except InterruptedError:
            self.selection['status'] = 'cancelled'
        except Exception:
            self.selection.update(status='failed', error='Evidence selection stopped unexpectedly. Saved results are retained.')
        self.selection['finished_at'] = datetime.now(timezone.utc).isoformat()
        self.state['status'] = 'selection_' + self.selection['status']
        if self.state.get('auto_triage') and not self.stop.is_set() and self.selection['status'] in ('completed','partial'):
            self.state['status'] = 'selection_running'
        self.write_store()
        self.event(f'Selection {self.selection["status"]}: {self.selection["facts_selected"]} selected from {self.selection["facts_reviewed"]}/{self.selection["facts_total"]} reviewed facts.')
        save(self.root / 'summary.json', self.selection, '')
