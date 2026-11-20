"""Pause a search for its owner's CAPTCHA answer without exposing provider secrets.

Only image bytes, instructions and a temporary challenge identifier reach the UI.
The isolated worker keeps cookies and form tokens. Challenges exist only in memory;
restarting the application cannot replay an answer or revive an old client. After
30 seconds without human activity, the server skips to the next provider even if
no browser is connected.
"""
import base64
import math
from time import monotonic
from threading import Event, Lock
from uuid import uuid4
from fastapi import HTTPException


class CaptchaHandoff:
    """Own session-scoped, one-use human decisions for live search workers."""

    def __init__(self):
        """Create an empty handoff registry without any provider requests.

        Args: None.
        Returns: None; initializes thread-safe state and a shutdown signal.
        """
        self.lock = Lock()
        self.pending = {}
        self.closed = Event()

    def wait(self, sid, challenge, cancelled):
        """Publish a challenge and wait cooperatively for the human's decision.

        Args:
            sid: Owning research session UUID string.
            challenge: Worker-supplied instruction and bounded base64 raster images.
            cancelled: Stop callback checked while waiting; raises InterruptedError.
        Returns:
            Submit/skip/cancel decision; 30 seconds without human activity produces
            skip even with no browser connected. Clears images on completion or
            cancellation. Shutdown cancels rather than approving or skipping work.
        """
        images = challenge.get('images', [])
        if not 1 <= len(images) <= 16:
            raise ValueError('Unsupported CAPTCHA image count.')
        tiles = []
        for image in images:
            if image['mime'] not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ValueError('Unsupported CAPTCHA image type.')
            data = base64.b64decode(image['data'], validate=True)
            if not 0 < len(data) <= 1000000:
                raise ValueError('Invalid CAPTCHA image size.')
            tiles.append((data, image['mime']))
        entry = {'id': str(uuid4()), 'instruction': str(challenge.get('instruction', 'Complete the image challenge.'))[:1000],
                 'tiles': tiles, 'event': Event(), 'answer': None, 'deadline': monotonic() + 30}
        with self.lock:
            if sid in self.pending:
                raise RuntimeError('A search challenge is already pending.')
            self.pending[sid] = entry
        try:
            while not entry['event'].wait(.2):
                cancelled()
                if self.closed.is_set():
                    raise InterruptedError('Application stopped during human verification.')
                with self.lock:
                    self.expire(entry)
            cancelled()
            return entry['answer']
        finally:
            with self.lock:
                if self.pending.get(sid) is entry:
                    self.pending.pop(sid)

    def snapshot(self, sid):
        """Describe a pending challenge without returning tokens, cookies or images.

        Args: sid: Owning session UUID string.
        Returns: Public metadata or None after a decision or when no challenge exists.
        """
        with self.lock:
            entry = self.pending.get(sid)
            if entry:
                self.expire(entry)
            if not entry or entry['answer'] is not None:
                return None
            return {'id': entry['id'], 'provider': 'duckduckgo',
                    'instruction': entry['instruction'], 'tile_count': len(entry['tiles']),
                    'idle_seconds_remaining': max(0, math.ceil(entry['deadline'] - monotonic()))}

    def expire(self, entry):
        """Skip an idle provider once, under the caller-held registry lock.

        Args: entry: Pending challenge with a monotonic inactivity deadline.
        Returns: None; at expiry sets a skip decision and wakes its worker. Reading
            status never extends the deadline; an existing answer is never replaced.
        """
        if entry['answer'] is None and monotonic() >= entry['deadline']:
            entry['answer'] = {'action': 'skip', 'tiles': []}
            entry['event'].set()

    def image(self, sid, challenge_id, index):
        """Serve one cached raster tile only for its current session/challenge.

        Args: sid: Session UUID; challenge_id: One-use displayed ID; index: Tile number.
        Returns: Image bytes and MIME type, or HTTP404 for stale/wrong/out-of-range IDs.
        """
        with self.lock:
            entry = self.pending.get(sid)
            if not entry or entry['id'] != challenge_id or entry['answer'] is not None or not 0 <= index < len(entry['tiles']):
                raise HTTPException(404, 'Challenge image is no longer available.')
            return entry['tiles'][index]

    def decide(self, sid, challenge_id, action, tiles):
        """Accept one explicit decision, rejecting stale or duplicate submissions.

        Args: sid: Session UUID; challenge_id: Displayed ID; action: submit/skip/cancel/activity;
            tiles: Human-selected zero-based indices, possibly empty for submit.
        Returns: None; activity renews the 30-second deadline without answering;
            other actions wake the worker. Invalid indices/actions raise422;
            missing or already-answered challenges raise409 without changing work.
        """
        with self.lock:
            entry = self.pending.get(sid)
            if entry:
                self.expire(entry)
            if not entry or entry['id'] != challenge_id or entry['answer'] is not None:
                raise HTTPException(409, 'This challenge is no longer waiting for an answer.')
            if action not in ('submit', 'skip', 'cancel', 'activity') or len(tiles) > 16 or any(type(i) is not int or not 0 <= i < len(entry['tiles']) for i in tiles):
                raise HTTPException(422, 'Invalid challenge decision.')
            if action == 'activity':
                entry['deadline'] = monotonic() + 30
                return
            entry['answer'] = {'action': action, 'tiles': sorted(set(tiles)) if action == 'submit' else []}
            entry['event'].set()

    def close(self):
        """Cancel pending waits when the application shuts down.

        Args: None.
        Returns: None; workers observe shutdown within their short polling interval.
        """
        self.closed.set()
