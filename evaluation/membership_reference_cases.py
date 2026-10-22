"""Load the dated reference answers for the first legislature membership experiments.

The JSON file contains research questions, source URLs and independently checked
reference names/roles. The assistant verified this small set against official
records and Wikipedia; it is not a human-labelled benchmark or a national roster.
This module loads data only; it never fetches sources or invokes the model.
"""
import json
from pathlib import Path

CASE_FILE = Path(__file__).parent / 'cases' / 'legislature_membership.json'


def load_suite():
    """Read the checked-in reference version; callers must preserve its date and review status."""
    return json.loads(CASE_FILE.read_text(encoding='utf-8'))


def get_case(suite, case_id):
    """Select an explicit case, failing on a typo instead of quietly running a different question."""
    for case in suite['cases']:
        if case['case_id'] == case_id:
            return case
    raise ValueError('Unknown evaluation case: ' + case_id)


def reference_validation(suite):
    """Report the review recorded in this exact answer-key version, never a newer one.

    Old experiment copies predate source-verification metadata. Keep their
    historical human-review label but do not silently declare them verified.
    This reads an audit record; it does not recheck today's live membership.
    """
    return suite.get('reference_validation', {
        'status': 'not_recorded', 'reviewer': 'unspecified',
        'human_reference_review': suite.get('human_reference_review', 'not_recorded')})
