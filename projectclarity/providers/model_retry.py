"""Account for each model request and retry only eligible temporary transport failures.

Planning, extraction and other structured tasks use this shared executor. Before
sending a request it reserves the allowance and saves the attempt; after a failure
it preserves private error details and applies the configured retry count and delay.
SDK-level retries are disabled so each actual invocation remains visible in these
records. For example, a temporary timeout can retry within the remaining allowance,
but a malformed answer is not repaired here. The caller validates and accepts task
results. Interrupted requests with unknown outcomes are not automatically repeated.
"""
import hashlib
import json
import random
import time

from projectclarity.providers.model_failures import ModelCallFailure, describe_model_failure
from projectclarity.providers.error_receipts import save_provider_error
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.run_monitor import MonitoringError
from projectclarity.session_storage import save_session, write_text
from projectclarity.coverage.checkpoints import model_request_ceiling

RETRY_BASE_SECONDS = 1.0
MAX_RETRY_WAIT_SECONDS = 60.0

from projectclarity.providers.concurrent_calls import ModelInvocation, RetryDelay, run_model_jobs


def remaining_model_time(session):
    """Read the remaining acquisition clock without replenishing it during model work.

    Args:
        session: Run with a persisted acquisition checkpoint, if acquisition has begun.

    Returns:
        Remaining seconds, or None before acquisition starts (including initial planning).
        Wall time since the checkpoint is charged, never treated as free retry time.
    """
    usage = session.retrieval_usage
    if not usage:
        return None
    elapsed = usage.get('elapsed_seconds', 0)
    if usage.get('checkpoint_wall_time') is not None:
        elapsed += max(0, time.time() - usage['checkpoint_wall_time'])
    return max(0, session.request.fetch_limits.max_run_seconds - elapsed)


def save_model_attempts(session, output_root):
    """Persist the attempt ledger and matching session reservations before more work.

    Args:
        session: Mutable run owning content-free model attempt records.
        output_root: Directory containing this run's already-created directory.

    Returns:
        None. Atomically replace each file separately; this is not a multi-file transaction.
    """
    save_session(session, output_root)
    write_text(output_root / session.session_id / 'model_attempts.json',
        json.dumps({'schema_version': 1, 'attempts': session.model_attempts}, indent=2) + '\n')


