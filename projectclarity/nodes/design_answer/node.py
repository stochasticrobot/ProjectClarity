"""Ask a language model what the final answer must contain before planning research.

For a question asking for researchers and their specialties, the model can define
a report section with name, affiliation and specialty fields, identify which
fields distinguish one person from another, and specify the evidence needed for
completion. This specification is called the answer schema; it describes the
answer's structure and requirements, not the researched answer itself.

The workflow calls design_answer when the design-answer option is enabled. The
accepted specification is saved as answer_schema.json and used by later planning,
extraction and reporting. Requests share the run's model-call allowance. Invalid
output may receive one correction attempt when enough allowance remains for
planning; failure stops the run instead of inventing a replacement specification.
An interrupted request with an unknown outcome is not automatically repeated.
"""
import json
from projectclarity.answer_schema.contracts import AnswerSchema
from projectclarity.coverage.checkpoints import model_request_ceiling
from projectclarity.prompt_loader import load_prompt
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.session_storage import save_session, write_text

SYSTEM_PROMPT = load_prompt('design_answer.txt')


def design_answer(state, output_root, key_reader, monitor=None):
    """Create the report specification that will guide research, or record failure.

    Ask the model to define answer sections, identifying fields, required information
    and completion checks from the user's question. This happens before the research
    plan is generated, so planning can work toward those requirements. Reuse an
    existing specification without another request. If an earlier design request
    started but no accepted specification was saved, fail rather than risk paying
    for the same uncertain request again.

    Args:
        state: Workflow dictionary containing the initialized ResearchSession under
            session. Its visit history, design-attempt records, status, error and
            answer_schema are updated in place.
        output_root: Local directory containing run-ID subdirectories. Session state,
            private model-call records and an accepted answer_schema.json are saved
            beneath the current run's directory.
        key_reader: Zero-argument callable returning the API key when the shared
            model executor needs credentials; the key is not part of saved state.
        monitor: Optional progress observer with cancellation checks, or None.

    Returns:
        A dictionary containing the same mutated session under session. Success sets
        answer_schema to the validated specification; cancellation or a rejected
        design sets the corresponding run status and an error. One completed reply
        rejected for invalid structure may receive one correction if at least two
        requests remain, preserving one for planning. The shared executor separately
        handles transient transport retries. A new design without explicit completion
        checks fails. Existing saved specifications are reused as they are. File-read
        or save failures can propagate; this function does not retrieve sources.
    """
    session = state['session']
    session.visited_nodes.append('design_answer')
    if session.answer_schema is not None:
        return {'session': session}
    if session.workflow.get('answer_design_started'):
        session.status = 'failed'
        session.error = 'Answer design was already attempted; uncertain model calls are not replayed.'
    else:
        session.workflow['answer_design_started'] = True
        save_session(session, output_root)
        payload = {
            'original_question': session.request.question,
            'research_date': session.created_at[:10]}
        for attempt in range(2):
            call_id = 'answer_design' if attempt == 0 else 'answer_design_correction'
            session.workflow['answer_design_attempts'] = attempt + 1
            save_session(session, output_root)
            messages = [('system', SYSTEM_PROMPT), ('human', json.dumps(payload))]
            answer, receipt = invoke_structured(session, output_root, call_id=call_id,
                task='design_answer', prompt_version='answer-design-v4', messages=messages,
                schema=AnswerSchema, key_reader=key_reader, monitor=monitor)
            if (answer is not None or attempt == 1
                    or receipt.get('error_code') != 'invalid_output_schema'
                    or receipt.get('stop_reason') != 'end_turn'
                    or model_request_ceiling(session, 'design_answer') - session.usage.requests_attempted < 2):
                break
            trace = json.loads((output_root / session.session_id / receipt['trace_path']).read_text())
            if trace.get('response_text_truncated') or not trace.get('response_text'):
                break
            # Show the rejected reply and validation issues to the correction
            # request. Do not treat that reply as the accepted report specification.
            payload['correction'] = dict(previous_response=trace['response_text'],
                validation_issues=receipt.get('validation_issues', []))
        if answer is None:
            session.status = 'cancelled' if receipt.get('error_code') == 'cancelled' else 'failed'
            session.error = 'Answer design failed: ' + str(receipt.get('error_code'))
            if receipt.get('validation_issues'):
                issue = receipt['validation_issues'][0]
                session.error += f" ({issue['path']}: {issue['message']})"
        elif not answer.completion_checks:
            session.status = 'failed'
            session.error = 'Answer design omitted the required completion checks.'
        else:
            session.answer_schema = answer.model_dump(mode='json')
            if receipt['call_id'] == 'answer_design_correction':
                session.workflow.setdefault('model_call_resolutions', {})['answer_design'] = receipt['call_id']
            write_text(output_root / session.session_id / 'answer_schema.json',
                       answer.model_dump_json(indent=2) + '\n')
    save_session(session, output_root)
    return {'session': session}
