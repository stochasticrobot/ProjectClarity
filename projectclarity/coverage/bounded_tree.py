"""Build a bounded, content-addressed coverage reduction graph without model I/O.

Source claims belong to exactly one leaf. A reducer receives at most four child
summaries, never all leaves or all report records. Full accounting lives in these
application descriptors and persisted receipts; only bounded witness references
travel upward. Summaries remain model interpretations, not lossless evidence.
"""
import json

from projectclarity.coverage.evidence_batches import fingerprint
from projectclarity.discovery.inventory_context import payload_messages
from projectclarity.source_context import source_context
from projectclarity.task_inputs import input_receipt

MAX_LEAF_INPUTS = 32
FAN_IN = 4
FLAGS = ('unresolved_conflict', 'unresolved_identity', 'requires_detail')


def candidate_input(session, candidate):
    """Project one source-checked claim without copying its entire review journal.

    Args:
        session: Run owning the original source context.
        candidate: Source-checked typed claim; evidence must already be validated.
    Returns:
        Complete statement, quotes, qualifications, roles, status and source context
        for a leaf. No report inventory, global study or other claims are attached.
    """
    return dict(input_id=candidate.candidate_id, candidate_id=candidate.candidate_id,
        source_id=candidate.source_id, statement=candidate.proposal.statement,
        goal_ids=candidate.proposal.goal_ids, status=candidate.adjudication_status,
        roles=candidate.proposal.research_roles, qualifications=candidate.proposal.qualifications,
        quotes=[e.quote for e in candidate.evidence],
        source_context=source_context(session, candidate.source_id))


def descriptor(stage, context, inputs, system, schema):
    """Bind one graph node to its exact bounded request and response contract.

    Args:
        stage: evidence for original claims or reduction for child summaries.
        context: Fixed accepted requirements and scope, not mutable progress.
        inputs: At most 32 claims or four child summaries.
        system: Approved task prompt loaded by the orchestrator.
        schema: Approved bounded digest response class.
    Returns:
        Descriptor with source context shared once per source, exact payload/hash,
        input IDs, allowed witnesses and budget
        receipt. It is not a model judgment and performs no calls or persistence.
    """
    sources = {row['source_id']: row['source_context'] for row in inputs} if stage == 'evidence' else {}
    payload = dict(stage=stage, context=context,
        inputs=[{k:v for k,v in row.items() if k != 'source_context'} for row in inputs], sources=sources)
    messages = payload_messages(system, payload)
    receipt = input_receipt('bounded_evidence', messages, schema)
    token = fingerprint([messages, schema.model_json_schema()])
    return dict(batch_id='BT_' + token, stage=stage, payload=payload,
        input_ids=[row['input_id'] for row in inputs],
        candidate_ids=[row['candidate_id'] for row in inputs] if stage == 'evidence' else [],
        witness_pool=sorted({cid for row in inputs for cid in
            ([row['candidate_id']] if stage == 'evidence' else row['witness_candidate_ids'])}),
        required_flags={flag: stage == 'reduction' and any(row[flag] for row in inputs) for flag in FLAGS},
        input_contract=receipt, characters=receipt['total_characters'])


def plan_leaves(session, valid, context, system, schema):
    """Partition all current source-checked evidence before paying for any summaries.

    Args:
        session: Source-context owner.
        valid: Ordered candidate-ID mapping verified by the production node.
        context: Fixed contract, scope and human guidance.
        system: Verbatim approved summary prompt.
        schema: Bounded response class used in size checks and content hashes.
    Returns:
        Ordered leaves accounting for each candidate exactly once. Fixed 32-item
        ranges split recursively by exact serialized size. Oversized single claims
        or base context raise ValueError; nothing is clipped or silently deferred.
    """
    base = descriptor('evidence', context, [], system, schema)
    if base['input_contract']['errors']:
        raise ValueError('bounded_context_too_large')
    rows = []
    for cid, candidate in valid.items():
        if cid != candidate.candidate_id:
            raise ValueError('bounded_candidate_id_mismatch')
        rows.append(candidate_input(session, candidate))
    leaves = []

    def partition(group):
        """Split a complete local group only when its measured request does not fit.

        Args:
            group: Nonempty contiguous claim group from the immutable snapshot.
        Returns:
            None. Appends fitting leaves; raises for an indivisible oversized claim.
        """
        node = descriptor('evidence', context, group, system, schema)
        if not node['input_contract']['errors']:
            leaves.append(node)
        elif len(group) == 1:
            raise ValueError('bounded_item_too_large:' + group[0]['input_id'])
        else:
            middle = len(group) // 2
            partition(group[:middle])
            partition(group[middle:])

    for start in range(0, len(rows), MAX_LEAF_INPUTS):
        partition(rows[start:start + MAX_LEAF_INPUTS])
    return leaves


