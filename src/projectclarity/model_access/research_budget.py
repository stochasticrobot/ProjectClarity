"""Pause research before further AI work would exceed its shared allowance.

Every stage and round draws from one call and token budget. Before sending a
request, the client reserves its counted input plus maximum output, so concurrent
workers cannot each spend the same remaining allowance. A pause keeps those
workers waiting until the user grants more room or quits. Private accounting is
saved separately from stage snapshots so progress updates cannot erase it.

Token counts are usage units, not prices. Provider counts are estimates before
execution; actual usage replaces reservations afterward. Missing usage keeps the
reservation as an explicit estimate and requires acknowledgement before more work.
"""
import json
from copy import deepcopy
from threading import Condition

from projectclarity.model_access.model_ledger import TOKEN_FIELDS, read_ledger

DEFAULT_MODEL_REQUEST_BUDGET = 750
DEFAULT_MODEL_TOKEN_BUDGET = 8_000_000


class BudgetStopped(RuntimeError):
    """The user or server shutdown cancelled model work waiting on an allowance."""


class ResearchBudgets:
    """Own durable, process-local admission gates for one server's research runs."""

    def __init__(self, root):
        """Prepare accounting without reading credentials or starting requests.

        Args: root: Directory containing private session folders.
        Returns: None; session records are loaded lazily under one condition lock.
        """
        self.root = root
        self.condition = Condition()
        self.states = {}
        self.stops = {}
        self.closed = False

    def _load(self, sid):
        """Load a run's allowance and reconcile interrupted reservations once.

        Args: sid: Validated session folder name; caller holds the condition lock.
        Returns: Mutable cached accounting, or None for standalone calls without a
            session file. Existing ledger usage is included, never reset on retry.
            Interrupted requests with unknown usage retain estimated charges.
        """
        if sid in self.states:
            return self.states[sid]
        folder = self.root / sid
        if not (folder / 'session.json').exists():
            return None
        settings = json.loads((folder / 'session.json').read_text())
        path = folder / 'model-budget.json'
        ledger = read_ledger(folder)
        if path.exists():
            state = json.loads(path.read_text())
            rows = {r['call_id']: r for r in ledger['calls']}
            for ticket, allowance in list(state['reservations'].items()):
                row = rows.get(ticket, {})
                self._finish(state, ticket, bool(row.get('attempted')), row.get('usage'))
            state['paused'] = False
        else:
            totals = ledger['totals']
            calls = settings.get('model_request_budget', DEFAULT_MODEL_REQUEST_BUDGET)
            tokens = settings.get('model_token_budget', DEFAULT_MODEL_TOKEN_BUDGET)
            state = dict(call_limit=calls, token_limit=tokens, call_increment=calls,
                         token_increment=tokens, calls_used=totals['attempted'],
                         known_tokens=totals['known_tokens'], estimated_tokens=0,
                         unknown_usage=totals['unknown_usage'],
                         unacknowledged_unknown=totals['unknown_usage'], reservations={},
                         paused=False, cancelled=False, revision=0, reason='', required_tokens=0)
        self.states[sid] = state
        self._save(sid, state)
        return state

    def _save(self, sid, state):
        """Replace a private accounting snapshot atomically while holding the lock.

        Args: sid: Session folder name; state: Budget counters and reservations.
        Returns: None; filesystem errors propagate before any new request is sent.
        """
        path = self.root / sid / 'model-budget.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2))
        temporary.chmod(0o600)
        temporary.replace(path)

    def begin(self, sid, stop):
        """Bind an explicitly started workflow to its cancellation event.

        Args: sid: Session UUID; stop: Event shared with stage workers.
        Returns: None; clears prior cancellation but never resets used allowances.
        """
        with self.condition:
            self.stops[sid] = stop
            state = self._load(sid)
            if state is not None:
                state['cancelled'] = stop.is_set()
                self._save(sid, state)

    def _check_stop(self, sid, state):
        """Prevent dispatch after quit or shutdown, including already-waiting calls.

        Args: sid: Session key; state: Current budget accounting or None.
        Returns: None when allowed; raises BudgetStopped after cancellation.
        """
        if self.closed or (state and state['cancelled']) or (
                sid in self.stops and self.stops[sid].is_set()):
            raise BudgetStopped('Research stopped; completed work has been saved.')

    def enabled(self, sid):
        """Identify session-backed calls without imposing UI budgets on CLI tools.

        Args: sid: Session folder name.
        Returns: Whether a saved session owns this request; no provider calls.
        """
        return (self.root / sid / 'session.json').exists()

    def wait_until_running(self, sid):
        """Avoid even a token-count request while a known pause or stop is active.

        Args: sid: Owning session UUID.
        Returns: None after any existing pause is resolved; BudgetStopped on quit.
            No request or token allowance is consumed by this preliminary check.
        """
        with self.condition:
            state = self._load(sid)
            while True:
                self._check_stop(sid, state)
                if not state or not state['paused']:
                    return
                self.condition.wait(timeout=0.25)

    def reserve(self, sid, ticket, tokens):
        """Wait for room, then reserve one generation request and its token ceiling.

        Args: sid: Session UUID; ticket: Unique ledger call ID; tokens: Provider-
            counted input plus maximum output, including reasoning allowance.
        Returns: None after durable admission. Blocks during a budget pause; Quit
            raises BudgetStopped. In-flight calls drain before requesting approval
            so unused reservations do not cause unnecessary permission dialogs.
        """
        with self.condition:
            state = self._load(sid)
            while state is not None:
                self._check_stop(sid, state)
                reserved = sum(state['reservations'].values())
                charged = state['known_tokens'] + state['estimated_tokens']
                calls_fit = state['calls_used'] + len(state['reservations']) < state['call_limit']
                tokens_fit = charged + reserved + tokens <= state['token_limit']
                if not state['paused'] and not state['unacknowledged_unknown'] and calls_fit and tokens_fit:
                    state['reservations'][ticket] = tokens
                    self._save(sid, state)
                    return
                if not state['reservations'] and not state['paused']:
                    reasons = []
                    if not calls_fit:
                        reasons.append('Model request budget reached.')
                    if not tokens_fit:
                        reasons.append('Token budget cannot cover the next request safely.')
                    if state['unacknowledged_unknown']:
                        reasons.append('Some attempted requests have unconfirmed token usage.')
                    state.update(paused=True, reason=' '.join(reasons),
                                 required_tokens=tokens, revision=state['revision'] + 1)
                    self._save(sid, state)
                self.condition.wait(timeout=0.25)

    def _finish(self, state, ticket, attempted, usage):
        """Replace a reservation with known usage or an explicitly uncertain charge.

        Args: state: Locked mutable accounting; ticket: Reserved call ID;
            attempted: Whether dispatch was attempted; usage: Provider counters or None.
        Returns: None; unsent requests refund everything. Missing usage retains the
            full estimate and blocks subsequent admission until acknowledged.
        """
        reserved = state['reservations'].pop(ticket, None)
        if reserved is None or not attempted:
            return
        state['calls_used'] += 1
        if isinstance(usage, dict) and all(isinstance(usage.get(k), int) for k in ('input_tokens', 'output_tokens')):
            state['known_tokens'] += sum(usage.get(k) or 0 for k in TOKEN_FIELDS)
        else:
            state['estimated_tokens'] += reserved
            state['unknown_usage'] += 1
            state['unacknowledged_unknown'] += 1

    def finish(self, sid, ticket, attempted, usage):
        """Settle a completed transport attempt and wake workers waiting for room.

        Args: sid: Session UUID; ticket: Reserved call ID; attempted: Dispatch flag;
            usage: Actual provider counters, including failed/incomplete replies.
        Returns: None; saves accounting before waking peers. Exceptions propagate.
        """
        with self.condition:
            state = self.states.get(sid)
            if state is not None:
                self._finish(state, ticket, attempted, usage)
                self._save(sid, state)
                self.condition.notify_all()

    def snapshot(self, sid):
        """Expose safe accounting without creating files or admitting new work.

        Args: sid: Existing session UUID.
        Returns: Copy of loaded or saved state, or None before first work; includes
            proposed Continue limits. Prompts, credentials and receipts are omitted.
        """
        with self.condition:
            state = self.states.get(sid)
            if state is None:
                path = self.root / sid / 'model-budget.json'
                if not path.exists():
                    return None
                state = json.loads(path.read_text())
            result = deepcopy(state)
            result['reserved_calls'] = len(result.pop('reservations'))
            result['reserved_tokens'] = sum(state['reservations'].values())
            result['next_call_limit'] = state['call_limit'] + state['call_increment']
            result['next_token_limit'] = max(state['token_limit'] + state['token_increment'],
                state['known_tokens'] + state['estimated_tokens'] + state['required_tokens'])
            return result

    def decide(self, sid, revision, action):
        """Apply one explicit user decision to exactly the displayed budget pause.

        Args: sid: Active session UUID; revision: Pause identifier shown by the UI;
            action: 'continue' to add an allowance, or 'quit' to cancel waiting work.
        Returns: Whether applied. Stale/repeated decisions do nothing. Continue
            acknowledges uncertain usage without relabelling it as known or free.
        """
        with self.condition:
            state = self._load(sid)
            if not state or not state['paused'] or state['revision'] != revision:
                return False
            if action == 'continue':
                state['call_limit'] += state['call_increment']
                state['token_limit'] = max(state['token_limit'] + state['token_increment'],
                    state['known_tokens'] + state['estimated_tokens'] + state['required_tokens'])
                state['unacknowledged_unknown'] = 0
            else:
                state['cancelled'] = True
                if sid in self.stops:
                    self.stops[sid].set()
            state['paused'] = False
            self._save(sid, state)
            self.condition.notify_all()
            return True

    def cancel(self, sid):
        """Wake paused workers when the ordinary Stop control is used.

        Args: sid: Session UUID whose stop event is already being set.
        Returns: None; preserves accounting and prevents any further dispatch.
        """
        with self.condition:
            state = self.states.get(sid)
            if state is not None:
                state.update(cancelled=True, paused=False)
                self._save(sid, state)
            self.condition.notify_all()

    def close(self):
        """Release paused workers during server shutdown without granting allowance.

        Args: None.
        Returns: None; in-flight requests finish normally, queued calls are cancelled.
        """
        with self.condition:
            self.closed = True
            for stop in self.stops.values():
                stop.set()
            self.condition.notify_all()