def model_attempt_steps(session, output_root, *, task, call_id, messages, model_factory,
                        monitor=None, secret=None):
    """Prepare and account for one model task, yielding each invocation or retry delay.

    This generator runs on the coordinator thread. Before yielding a prepared request,
    it reserves and saves the attempt; after receiving the response or exception it
    records usage or failure and decides whether a temporary error may retry. The
    concurrent-call runner performs the actual model invocation. A task may require
    several attempts, but every attempt consumes the same saved run allowance.

    Args:
        session: Run whose immutable request policy and shared usage constrain every attempt.
        output_root: Directory containing this run's journal and safe attempt ledger.
        task: Application task label, such as plan_research or review_claims.
        call_id: Owning node's logical call identifier; retries retain this association.
        messages: Unchanged application messages; only their hash enters this ledger.
        model_factory: Callable accepting a timeout in seconds and constructing a runnable
            with SDK retries disabled. Construction must not send a request.
        monitor: Optional cancellation and operational-event interface.
        secret: Optional credential, used only to exclude it from safe diagnostic fields.

    Returns:
        A coordinator-driven generator yielding network calls or retry delays, and returning
        the runnable response, still requiring parsing/evidence validation. Never advance
        this generator on worker threads; reservations and writes belong to its caller.
        Raise ModelCallFailure after a terminal failure, exhausted retry allowance, budget
        stop or long Retry-After deferral. Cancellation/interrupts propagate without retry.
        Every sent attempt consumes global and optional capture allowances; failed usage
        stays unknown. Successful response receipts retain provider-reported input,
        output and total tokens before downstream parsing; absent usage remains null.
        Saved started attempts are never automatically replayed.
        Scheduled non-coverage calls, including retries, honor the final coverage
        slot reservation; a retry cannot consume that slot during a concurrent wave.
    """
    from projectclarity.knowledge.capture_budget import capture_remaining

    previous = [a for a in session.model_attempts if a['task'] == task and a['call_id'] == call_id]
    if previous:
        raise ModelCallFailure('model_call_already_recorded', {}, previous[-1]['attempt_id'])
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
    last = None
    for retry in range(session.request.model_retries + 1):
        if monitor:
            monitor.check_cancel()
        code = ('request_limit' if session.usage.requests_attempted >= model_request_ceiling(session, task) else
            'knowledge_request_limit' if task == 'accumulate_knowledge' and capture_remaining(session) <= 0 else None)
        remaining = remaining_model_time(session)
        if remaining is not None and remaining <= 0:
            code = code or 'run_time_limit'
        if code:
            if last:
                last['recovery_stop'] = code
                save_model_attempts(session, output_root)
            raise ModelCallFailure(code, last.get('diagnostics', {}) if last else {},
                last['attempt_id'] if last else None)
        timeout = min(session.request.model_timeout_seconds, remaining) if remaining is not None else session.request.model_timeout_seconds
        model = model_factory(timeout)
        if monitor:
            monitor.check_cancel()
        attempt = {'attempt_id': f'M{len(session.model_attempts) + 1}', 'task': task,
            'call_id': call_id, 'attempt_number': retry + 1, 'status': 'started',
            'started_at': time.time(), 'timeout_seconds': timeout, 'input_sha256': digest,
            'diagnostics': None, 'retry_scheduled': False}
        session.model_attempts.append(attempt)
        session.usage.requests_attempted += 1
        save_model_attempts(session, output_root)
        started = time.monotonic()
        try:
            if monitor:
                monitor.observe('model_attempt_started', stage=task, batch_id=call_id,
                    llm_requests=session.usage.requests_attempted, attempt_id=attempt['attempt_id'],
                    attempt_number=retry + 1)
            response = yield ModelInvocation(model, messages)
        except (RunCancelled, KeyboardInterrupt, EOFError, MonitoringError, SystemExit):
            attempt['status'] = 'uncertain'
            session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
            raise
        except Exception as error:
            attempt.update(status='failed', diagnostics=describe_model_failure(error, secret))
            error_path = save_provider_error(
                output_root / session.session_id, attempt['attempt_id'], error, secret)
            if error_path:
                attempt['diagnostics']['provider_error_path'] = error_path
            session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
        else:
            attempt['status'] = 'response_received'
            raw = response.get('raw') if isinstance(response, dict) else None
            usage = getattr(raw, 'usage_metadata', None) or {}
            # One receipt per actual wire attempt. Keep missing usage unknown;
            # do not add cache subtotals to already-reported input tokens.
            attempt['token_usage'] = {key: usage.get(key) for key in
                ('input_tokens', 'output_tokens', 'total_tokens')}
            return response
        finally:
            attempt['elapsed_seconds'] = round(time.monotonic() - started, 3)
            save_model_attempts(session, output_root)
            if monitor:
                monitor.observe('model_attempt_finished', stage=task, batch_id=call_id,
                    attempt_id=attempt['attempt_id'], attempt_number=retry + 1,
                    call_status=attempt['status'], call_seconds=attempt['elapsed_seconds'],
                    error_code=(attempt['diagnostics'] or {}).get('category'))
        last = attempt
        diagnostics = attempt['diagnostics']
        if not diagnostics['retryable'] or retry == session.request.model_retries:
            attempt['recovery_stop'] = 'non_retryable' if not diagnostics['retryable'] else 'retry_limit'
            save_model_attempts(session, output_root)
            raise ModelCallFailure('provider_or_adapter_error', diagnostics, attempt['attempt_id'])
        delay = max(RETRY_BASE_SECONDS * 2 ** retry + random.uniform(0, RETRY_BASE_SECONDS / 4),
            diagnostics.get('retry_after_seconds') or 0)
        remaining = remaining_model_time(session)
        stop = ('request_limit' if session.usage.requests_attempted >= model_request_ceiling(session, task) else
            'knowledge_request_limit' if task == 'accumulate_knowledge' and capture_remaining(session) <= 0 else
            'run_time_limit' if remaining is not None and delay >= remaining else
            'model_retry_deferred' if delay > MAX_RETRY_WAIT_SECONDS else None)
        attempt.update(retry_at=time.time() + delay, retry_wait_seconds=delay,
            retry_scheduled=stop is None)
        if stop:
            attempt['recovery_stop'] = stop
            save_model_attempts(session, output_root)
            raise ModelCallFailure(stop, diagnostics, attempt['attempt_id'])
        save_model_attempts(session, output_root)
        if monitor:
            monitor.observe('model_retry_scheduled', stage=task, batch_id=call_id,
                attempt_id=attempt['attempt_id'], attempt_number=retry + 2,
                wait_seconds=delay, error_code=diagnostics['category'])
        try:
            yield RetryDelay(delay)
        except (RunCancelled, KeyboardInterrupt, EOFError, SystemExit):
            attempt.update(retry_scheduled=False, recovery_stop='interrupted_before_retry')
            save_model_attempts(session, output_root)
            raise


def invoke_with_retries(session, output_root, **kwargs):
    """Run the shared attempt state machine synchronously for dependent graph nodes.

    Args:
        session: Run owning the global budget and durable attempt ledger.
        output_root: Root containing this run's saved files.
        **kwargs: Task, call ID, messages, model factory, monitor and secret passed
            unchanged to model_attempt_steps.
    Returns:
        The provider response, or the original terminal exception. One coordinator
        performs all reservations and writes; only invocation may run on a worker.
    """
    return run_model_jobs([model_attempt_steps(session, output_root, **kwargs)],
        concurrency=1, monitor=kwargs.get('monitor'))[0]
