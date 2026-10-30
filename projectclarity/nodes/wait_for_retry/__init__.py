"""Wait briefly when the next eligible source attempt is delayed.

The node observes cancellation and elapsed-time limits while waiting for the
scheduler's retry time. It does not send a request itself. Importing this package
does not sleep or resume work.
"""
