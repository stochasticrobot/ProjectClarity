"""Assess separate report-evidence batches at the same time.

Each worker retains the existing token checks, splitting and omitted-fact recovery.
Only the coordinator writes task receipts and session progress. Findings are returned
in original batch order. Item reports retain successful fragments even when other
batches fail; legacy narrative reports retain their stricter all-batches requirement.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import copy, deepcopy
from queue import Queue, Empty
from threading import Event

COUNTERS = ('model_calls', 'batches_total', 'batches_finished',
            'facts_accounted', 'reused_tasks', 'merge_tasks_finished')


def assess_batch(builder, batch, inbox):
    """Run an existing dispatcher with private counters and acknowledged saves.

    Args:
        builder: Parent builder; immutable evidence/cache and transport are shared.
        batch: Disjoint whole facts assigned to this worker.
        inbox: Coordinator queue for progress deltas and durable checkpoint requests.
    Returns:
        Ordered fragments, split count and optional error. Item-mode failures retain
        validated parent fragments; other failures propagate after raw responses
        are saved. Recursive splitting/recovery stays on this worker's single slot.
    """
    worker = copy(builder)
    owner = copy(builder.owner)
    worker.owner = owner
    worker.splits = 0
    worker.retained_fragments = []
    owner.progress = deepcopy(builder.owner.progress)
    owner.state = {**builder.owner.state, 'report': owner.progress}
    previous = {key: owner.progress[key] for key in COUNTERS}

    def publish(state):
        """Send private counter changes without writing shared session state.

        Args: state: Private snapshot supplied by the existing dispatcher.
        Returns: None; queues additive deltas and the largest observed token count.
        """
        progress = state['report']
        delta = {key: progress[key] - previous[key] for key in COUNTERS}
        previous.update({key: progress[key] for key in COUNTERS})
        inbox.put(('progress', delta, progress['largest_counted_input']))

    def checkpoint(receipt):
        """Wait until the coordinator durably saves this exact task response.

        Args: receipt: Worker-owned task state, copied before queueing.
        Returns: None; raises a coordinator write error before work continues.
        """
        ready, errors = Event(), []
        inbox.put(('checkpoint', deepcopy(receipt), ready, errors))
        ready.wait()
        if errors:
            raise errors[0]

    owner.publish = publish
    worker.checkpoint = checkpoint
    try:
        return worker.dispatch('facts', batch), worker.splits, None
    except Exception as error:
        if not getattr(builder, 'allow_partial', False):
            raise
        # Even a failed omitted-fact recovery can have a useful parent fragment.
        # Return only independently validated fragments, never raw failed output.
        return worker.retained_fragments, worker.splits, str(error)


def select_report_batches(builder, batches):
    """Bound independent report assessments and serialize all persistent updates.

    Args:
        builder: Owning builder with initialized report counters and saved settings.
        batches: Deterministically ordered, disjoint whole-fact groups.
    Returns:
        Flattened findings in input order. Stop drains started work before raising;
        item-mode failures retain sibling successes for explicitly partial publication;
        legacy reports still fail. Disk/publication errors always stop publication.
        No new batch is scheduled after stop, and no merge runs in this pool.
    """
    owner = builder.owner
    limit = owner.state.get('report_selection_concurrency', 6)
    if type(limit) is not int or not 1 <= limit <= 16:
        raise ValueError('Report selection concurrency must be an integer from 1 to 16.')
    inbox, active, outputs, failures = Queue(), {}, {}, []
    next_batch = 0
    publication_errors = []

    def drain():
        """Commit queued receipts and progress on the coordinator thread.

        Args: None; consumes this attempt's worker messages.
        Returns: None; checkpoint failures are delivered to their waiting worker.
        """
        changed = False
        while True:
            try:
                message = inbox.get_nowait()
            except Empty:
                break
            if message[0] == 'checkpoint':
                _, receipt, ready, errors = message
                try:
                    builder.checkpoint(receipt)
                except Exception as error:
                    errors.append(error)
                    publication_errors.append(error)
                    owner.stop.set()
                finally:
                    ready.set()
            else:
                _, delta, largest = message
                for key, value in delta.items():
                    owner.progress[key] += value
                owner.progress['largest_counted_input'] = max(owner.progress['largest_counted_input'], largest)
                changed = True
        if changed and not publication_errors:
            try:
                owner.publish(owner.state)
            except Exception as error:
                # Keep draining acknowledged checkpoints: leaving here would strand
                # workers waiting for saves while executor shutdown waits for them.
                publication_errors.append(error)
                owner.stop.set()

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='clarity-report-selection') as pool:
        while active or (next_batch < len(batches) and not owner.stop.is_set()):
            while len(active) < limit and next_batch < len(batches) and not owner.stop.is_set():
                index = next_batch
                next_batch += 1
                active[pool.submit(assess_batch, builder, batches[index], inbox)] = index
            done, _ = wait(active, timeout=.05, return_when=FIRST_COMPLETED)
            drain()
            for future in done:
                index = active.pop(future)
                try:
                    outputs[index], splits, issue = future.result()
                    if issue:
                        failures.append(issue)
                    builder.splits += splits
                except Exception as error:
                    failures.append(str(error))
        drain()
    if publication_errors:
        raise publication_errors[0]
    if owner.stop.is_set():
        raise InterruptedError('Stopped before report combination; completed work retained.')
    owner.progress['failed_batches'] = len(failures)
    if failures and not getattr(builder, 'allow_partial', False):
        raise ValueError(f'{len(failures)} evidence batch(es) failed. Validated batches are saved for retry. First issue: {failures[0]}')
    return [output for index in sorted(outputs) for output in outputs[index]]
