"""Group checks for saving, inspecting and resolving a paused plan review.

Contract tests reject invalid responses; integration tests resume real LangGraph
checkpoints across processes using temporary local documents. Run
``python -m unittest discover -s tests/review -t . -v`` offline. These tests do
not establish a browser UI, multi-user authorization or claim-review capability.
Importing this package does not submit decisions or start a worker.
"""
