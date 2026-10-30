"""Explain invalid model-response fields without copying rejected private values.

Structured response parsing can fail because a required field is missing or a rule
was violated. These helpers turn parser exceptions into a limited list of known
field names and application-written explanations for saved diagnostics or an
explicit correction request. Arbitrary exception text and model-invented field
names are not copied. Another helper distinguishes unresolved failed calls from
calls replaced by an explicitly accepted correction. This module does not itself
retry requests or decide whether a replacement answer is acceptable.
"""
from pydantic import ValidationError


MESSAGES = {
    'missing': 'Required field is missing.',
    'extra_forbidden': 'An unexpected field was supplied; its name is omitted.',
    'string_type': 'Expected text.',
    'list_type': 'Expected a list.',
    'model_type': 'Expected an object with the required fields.',
    'string_too_short': 'Text is empty or shorter than the schema permits.',
    'string_too_long': 'Text exceeds the schema length limit.',
    'string_pattern_mismatch': 'Identifier does not match the required format.',
    'too_short': 'List has fewer entries than the schema requires.',
    'too_long': 'List has more entries than the schema permits.',
    'int_type': 'Expected a whole number.',
    'literal_error': "Value is not one of the schema's permitted choices.",
    'greater_than_equal': 'Number is below the permitted minimum.',
    'less_than_equal': 'Number exceeds the permitted maximum.',
    'validation_error': 'A schema validation rule failed.',
    'unverified_completion_target': 'Only an explicit user-requested count can be set before research.',
    'missing_requested_target': 'A user-requested target requires its count.',
    'none_required': 'Expected null for this alternative.',
    'json_invalid': 'The reply must be a complete JSON object.',
    'report_fields_invalid': 'Report fields must be unique and not provenance fields.',
    'report_identity_invalid': 'Identity fields must be unique names of defined report fields.',
    'narrative_fields_invalid': 'Narrative sections require empty fields and identity_fields.',
    'report_identity_missing': 'Tabular sections require fields and explicit identity_fields.',
    'report_goals_duplicate': 'Report section goal identifiers must be unique.',
    'answer_checks_duplicate': 'Completion check identifiers must be unique.',
    'answer_goals_mismatch': 'Completion checks must cover exactly the report section goal IDs.',
    'nonnumeric_target': 'Constraints and evidence sufficiency require not_applicable and a null count.',
    'enumeration_target_missing': 'Enumeration requires research or user_request, never not_applicable.',
    'answer_inventory_mismatch': 'A report-bound check must enumerate only its section goal and copy its unit exactly.',
}


def validation_issues(error, schema):
    """Extract safe field/rule feedback from a wrapped structured-output failure.

    Args:
        error: Parser exception or None; causes are traversed without their text.
        schema: Expected Pydantic model; its declared field/type names are safe.
    Returns:
        At most eight JSON-compatible path/code/message records. Unknown rules or
        parser failures receive generic feedback. No input values or I/O escape.
    """
    spec = schema.model_json_schema()
    names = set(spec.get('properties', {})) | set(spec.get('$defs', {}))
    for definition in spec.get('$defs', {}).values():
        names.update(definition.get('properties', {}))
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, ValidationError):
            issues = []
            for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
                parts = [str(p) if type(p) is int and 0 <= p <= 10000 else
                         p if isinstance(p, str) and p in names else '<unknown-field>'
                         for p in item['loc'][:8]]
                code = item['type'] if item['type'] in MESSAGES else 'validation_error'
                issues.append(dict(path='.'.join(parts)[:240] or 'answer',
                    code=code, message=MESSAGES[code]))
            return issues
        error = error.__cause__ or error.__context__
    return [dict(path='answer', code='validation_error',
                 message='The reply did not match the supplied response schema.')]


def unresolved_failures(session):
    """Return failed model calls that have not been replaced by an explicitly accepted correction.

    A later successful call is not automatically a repair of an earlier one. Only a
    saved resolution linking the rejected call to its accepted replacement removes
    that failure from the unresolved list.

    Args:
        session: Run whose owning nodes journal call-ID resolutions only after
            accepting a replacement result; historical receipts remain untouched.
    Returns:
        Failed receipts without a completed same-task replacement explicitly
        accepted by their domain node. Missing, self-referential, failed or
        cross-task resolution links cannot hide a failure. Does not mutate state.
    """
    calls = {c['call_id']: c for c in session.processing_calls}
    resolutions = session.workflow.get('model_call_resolutions', {})
    failures = []
    for call in session.processing_calls:
        if not call.get('error_code') or call.get('status') != 'failed':
            continue
        replacement = calls.get(resolutions.get(call['call_id']), {})
        if (replacement.get('status') == 'completed'
                and not replacement.get('error_code')
                and replacement.get('task') == call['task']):
            continue
        failures.append(call)
    return failures
