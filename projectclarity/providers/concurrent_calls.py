"""Run several model requests concurrently without concurrent writes to research state.

Passage extraction creates task generators: each can request a model invocation,
request a retry delay, then process the returned response. run_model_jobs advances
those generators on the calling thread, called the coordinator here. Worker threads
only invoke the prepared model with its messages; they never receive the mutable
session, monitor or output directory.

This lets network waits overlap while budget reservations, result updates and file
writes stay ordered on one thread. Cancellation stops new dispatch and interrupts
the task generators, but an already sent request can still complete and incur usage.
The implementation uses Python's standard thread-pool executor.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from contextvars import copy_context
import time

from projectclarity.retrieval.retrieval_limits import RunCancelled


class ConcurrentWorkInterrupted(RunCancelled):
    """Mark a peer's unknown outcome during runner cleanup, not a user cancellation.

    The original exception remains authoritative for the caller. Cleanup must not
    turn a process interruption into a cancelled run that cannot be resumed.
    """


@dataclass
class ModelInvocation:
    """A prepared model request whose allowance was already reserved by its task.

    Worker threads receive only the model adapter and messages, not the mutable
    research session; the coordinator processes the returned response.
    """
    model: object
    messages: list


@dataclass
class RetryDelay:
    """A requested retry delay in seconds, allowing other model tasks to continue.

    The coordinator records when this task becomes ready again and keeps checking
    cancellation while it waits; no worker thread sleeps for this delay.
    """
    seconds: float


def run_model_jobs(jobs, *, concurrency, monitor=None):
    """Execute several model-task generators while updating all research state on the calling thread.

    Each task yields a prepared model invocation or retry delay. Worker threads perform
    only the invocation; returned responses and errors are sent back to the generator
    on the coordinator thread. This overlaps network waits without competing session
    or output-file writers.

    Args:
        jobs: Iterable of generators yielding ModelInvocation or RetryDelay. All
            advancement, including exception handling, runs on the calling thread.
        concurrency: Maximum simultaneous logical tasks and wire requests, at least one.
        monitor: Optional cancellation checker, never sent to worker threads.
    Returns:
        A list of completed task-generator return values in completion order. A True result stops dispatch
        of new jobs but drains already-started peers (used for provider failures).
        Fatal errors/cancellation interrupt all active generators and propagate;
        sent requests may still incur usage and cannot be forcibly recalled.
    """
    if concurrency < 1:
        raise ValueError('Concurrency must be positive')
    pending = iter(jobs)
    live, delayed, futures, results = set(), {}, {}, []
    exhausted = False
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='research-model')

    def advance(job, value=None, error=None):
        """Advance one task on the coordinator, preserving its original exception.

        Args:
            job: Active task generator with coordinator-owned state.
            value: Successful wire response, or None for initial/delay advancement.
            error: Optional wire exception thrown into the task's retry machinery.
        Returns:
            None. Enqueue a reserved invocation/delay or retain the completed result;
            unexpected yields and unhandled task errors propagate to the caller.
        """
        nonlocal exhausted
        try:
            step = job.throw(error) if error is not None else job.send(value)
        except StopIteration as done:
            live.remove(job)
            results.append(done.value)
            if done.value is True:
                exhausted = True
            return
        if isinstance(step, ModelInvocation):
            futures[executor.submit(copy_context().run, step.model.invoke, step.messages)] = job
        elif isinstance(step, RetryDelay):
            delayed[job] = time.monotonic() + step.seconds
        else:
            raise TypeError('Unknown model task step')

    try:
        while live or not exhausted:
            if monitor:
                monitor.check_cancel()
            while not exhausted and len(live) < concurrency:
                try:
                    job = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                live.add(job)
                advance(job)
            for job, ready_at in list(delayed.items()):
                if ready_at <= time.monotonic():
                    del delayed[job]
                    advance(job)
            if futures:
                done, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    job = futures.pop(future)
                    try:
                        response = future.result()
                    except BaseException as error:
                        advance(job, error=error)
                    else:
                        advance(job, value=response)
            elif delayed:
                time.sleep(min(0.05, max(0, min(delayed.values()) - time.monotonic())))
        return results
    finally:
        # Interrupt suspended state machines before releasing worker references.
        # A worker still running owns no journal/monitor and cannot overwrite state.
        for job in list(live):
            try:
                job.throw(ConcurrentWorkInterrupted('Concurrent model work interrupted'))
            except BaseException:
                pass  # Preserve the first failure while flushing peer finally blocks.
            finally:
                job.close()
        executor.shutdown(wait=False, cancel_futures=True)
