"""Check saved research state and what happens when a disk operation fails.

For example, a failed replacement must not destroy the previous session.json.
These tests use temporary files and inject failures only at the write/replace
boundary. They help distinguish a storage problem from a planner problem when
the application moves into containers. They do not simulate a power loss or
prove database-style transactions across several output files.
"""

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projectclarity.research_schemas import ResearchSession
from projectclarity.session_storage import save_session, write_text
from tests.research.research_test_fixtures import example_session


class SessionStorageTests(unittest.TestCase):
    """Inspect real files; no developer sessions or credentials are opened."""

    def setUp(self):
        """Give each storage test a disposable directory so failed writes cannot damage real runs."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_saved_session_round_trips_without_losing_category_or_unicode(self):
        """The saved snapshot can be decoded with its original data and version."""
        session = example_session()
        session.request.question = "Qui étudie la sécurité de l’IA?"
        (self.root / session.session_id).mkdir()
        save_session(session, self.root)
        restored = ResearchSession.model_validate_json(
            (self.root / session.session_id / "session.json").read_text(encoding="utf-8")
        )
        self.assertEqual(restored, session)

    def test_successful_write_replaces_previous_snapshot_completely(self):
        """Shorter new content must not retain the tail of an older snapshot."""
        target = self.root / "session.json"
        target.write_text("previous content much longer than the replacement")
        write_text(target, "new ✓\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new ✓\n")
        self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_failed_replacement_preserves_previous_snapshot_and_raises(self):
        """A permissions failure is visible to the caller without corrupting old data."""
        target = self.root / "session.json"
        target.write_bytes(b'{"previous": true}\n')
        with patch.object(Path, "replace", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(PermissionError):
                write_text(target, '{"new": true}\n')
        self.assertEqual(target.read_bytes(), b'{"previous": true}\n')

    def test_partial_temporary_write_preserves_previous_snapshot(self):
        """Running out of disk during a write cannot publish half a JSON document."""
        target = self.root / "session.json"
        target.write_bytes(b'{"previous": true}\n')

        def interrupted_write(path, text, *, encoding):
            # A real partial file makes this stronger than raising before I/O.
            """Write a partial temporary file, then simulate disk exhaustion before final replacement."""
            path.write_bytes(b'{"unfinished":')
            raise OSError(errno.ENOSPC, "synthetic disk full")

        with patch.object(Path, "write_text", interrupted_write):
            with self.assertRaises(OSError) as caught:
                write_text(target, '{"new": true}\n')
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(target.read_bytes(), b'{"previous": true}\n')
