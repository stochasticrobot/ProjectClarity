"""Build a large report with small, saved and independently checked requests.

Facts are assessed in batches. Findings for equal entity/topic labels are then
reviewed in bounded groups, and batch summaries are combined in bounded rounds.
The final document is assembled locally, never sent back as one enormous request.
Successful responses can be reused after an explicit retry; source data and failed
attempts are retained. This checks coverage and citations, not semantic accuracy.
"""
import json
from collections import defaultdict
from projectclarity.model_access.output_recovery import validated_output
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.model_access.model_client import request_for
from projectclarity.model_access.model_ledger import checked_call
from projectclarity.reporting.report_payloads import (
    INPUT_TOKEN_LIMIT, MAX_BATCH_FACTS, MAX_MERGE_ITEMS, canonical_json,
    compact_packet, digest, entry_key, fact_group_key, request_size,
)
from projectclarity.reporting.report_merging import MergedEntries, MergedSummary, merge_inputs, validate_merge


class BoundedReportBuilder:
    """Own request planning, validated response reuse and bounded combination."""

    def __init__(self, owner, evidence_hash):
        """Bind one report attempt to exact evidence and its saved history.

        Args:
            owner: ReportRun with packet, state, root, publish, caller and stop.
            evidence_hash: SHA256 of the unchanged report-knowledge input file.
        Returns: None; creates an attempt-local task directory and indexes earlier
            raw task receipts from this session only, including failed responses that
            the recovery policy can salvage. Invalid cache files are
            ignored; responses are revalidated against current inputs before reuse.
            Reuse is limited to the same provider, model, output-contract identity
            and request fingerprint. Legacy tasks without a matching contract are
            retained but are not silently reused by a new real provider caller.
        """
        self.owner = owner
        self.evidence_hash = evidence_hash
        self.tasks = owner.root / 'tasks'
        self.tasks.mkdir(mode=0o700)
        self.cache = {}
        self.splits = 0
        self.retained_fragments = []
        self.split_failures = []
        for prior in owner.state.get('report_history', []):
            relative = prior.get('artifact_directory')
            if not relative:
                continue
            folder = (owner.session_root / relative).resolve()
            if not folder.is_relative_to(owner.session_root.resolve()):
                continue
            for path in (folder / 'tasks').glob('*.json'):
                try:
                    receipt = json.loads(path.read_text())
                    if receipt.get('contract_id') == getattr(owner.caller, 'contract_id', None) and receipt.get('provider', 'anthropic') == getattr(owner.caller, 'provider', 'anthropic') and receipt.get('status') in ('checks_pass', 'response_received', 'failed') and receipt.get('evidence_sha256') == evidence_hash and digest(receipt['request']) == receipt['request_sha256']:
                        self.cache[receipt['request_sha256']] = {**receipt, '_source_artifact': str(path.relative_to(owner.session_root))}
                except (ValueError, KeyError, OSError):
                    continue
        owner.progress.update(phase='Preparing batches', batches_finished=0,
                              batches_total=0, reused_tasks=0, merge_tasks_finished=0,
                              input_token_limit=INPUT_TOKEN_LIMIT, largest_counted_input=0)

    def checkpoint(self, receipt):
        """Atomically replace only this attempt's current task receipt.

        Args:
            receipt: Request-bound raw response, status and validation metadata.
        Returns: None; owner-only temporary file replaces the current receipt.
            Historical attempts are never overwritten. Write failures propagate.
        """
        path = self.tasks / (receipt['request_sha256'] + '.json')
        temporary = path.with_suffix('.tmp')
        save(temporary, receipt, '')
        temporary.replace(path)

    def request(self, kind, items):
        """Construct the complete prompt, schema and compact input for one task.

        Args:
            kind: facts, entries or summary.
            items: Whole original facts or structured findings assigned to the task.
        Returns: Provider-ready request. Every task includes unchanged question and
            requirements. Output allowance is separate from the input-token limit.
        """
        from projectclarity.reporting.report_generation import ReportResult
        packet = self.owner.packet
        if kind == 'facts':
            data = compact_packet(packet, items)
            prompt, schema = 'legacy_prepare_report_batch.txt', ReportResult
        else:
            data = {key: packet[key] for key in ('research_question', 'requirements', 'coverage')}
            data['findings'] = merge_inputs(items, kind)
            prompt, schema = ('merge_report_entries.txt', MergedEntries) if kind == 'entries' else ('merge_report_summary.txt', MergedSummary)
        request = request_for(prompt, data, schema, self.owner.caller.model)
        request.update(max_tokens=32768, timeout=180)
        return request

    def groups(self, kind, items):
        """Pack whole records with byte-based headroom before adapter admission checks.

        Args:
            kind: Task kind selecting request shape and maximum records.
            items: Ordered full facts or findings, never truncated.
        Returns: Ordered groups with each item present once. UTF8 bytes/2 plus 1024
            is only a planning estimate, not a token guarantee; dispatch checks the
            adapter count (explicitly estimated for Nova) and splits again if needed. Single oversized items are
            retained here so dispatch can explain the failure without data loss.
        """
        maximum = MAX_BATCH_FACTS if kind == 'facts' else MAX_MERGE_ITEMS
        groups, batch = [], []
        for item in items:
            proposed = batch + [item]
            if batch and (len(proposed) > maximum or request_size(self.request(kind, proposed)) / 2 + 1024 > INPUT_TOKEN_LIMIT):
                groups.append(batch)
                batch = []
            batch.append(item)
        if batch:
            groups.append(batch)
        return groups

    def validate(self, text, kind, items):
        """Validate exactly the records assigned to this request, including replays.

        Args:
            text: Raw saved or newly returned model text.
            kind: facts, entries or summary.
            items: Exact task inputs with original citation references.
        Returns: Validated report fragment or expanded merged findings. Raises for
            invalid citations, changed fields or erased known conflicts. Fact batches
            retain private omission/quarantine metadata for bounded recovery.
        """
        from projectclarity.reporting.report_generation import validate_report
        if kind == 'facts':
            from projectclarity.reporting.report_batch_recovery import inspect_batch
            return inspect_batch(text, {**self.owner.packet, 'facts': items})
        return validate_merge(text, items, self.owner.requirements, kind)

    def dispatch(self, kind, items, recovery=False):
        """Count before generation, recursively split overflow and save checked work.

        Args:
            kind: facts, entries or summary.
            items: Whole input records for this tentative request.
            recovery: True for the single allowed omitted-fact recovery pass.
        Returns: List of accepted task outputs (more than one after splitting).
            Live transports use the adapter's full-request count or labelled estimate. Offline test
            transports without a counter use UTF8 bytes plus overhead, explicitly
            labelled as an offline bound. Count failures prevent generation. An
            oversized single record fails without truncation. At most one targeted recovery pass assesses omitted facts; failed
            or still-incomplete recovery requires an explicit user retry. Item-mode split
            children continue independently after output failures. Opted-in item
            calls retry rejected output once using the identical counted request;
            both attempts consume budget and retain separate receipts.
        """
        owner = self.owner
        if owner.stop.is_set():
            raise InterruptedError('Stopped before the next report task.')
        request = self.request(kind, items)
        key = digest(request)
        receipt = {'request': request, 'request_sha256': key,
                   'provider': getattr(owner.caller, 'provider', 'anthropic'),
                   'contract_id': getattr(owner.caller, 'contract_id', None),
                   'evidence_sha256': self.evidence_hash, 'kind': kind,
                   'status': 'failed', 'input_items': len(items), 'attempted_calls': 0}
        cached = self.cache.get(key)
        partial_outputs=[]
        preserved_outputs=[]
        try:
            if cached:
                try:
                    result = self.validate(cached['raw_text'], kind, items)
                    if kind == 'facts' and result.get('_issues'):
                        raise ValueError('Saved fields still need a fresh assessment.')
                    missing = result.get('_recovery', {}).get('missing_fact_ids', []) if kind == 'facts' else []
                    # A reply with no usable coverage (or incomplete recovery) must
                    # get a fresh assessment on explicit retry, not fail forever.
                    if missing and (recovery or len(missing) == len(items)):
                        raise ValueError('Cached response still needs a fresh assessment.')
                    if cached['status'] == 'checks_pass' and digest(result) != cached['result_sha256'] and (kind != 'facts' or digest({k: v for k, v in result.items() if k != '_recovery'}) != cached['result_sha256']):
                        raise ValueError('Saved task result fingerprint differs.')
                except (ValueError, KeyError):
                    cached = None
            if cached:
                receipt['output_attempts']=cached.get('output_attempts',[])
                for prior_attempt in receipt['output_attempts']:
                    prior_text=prior_attempt.get('raw_text')
                    if not prior_text or prior_text==cached['raw_text']:
                        continue
                    try:
                        prior_output=self.validate(prior_text,kind,items)
                    except ValueError:
                        continue
                    if prior_output.get('_issues') and all(digest(part)!=digest(prior_output) for part in partial_outputs):
                        partial_outputs.append(prior_output)
                receipt.update(raw_text=cached['raw_text'], reused=True, reused_from=cached['_source_artifact'],
                               input_tokens=cached.get('input_tokens'), count_method=cached.get('count_method'))
                owner.progress['reused_tasks'] += 1
            else:
                counter = getattr(owner.caller, 'count_input_tokens', None)
                count = counter(request) if counter else request_size(request) + 1024
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError('Input token count was not a nonnegative integer.')
                method = (getattr(owner.caller, 'count_method', None) or 'provider_count') if counter else 'offline_utf8_bound'
                receipt.update(input_tokens=count, count_method=method)
                if count > INPUT_TOKEN_LIMIT:
                    self.splits += 1
                    receipt['status'] = 'split_before_generation'
                    self.checkpoint(receipt)
                    if len(items) == 1:
                        raise ValueError(f'A single {kind} record exceeds the {INPUT_TOKEN_LIMIT:,}-token input limit; no text was truncated or sent.')
                    middle = len(items) // 2
                    if kind == 'facts':
                        owner.progress['batches_total'] += 1
                    results=[]
                    failures_before=len(self.split_failures)
                    for child in (items[:middle], items[middle:]):
                        retained_before=len(self.retained_fragments)
                        try:
                            results.extend(self.dispatch(kind, child, recovery))
                        except Exception as error:
                            if (not getattr(self, 'allow_partial', False)
                                    or isinstance(error, (InterruptedError, OSError)) or owner.stop.is_set()):
                                raise
                            # Failure belongs to this child, not its unattempted sibling.
                            # Retain validated parent/recovery fragments once, in input order.
                            results.extend(self.retained_fragments[retained_before:])
                            self.split_failures.append(str(error))
                    receipt['status']='split_with_failures' if len(self.split_failures)>failures_before else 'split_before_generation'
                    return results
                if owner.stop.is_set():
                    raise InterruptedError('Stopped after token counting, before generation.')
                owner.progress['largest_counted_input'] = max(owner.progress['largest_counted_input'], count)
                def before_call():
                    """Check cancellation and count each explicit generation attempt.

                    Args: None.
                    Returns: None; updates private progress before dispatch. Raises
                        InterruptedError on stop; caller budgets gate actual requests.
                    """
                    if owner.stop.is_set():
                        raise InterruptedError('Stopped before report output retry.')
                    receipt['attempted_calls'] += 1
                    owner.progress['model_calls'] += 1
                    owner.publish(owner.state)
                def validate(text):
                    """Checkpoint returned text before checking it, retaining failures.

                    Args: text: Exact completed provider response.
                    Returns: Validated task output; exceptions keep the raw receipt.
                    """
                    receipt.update(raw_text=text, status='response_received')
                    self.checkpoint(receipt)
                    output=self.validate(text, kind, items)
                    if getattr(self, 'recover_invalid_output', False) and output.get('_issues'):
                        if all(digest(part)!=digest(output) for part in partial_outputs):
                            partial_outputs.append(output)
                        raise ValueError('Report output contains malformed fields; valid cited fields are retained for recovery.')
                    return output
                label=f'report-{owner.root.name}-{kind}-{key[:12]}'
                if getattr(self, 'recover_invalid_output', False):
                    def record(attempt):
                        """Retain each rejected/successful output before any retry.

                        Args: attempt: Completed attempt audit from output recovery.
                        Returns: None; saves history without altering earlier ledger calls.
                        """
                        receipt.setdefault('output_attempts', []).append(attempt)
                        self.checkpoint(receipt)
                    result = validated_output(owner.caller, label, request,
                        owner.state['session_id'], validate, before_call, record)
                else:
                    before_call()
                    result = checked_call(owner.caller, label, request, owner.state['session_id'], validate)
            # A valid reassessment may omit an earlier valid contribution. Preserve
            # those contributions too; assembly retains alternatives/conflicts.
            for partial in partial_outputs:
                preserved={k:v for k,v in partial.items() if k not in ('_issues','_recovery')}
                preserved_outputs.append(preserved)
                self.retained_fragments.append(preserved)
            if partial_outputs:
                receipt['superseded_field_issues']=[issue for part in partial_outputs for issue in part.get('_issues',[])]
                receipt['preserved_outputs']=preserved_outputs
            receipt['result_sha256'] = digest(result)
            details = result.pop('_recovery', None) if kind == 'facts' else None
            missing = details['missing_fact_ids'] if details else []
            if details:
                receipt['recovery'] = details
            if getattr(self, 'allow_partial', False):
                self.retained_fragments.append(result)
            if missing and (recovery or len(missing) == len(items)):
                raise ValueError('Omitted facts remain after bounded recovery; saved findings retained for retry.')
            recovered = []
            if missing:
                owner.progress['batches_total'] += 1
                recovered = self.dispatch('facts', [item for item in items if item['id'] in missing], recovery=True)
            receipt['status'] = 'checks_pass'
            if kind == 'facts':
                owner.progress['batches_finished'] += 1
                owner.progress['facts_accounted'] += len(items) - len(missing)
            else:
                owner.progress['merge_tasks_finished'] += 1
            return preserved_outputs + [result] + recovered
        except Exception as error:
            if partial_outputs and not preserved_outputs:
                # Both attempts may fail. Publish only the already validated subset,
                # retain unresolved-field diagnostics, and never invent exclusions.
                for partial in partial_outputs:
                    self.retained_fragments.append({k:v for k,v in partial.items() if k!='_recovery'})
                receipt['retained_partial_outputs']=partial_outputs
            receipt['status'] = 'failed'
            receipt['error'] = {'type': type(error).__name__, 'message': str(error)[:500]}
            raise
        finally:
            self.checkpoint(receipt)
            owner.publish(owner.state)

    def merge(self, kind, items):
        """Combine bounded groups repeatedly without an unbounded final request.

        Args:
            kind: entries or summary.
            items: Findings for one label group, or the report's summary paragraphs.
        Returns: Expanded merged findings. Ambiguous equal-label entries can stay
            separate with a visible gap. Summary rounds that cannot shrink fail
            visibly rather than dropping coverage. At most eight rounds run.
        """
        if len(items) < 2:
            return items
        for _ in range(8):
            batches = self.groups(kind, items)
            result = []
            prior_splits = self.splits
            for batch in batches:
                if len(batch) == 1:
                    result.extend(batch)
                else:
                    for output in self.dispatch(kind, batch):
                        result.extend(output)
            if len(batches) == 1 and self.splits == prior_splits:
                return result
            if len(result) >= len(items) and request_size({'findings': merge_inputs(result, kind)}) >= request_size({'findings': merge_inputs(items, kind)}):
                if kind == 'entries':
                    self.merge_gaps.append('Some similarly named findings remain separate because identity or compact combination could not be established.')
                    return result
                raise ValueError('Summary merging did not reduce the bounded inputs; saved findings are retained.')
            items = result
            if len(items) < 2:
                return items
        raise ValueError('Report merging reached its eight-round bound; saved findings are retained.')

    def build(self):
        """Assess all facts, merge findings and validate the assembled full report.

        Args: None.
        Returns: Final report structure with complete original-fact accounting.
            Saves each completed task; a failed batch prevents publication, while
            other independent batches can still finish for a later explicit retry.
            Cancellation stops new scheduling and drains started batches. Evidence
            assessment is bounded by the saved report-selection limit; merges remain
            sequential. No research is repeated.
        """
        from projectclarity.reporting.report_generation import validate_report
        owner = self.owner
        batches = self.groups('facts', sorted(owner.packet['facts'], key=fact_group_key))
        owner.progress.update(phase='Assessing evidence batches', batches_total=len(batches))
        from projectclarity.reporting.concurrent_report_selection import select_report_batches
        outputs = select_report_batches(self, batches)
        if owner.stop.is_set():
            raise InterruptedError('Stopped before report combination.')
        owner.progress['phase'] = 'Combining structured findings'
        owner.publish(owner.state)
        self.merge_gaps = []
        grouped = defaultdict(list)
        for output in outputs:
            for entry in output['entries']:
                grouped[entry_key(entry)].append(entry)
        entries = []
        for group in grouped.values():
            entries.extend(self.merge('entries', group))
        owner.progress['phase'] = 'Combining report summary'
        owner.publish(owner.state)
        summaries = [part for output in outputs for part in output['summary']]
        # A single batch is already a fully validated small report; do not pay for
        # a second narrative request merely to restate the same accepted response.
        summary = self.merge('summary', summaries) if len(outputs) > 1 else summaries
        result = {'entries': entries, 'summary': summary,
                  'gaps': list(dict.fromkeys([gap for output in outputs for gap in output['gaps']] + self.merge_gaps)),
                  'exclusions': [item for output in outputs for item in output['exclusions']]}
        result = validate_report(canonical_json(result), owner.packet)
        owner.progress['phase'] = 'Saving final report'
        return result
