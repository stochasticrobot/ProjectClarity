"""Produce a single-entity report through the real shared bounded execution path.

Profiles have their own prompts, identity checks and assembly, but use the same
budgets, receipts, retry bound, batch splitting and saved-response validation as
directories. No final unbounded synthesis or silent directory fallback occurs.
"""
from projectclarity.reporting.batched_reporting import BoundedReportBuilder
from projectclarity.reporting.concurrent_report_selection import select_report_batches
from projectclarity.reporting.report_payloads import fact_group_key
from projectclarity.reporting.entity_profile.selection import profile_request, validate_profile_batch
from projectclarity.reporting.entity_profile.assembly import assemble_profile, validate_profile


class EntityProfileBuilder(BoundedReportBuilder):
    """Type-specific execution using shared scheduling and durable recovery."""
    allow_partial = True
    recover_invalid_output = True

    def request(self, kind, items):
        """Prepare one profile contribution request using the frozen subject scope.

        Args: kind: Must be facts; items: Whole reviewed records in this batch.
        Returns: Profile request; other task kinds raise before dispatch. No I/O.
        """
        if kind != 'facts':
            raise ValueError('Profiles do not use model merge tasks.')
        return profile_request(self.owner.packet, items, self.owner.caller.model)

    def validate(self, text, kind, items):
        """Check returned or cached evidence contributions under current profile rules.

        Args: text: Raw completed JSON; kind: facts; items: Exact offered evidence.
        Returns: Checked profile fragment with omission metadata; invalid output
            raises ValueError and remains eligible for the shared single retry.
        """
        if kind != 'facts':
            raise ValueError('Unexpected profile task kind.')
        return validate_profile_batch(text, self.owner.packet, items)

    def build(self):
        """Assess all evidence, assemble the profile and retain precise gap accounting.

        Args: None; uses this attempt's owner and saved plan.
        Returns: Checked profile with all requested sections. Failed independent
            batches retain successful siblings as a partial report. No usable
            assessment raises; empty input yields an unknown profile without calls.
            Stop prevents publication. Missing evidence is a gap, not invented prose.
        """
        owner = self.owner
        batches = self.groups('facts', sorted(owner.packet['facts'], key=fact_group_key))
        owner.progress.update(phase='Assessing profile evidence', batches_total=len(batches))
        fragments = select_report_batches(self, batches) if batches else []
        if owner.stop.is_set():
            raise InterruptedError('Stopped before profile assembly.')
        owner.progress['phase'] = 'Assembling entity profile'
        owner.publish(owner.state)
        result = assemble_profile(fragments, owner.packet)
        used = {fid for finding in result['summary'] + [f for s in result['profile']['sections'] for f in s['findings']]
                for fid in finding['fact_ids']}
        covered = used | {r['fact_id'] for r in result['exclusions']}
        if owner.packet['facts'] and not covered:
            raise ValueError('No profile evidence could be assessed; saved batches remain available for retry.')
        unreviewed = [f['id'] for f in owner.packet['facts'] if f['id'] not in covered]
        partial = bool(unreviewed or owner.progress.get('failed_batches'))
        if partial:
            result['gaps'].append(f'Partial profile: {len(unreviewed)} input facts remain unreviewed; failed batches remain available for retry.')
        result = validate_profile(result, {**owner.packet, 'facts': [f for f in owner.packet['facts'] if f['id'] in covered]})
        quarantined = sum(len(part.get('quarantined_fields', [])) for part in fragments)
        owner.packet['coverage'].update(unreviewed_report_fact_ids=unreviewed, unreviewed_report=len(unreviewed),
            withheld_invalid_fields=quarantined, ambiguous_identity_facts=len({r['fact_id'] for r in result['profile']['identity_uncertainty']}))
        owner.progress.update(partial=partial, facts_accounted=len(covered), unreviewed_facts=len(unreviewed),
                              withheld_invalid_fields=quarantined, phase='Saving final report')
        return result
