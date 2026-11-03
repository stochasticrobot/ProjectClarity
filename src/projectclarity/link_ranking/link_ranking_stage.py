"""Choose promising saved links without assessing thousands of titles.

After evidence is reviewed, this step gives links points for their source page's
contribution to the report evidence. Repeated destinations are combined and a
bounded shortlist is sent to the AI for title-based usefulness ratings. The user
can inspect both scores, every source association and why other links were left
out. No destination is downloaded and no new facts are added to the report.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Queue, Empty
from projectclarity.link_ranking.concurrent_link_ranking import rank_batch
from projectclarity.claim_review.claim_evidence_collection import checked_path
from projectclarity.documents.document_links import document_links
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.link_ranking.link_shortlisting import shortlist_links, MAX_SHORTLIST, MAX_SOURCE_SHORTLIST, TITLE_BATCH_SIZE
from projectclarity.link_ranking.link_scoring import TITLE_POINTS, select_links


class LinkRankingStage:
    """One durable ranking attempt; selected links are suggestions, not new evidence."""

    def __init__(self, root, state, publish, caller, stop):
        """Snapshot report evidence and create an isolated ranking artifact directory.

        Args:
            root: Owning session directory.
            state: Mutable saved session with investigation and acquired sources.
            publish: Callback persisting public progress.
            caller: Audited model transport exposing its model name.
            stop: Cooperative cancellation event checked between batches/documents.
        Returns:
            None; writes a manifest. Missing or inconsistent evidence raises before
            model calls. Existing documents and report inputs are never changed.
        """
        self.state, self.publish, self.caller, self.stop = state, publish, caller, stop
        folder = checked_path(root, state['investigation']['artifact_directory'])
        raw = (folder / 'report_knowledge.json').read_bytes()
        store = json.loads(raw)
        if store['research_question'] != state['research_question']:
            raise ValueError('Report evidence question differs.')
        facts = store['facts']
        if len({fact['fact_id'] for fact in facts}) != len(facts):
            raise ValueError('Duplicate report evidence facts.')
        self.counts = Counter(str(fact['source']['document_id']) for fact in facts)
        self.total = len(facts)
        self.source_root = checked_path(root, state.get('run', {}).get('artifact_directory', 'run'))
        self.root = folder / ('links-' + uuid4().hex[:12])
        self.root.mkdir(mode=0o700)
        self.rows = []
        self.session_root = root
        self.evidence_hash = hashlib.sha256(raw).hexdigest()
        self.cached_batches = {}
        self.progress = {'status': 'running', 'artifact_directory': str(self.root.relative_to(root)),
            'started_at': datetime.now(timezone.utc).isoformat(), 'evidence_facts': self.total,
            'candidates_total': 0, 'candidates_ranked': 0, 'batches_total': 0,
            'batches_finished': 0, 'batches_reused':0, 'concurrency':state.get('link_ranking_concurrency',6), 'model_calls': 0, 'selected': [], 'issues': []}
        state['link_ranking'] = self.progress
        save(self.root / 'manifest.json', {'research_question': state['research_question'],
            'evidence_sha256': hashlib.sha256(raw).hexdigest(), 'evidence_facts': self.total,
            'source_fact_counts': dict(self.counts), 'title_points': TITLE_POINTS,
            'concurrency':self.progress['concurrency'], 'batch_size': TITLE_BATCH_SIZE, 'maximum_links': 10, 'maximum_per_source': 3,
            'maximum_shortlist': MAX_SHORTLIST, 'maximum_shortlist_per_source': MAX_SOURCE_SHORTLIST,
            'policy': 'Deduplicate before ranking; highest source contribution owns shortlist and final quotas. Retain all source associations.'}, '')

    def persist(self):
        """Atomically save inspectable candidates before publishing their progress.

        Args: None.
        Returns: None; file or publication failures propagate. No external calls.
        """
        self.progress['selected'] = select_links(self.rows)
        self.progress['candidates_ranked'] = sum(row['status'] == 'ranked' for row in self.rows)
        temporary = self.root / 'ranked_links.tmp'
        save(temporary, {'candidates': self.rows, 'selected': self.progress['selected'],
                        'issues': self.progress['issues']}, '')
        temporary.replace(self.root / 'ranked_links.json')
        self.publish(self.state)

    def collect(self):
        """Recover saved links, combine destinations and shortlist before any model call.

        Args: None.
        Returns: None; saves input hashes. Missing/unreadable source artifacts are
            recorded as issues, not interpreted as pages with no links. Stops raise
            InterruptedError. Saves counts for occurrences, unique destinations, exclusions
            and unassessed links. At most 500 destinations (50 per source) proceed;
            untitled shortlisted links receive a transparent zero bonus.
        """
        inputs = []
        for source in self.state.get('run', {}).get('sources', []):
            if self.stop.is_set():
                raise InterruptedError()
            if not source.get('downloaded') or source.get('duplicate'):
                continue
            try:
                folder = checked_path(self.source_root, f'document-{source["id"]:03d}')
                acquisition = json.loads((folder / 'web_acquisition.json').read_text())
                body = (folder / 'source.bin').read_bytes()
                digest = hashlib.sha256(body).hexdigest()
                if digest != acquisition['sha256']:
                    raise ValueError('Source bytes changed.')
                title, links = document_links(body, acquisition['content_type'],
                    acquisition['final_url'], source.get('title', ''))
                inputs.append({'document_id': source['id'], 'sha256': digest, 'links': len(links)})
                score = 100 * self.counts[str(source['id'])] / self.total if self.total else 0
                for link in links:
                    titled = bool(link['link_title'].strip())
                    self.rows.append({**link, 'id': f'L{len(self.rows)+1}',
                        'source_document_id': source['id'], 'source_url': acquisition['final_url'],
                        'source_title': title, 'document_score': score,
                        'title_score': None if titled else 0, 'rating': None if titled else 'none',
                        'reason': '' if titled else 'No link title available; no title bonus.',
                        'status': 'pending' if titled else 'ranked'})
            except Exception:
                self.progress['issues'].append(f'Source {source["id"]}: saved links could not be read.')
        save(self.root / 'source_inputs.json', {'sources': inputs}, '')
        from projectclarity.workflow.research_rounds import visited_sources
        self.rows, counts = shortlist_links(self.rows, visited_sources(self.state))
        self.progress.update(counts)
        self.progress['candidates_total'] = len(self.rows)
        self.persist()

    def load_cached_batches(self, batches):
        """Index unchanged successful replies across archived ranking attempts.

        Args: batches: Current deterministic shortlist groups, after collection.
        Returns: None; stores candidate receipts with source provenance. Evidence
            hashes and shortlist policy must match; workers check exact requests
            and validate ratings again. Old files are never changed. Changed or
            failed work is not reused and is eligible for this explicit retry.
        """
        for prior in self.state.get('link_ranking_history', []):
            relative = prior.get('artifact_directory')
            if not relative:
                continue
            try:
                folder = checked_path(self.session_root,relative)
                manifest = json.loads((folder/'manifest.json').read_text())
                if (manifest.get('evidence_sha256') != self.evidence_hash
                        or manifest.get('research_question') != self.state['research_question']
                        or manifest.get('maximum_shortlist') != MAX_SHORTLIST
                        or manifest.get('maximum_shortlist_per_source') != MAX_SOURCE_SHORTLIST
                        or manifest.get('batch_size') != TITLE_BATCH_SIZE):
                    continue
                for index, batch in enumerate(batches,1):
                    path = folder/f'batch-{index:04d}.json'
                    if path.exists():
                        receipt = json.loads(path.read_text())
                        if receipt.get('status')=='checks_pass' and receipt.get('ids')==[row['id'] for row in batch]:
                            self.cached_batches[index] = {**receipt,'_reused_from':str(path.relative_to(self.session_root.resolve()))}
            except (ValueError,KeyError,OSError):
                continue

    def rank_batches(self, batches):
        """Run bounded independent ratings while serializing scores and saves.

        Args: batches: Stable shortlist groups; row dictionaries belong to owner.
        Returns: None; saves finished receipts and ranked rows in original order.
            Cancellation drains started calls and then raises, leaving unfinished
            rows pending. Invalid responses stay unranked. No new work after stop.
        """
        limit = self.progress['concurrency']
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('Link-ranking concurrency must be an integer from 1 to 16.')
        inbox, active, next_batch = Queue(), {}, 0

        def updates():
            """Count actual calls on the only state-writing thread.

            Args: None; drains this attempt's worker notification queue.
            Returns: None; publishes only when dispatch counts change.
            """
            changed = False
            while True:
                try:
                    inbox.get_nowait()
                except Empty:
                    break
                self.progress['model_calls'] += 1; changed = True
            if changed:
                self.persist()

        with ThreadPoolExecutor(max_workers=limit,thread_name_prefix='clarity-link-ranking') as pool:
            while active or (next_batch < len(batches) and not self.stop.is_set()):
                while len(active) < limit and next_batch < len(batches) and not self.stop.is_set():
                    batch = batches[next_batch]; next_batch += 1; index = next_batch
                    future = pool.submit(rank_batch,self,index,[dict(row) for row in batch],inbox,self.cached_batches.get(index))
                    active[future] = (index,batch)
                if not active:
                    break
                done, _ = wait(active,timeout=0.1,return_when=FIRST_COMPLETED); updates()
                for future in done:
                    index,batch = active.pop(future); receipt = future.result(); updates()
                    if receipt is None:
                        continue
                    if receipt['status']=='checks_pass':
                        ratings = {item['id']:item for item in receipt['ratings']}
                        for row in batch:
                            rating = ratings[row['id']]
                            row.update(status='ranked',rating=rating['rating'],title_score=TITLE_POINTS[rating['rating']],reason=rating['reason'])
                        if receipt.get('reused_from'):
                            self.progress['batches_reused'] += 1
                    else:
                        for row in batch:
                            row['status'] = 'failed'
                        self.progress['issues'].append(f'Title batch {index} failed; its links remain unranked.')
                    save(self.root/f'batch-{index:04d}.json',receipt,'')
                    self.progress['batches_finished'] += 1; self.persist()
        updates()
        if self.stop.is_set():
            raise InterruptedError()

    def execute(self):
        """Rate at most 500 shortlisted titles in at most twenty batches per attempt.

        Args: None.
        Returns: None; publishes completed/partial/failed/cancelled state and saves
            receipts. Partial ranking may proceed to reporting; all failed title
            batches with no ranked candidates stop the graph. Never fetches links,
            retries requests, or changes report evidence.
        """
        try:
            self.collect()
            pending = [row for row in self.rows if row['status'] == 'pending']
            batches = [pending[i:i+TITLE_BATCH_SIZE] for i in range(0, len(pending), TITLE_BATCH_SIZE)]
            self.progress['batches_total'] = len(batches)
            self.load_cached_batches(batches)
            self.rank_batches(batches)
            self.progress['status'] = ('partial' if self.progress['candidates_ranked'] else 'failed') if self.progress['issues'] else 'completed'
            if self.stop.is_set():
                self.progress['status'] = 'cancelled'
        except InterruptedError:
            self.progress['status'] = 'cancelled'
        except Exception:
            self.progress.update(status='failed', error='Link ranking stopped unexpectedly; saved evidence is unchanged.')
        self.progress['finished_at'] = datetime.now(timezone.utc).isoformat()
        self.state['status'] = 'link_ranking_' + self.progress['status']
        if self.progress['status'] in ('completed', 'partial') and not self.stop.is_set():
            self.state['status'] = 'link_ranking_running'
        self.persist()
        save(self.root / 'summary.json', self.progress, '')