def validate_summary(node, assessment, schema):
    """Validate accounting and sticky uncertainty independently of model prose.

    Args:
        node: Current exact input descriptor with allowed reference IDs.
        assessment: Parsed or serialized model response.
        schema: Bounded response class; validates lengths as well as field types.
    Returns:
        Typed response. Missing/duplicate input accounting, invented witnesses,
        missing witnesses or cleared child flags raise ValueError. No semantic
        entailment, preservation of every nuance or entity equality is inferred.
    """
    answer = schema.model_validate(assessment)
    ids = answer.covered_input_ids
    if len(ids) != len(set(ids)) or set(ids) != set(node['input_ids']):
        raise ValueError('bounded_input_accounting_mismatch')
    if set(answer.witness_candidate_ids) - set(node['witness_pool']):
        raise ValueError('bounded_unknown_witness')
    if node['witness_pool'] and not answer.witness_candidate_ids:
        raise ValueError('bounded_missing_witness')
    if any(required and not getattr(answer, flag) for flag, required in node['required_flags'].items()):
        raise ValueError('bounded_cleared_child_blocker')
    return answer


def reusable(node, receipt, schema):
    """Require exact inputs and intact, valid outputs before reusing a tree node.

    Args:
        node: Current descriptor built from source-checked evidence or child outputs.
        receipt: Optional saved node journal.
        schema: Current bounded response contract.
    Returns:
        True only for an error-free completed receipt with exact identity, output
        hash and valid accounting/flags. Invalid or stale receipts return False.
    """
    if not (receipt and receipt.get('status') == 'completed' and not receipt.get('error_code')
            and receipt.get('batch_id') == node['batch_id'] and receipt.get('assessment') is not None
            and receipt.get('output_sha256') == fingerprint(receipt['assessment'])):
        return False
    try:
        validate_summary(node, receipt['assessment'], schema)
    except (ValueError, TypeError):
        return False
    return True


def child_input(node, receipt):
    """Expose a bounded summary, not its transitive candidate or child manifest.

    Args:
        node: Completed input descriptor.
        receipt: Already validated matching output receipt.
    Returns:
        Child identifier, bounded summary/witnesses and unresolved flags. The child
        output hash binds ancestors without shipping all descendant identifiers.
    """
    return dict(input_id=node['batch_id'], output_sha256=receipt['output_sha256'],
        **{key: receipt['assessment'][key] for key in
            ('summary', 'witness_candidate_ids', 'limitations', *FLAGS)})


def plan_tree(session, valid, context, system, schema, receipts):
    """Advance a bounded-fan-in reduction graph using only intact completed children.

    Args:
        session: Run owning source context.
        valid: Freshly source-checked candidate snapshot.
        context: Fixed accepted answer requirements.
        system: Verbatim approved summary prompt.
        schema: Current bounded digest contract.
        receipts: Saved summaries indexed by their content-addressed IDs.
    Returns:
        Nodes, pending frontier and optional completed root. Every original claim
        belongs to exactly one leaf. Reducers contain at most four children; a lone
        child is promoted without another call. Oversized reduction inputs fail
        before dispatch instead of falling back to an unbounded concatenation.
    """
    level = plan_leaves(session, valid, context, system, schema)
    nodes = list(level)
    while level:
        pending = [node for node in level if not reusable(node, receipts.get(node['batch_id']), schema)]
        if pending:
            return dict(nodes=nodes, pending=pending, root=None)
        if len(level) == 1:
            return dict(nodes=nodes, pending=[], root=level[0])
        parents = []
        for start in range(0, len(level), FAN_IN):
            group = level[start:start + FAN_IN]
            if len(group) == 1:
                parents.append(group[0])
                continue
            node = descriptor('reduction', context,
                [child_input(child, receipts[child['batch_id']]) for child in group], system, schema)
            if node['input_contract']['errors']:
                raise ValueError('bounded_reduction_too_large')
            parents.append(node)
            nodes.append(node)
        level = parents
    return dict(nodes=[], pending=[], root=None)
