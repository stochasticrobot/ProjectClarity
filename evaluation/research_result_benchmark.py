"""Score supported final report entries against a frozen Parliament page's answer key.

Unlike the isolated extraction scorer, this command reads the result records
produced by a normal research run. The caller names the fields holding the member's
name and riding (electoral district). The scorer checks source integrity, counts
complete supported pairs, and reports missing, unexpected and incomplete records.
It is read-only and makes no model call. Scores measure agreement with the dated
page reference, not independently verified present-day membership.
"""
import argparse
import json
from pathlib import Path

from evaluation.page_extraction_scoring import person_name, score_page
from evaluation.membership_result_scoring import normalize_name, relation_metrics
from projectclarity.provisional_answer import read_saved_answer


def score_results(directory, reference_path, name_field, riding_field):
    """Compare supported final records with the independent reference for their page.

    Args:
        directory: Finished or in-progress production run with one frozen source.
        reference_path: Source-local reference JSON, never used during model calls.
        name_field: Explicit planner-selected record field containing the person's name.
        riding_field: Explicit planner-selected record field containing the riding.
    Returns:
        Scores, missing/extra pairs and failure/unfinished counts. Source hashes and
        anchors are checked through existing production readers. Normalization uses
        the existing finite name-title/order policy, not fuzzy matches. Incomplete
        records are reported but not promoted; historical extras are not silently
        filtered from final supported results. No writes, network or model calls occur.
    """
    extraction = score_page(directory, reference_path)
    reference = json.loads(reference_path.read_text())
    answer = read_saved_answer(directory.parent, directory.name)
    expected = {(person_name(r['mp']), normalize_name(r['riding'])) for r in reference['expected_pairs']}
    goals = []
    for goal in answer.get('progress', {}).get('goals', []):
        results = goal['results']
        if results is None or not {name_field, riding_field} <= set(results['required_fields']):
            continue
        supported, pending, incomplete = set(), set(), set()
        for row in results['records']:
            values = row['values']
            if not values.get(name_field) or not values.get(riding_field):
                continue
            pair = (person_name(values[name_field]), normalize_name(values[riding_field]))
            if row['status'] == 'supported':
                supported.add(pair)
            elif row['status'] == 'incomplete':
                incomplete.add(pair)
            else:
                pending.add(pair)
        goals.append({'goal_id': goal['goal_id'], 'required_fields': results['required_fields'],
            'supported_complete_pairs': relation_metrics(expected, supported),
            'pending_pairs': relation_metrics(expected, pending),
            'incomplete_observation_pairs': relation_metrics(expected, incomplete),
            'reported_complete_records': results['supported']})
    if not goals:
        raise ValueError('No result shape contains both explicitly selected field names.')
    return {'run_id': directory.name, 'policy': 'final-results-v1-exact-pairs',
        'source_sha256': extraction['source_sha256'], 'reference_sha256': extraction['reference_sha256'],
        'field_mapping': {'name': name_field, 'riding': riding_field}, 'goals': goals,
        'anchored_relationship_pairs': extraction['anchored_pairs'],
        'failed_batches': extraction['failed_batches'], 'unfinished_batches': extraction['unfinished_batches'],
        'integrity_excluded': answer['integrity_excluded'], 'status': answer['status'],
        'scope': 'Final source-supported pair fidelity, not independently verified present-day membership.'}


def main(argv=None):
    """Print read-only scores for a completed or partial production roster run.

    Args:
        argv: Optional explicit arguments; None reads the process command line.
    Returns:
        Zero only for exact supported-pair agreement and no unfinished/failed batches;
        otherwise one. Invalid source hashes or field mappings raise without scoring.
        Scores go to stdout so operators choose where to save their private audit.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--name-field', required=True)
    parser.add_argument('--riding-field', required=True)
    args = parser.parse_args(argv)
    result = score_results(args.directory, args.reference, args.name_field, args.riding_field)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result['failed_batches'] or result['unfinished_batches']) or
        any(g['supported_complete_pairs']['f1'] != 1 for g in result['goals']))


if __name__ == '__main__':
    raise SystemExit(main())
