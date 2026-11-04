"""Prepare the requested answer using bounded filters and code-only assembly.

This uses the existing parallel dispatcher, token preflight and exact-request
recovery. Only evidence filtering calls the model; final formatting is local.
"""
from projectclarity.reporting.batched_reporting import BoundedReportBuilder
from projectclarity.reporting.answer_item_selection import selection_request, validate_selection
from projectclarity.reporting.answer_item_assembly import assemble_items
from projectclarity.reporting.concurrent_report_selection import select_report_batches
from projectclarity.reporting.report_payloads import fact_group_key, canonical_json


class ItemReportBuilder(BoundedReportBuilder):
    """Reuse durable bounded execution without the old narrative/merge workflow."""

    allow_partial = True

    def request(self, kind, items):
        """Prepare a filtering request, never a model-written report merge.

        Args: kind: Must be facts; items: Whole reviewed evidence records.
        Returns: Complete request; unexpected task kinds fail before provider work.
        """
        if kind!='facts':raise ValueError('Item reports do not use model merge tasks.')
        return selection_request(self.owner.packet,items,self.owner.caller.model)

    def validate(self, text, kind, items):
        """Validate selected contributions against this batch's complete evidence.

        Args: text: Raw response; kind: facts task; items: Exact input records.
        Returns: Checked fragment with bounded missing-fact recovery metadata.
        """
        if kind!='facts':raise ValueError('Unexpected item report task.')
        return validate_selection(text,self.owner.packet,items)

    def build(self):
        """Filter evidence in parallel, assemble items locally and check accounting.

        Args: None.
        Returns: Final item list with citations and no narrative. Failed batches
            retain usable siblings in an explicitly partial report;
            failed facts remain unreviewed, never exclusions. Stop still cancels.
        """
        from projectclarity.reporting.report_generation import validate_report
        owner=self.owner
        batches=self.groups('facts',sorted(owner.packet['facts'],key=fact_group_key))
        owner.progress.update(phase='Selecting answer items',batches_total=len(batches))
        fragments=select_report_batches(self,batches)
        if owner.stop.is_set():raise InterruptedError('Stopped before answer assembly.')
        owner.progress['phase']='Assembling answer items';owner.publish(owner.state)
        assembled=assemble_items(fragments,owner.packet)
        covered={row['fact_id'] for row in assembled['exclusions']}
        for entry in assembled['entries']:
            covered.update(entry['label_fact_ids'])
            for field in entry['fields']:
                covered.update(field['fact_ids'])
        if owner.packet['facts'] and not covered:
            raise ValueError('No report evidence could be assessed. Saved batches remain available for retry.')
        unreviewed=[f['id'] for f in owner.packet['facts'] if f['id'] not in covered]
        issues=sum(len(fragment.get('_issues',[])) for fragment in fragments)
        partial=bool(unreviewed or issues or owner.progress.get('failed_batches'))
        if partial:
            assembled['gaps'].append(f'Partial report: {len(unreviewed)} of {len(owner.packet["facts"])} input facts remain unreviewed; {issues} invalid fields were withheld. Retry unresolved work to improve coverage.')
        # Validate the accepted subset against its real citations. Preserve the full
        # input separately and list every unreviewed ID; never invent exclusions
        # merely to satisfy the complete-report coverage invariant.
        result=validate_report(canonical_json(assembled),{**owner.packet,
            'facts':[f for f in owner.packet['facts'] if f['id'] in covered]})
        owner.packet['coverage'].update(unreviewed_report_fact_ids=unreviewed,
            unreviewed_report=len(unreviewed), withheld_invalid_fields=issues)
        owner.progress.update(partial=partial, facts_accounted=len(covered),
            unreviewed_facts=len(unreviewed), withheld_invalid_fields=issues)
        owner.progress['phase']='Saving final report'
        return result
