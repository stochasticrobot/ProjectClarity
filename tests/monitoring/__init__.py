"""Group offline tests for live progress, operational status, and worker control.

These checks distinguish monitoring records from a running worker and exercise
stop requests across real local processes. External model/source responses are
fixtures; no cloud credentials or live website access are needed.
"""
