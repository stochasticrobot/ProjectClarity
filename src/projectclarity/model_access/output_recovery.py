"""Retry a rejected model output once without changing its evidence or validation.

This is an opt-in stage policy, not a transport retry. Both attempts use the same
complete request and remain separate ledger calls. Transport/authentication errors,
confirmed truncation and cancellation are not retried here. A second malformed
answer remains a failure; no text, identifiers or citations are guessed or repaired.
"""
from projectclarity.model_access.model_outcomes import ModelOutputStructureError
from projectclarity.model_access.model_ledger import mark_validation


def validated_output(caller, stage, request, session_id, validate, before_call, record):
    """Make at most two generation attempts when output fails structural validation.

    Args:
        caller: Production model caller; owns credentials, budgets and raw receipts.
        stage: Base stage label; retry receives a distinct output-retry suffix.
        request: Complete unchanged request, including all evidence and output rules.
        session_id: Owning session identifier for ledger and budget accounting.
        validate: Function accepting completed text and returning a validated result;
            ValueError means invalid output, not a provider/transport failure.
        before_call: Zero-argument callback checking cancellation and counting an
            attempt before each dispatch. Exceptions prevent that dispatch.
        record: Callback receiving each attempt's text/status/error for durable
            stage-level storage. Write failures propagate and are never retried.
    Returns:
        First validated result. One output rejection permits one further attempt;
        another rejection propagates. Other exceptions stop immediately. Each
        attempt is recorded before retry, and its own ledger validation is marked.
        A successful retry does not relabel the original rejected response.
    """
    for index in range(2):
        label = stage if index == 0 else stage + '-output-retry'
        before_call()
        attempt = {'stage': label, 'attempt': index + 1}
        try:
            text = caller(label, request, session_id)
        except ModelOutputStructureError as error:
            attempt.update(status='rejected_output', error={'type':type(error).__name__, 'message':str(error)[:500]})
            record(attempt)
            if index:
                raise
            continue
        except Exception as error:
            attempt.update(status='request_failed', error={'type':type(error).__name__, 'message':str(error)[:500]})
            record(attempt)
            raise
        attempt['raw_text'] = text
        try:
            result = validate(text)
        except ValueError as error:
            mark_validation(caller, label, session_id, False)
            attempt.update(status='rejected_output', error={'type':type(error).__name__, 'message':str(error)[:500]})
            record(attempt)
            if index:
                raise
        except Exception as error:
            mark_validation(caller, label, session_id, False)
            attempt.update(status='validation_failed', error={'type':type(error).__name__, 'message':str(error)[:500]})
            record(attempt)
            raise
        else:
            mark_validation(caller, label, session_id, True)
            attempt['status'] = 'checks_pass'
            record(attempt)
            return result
