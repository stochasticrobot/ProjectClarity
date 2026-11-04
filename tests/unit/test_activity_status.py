"""Check the status labels without starting a browser, server or research run.

The shared browser code is evaluated by Node using saved-state examples. These
checks distinguish active work from failures, partial results and historical views.
Browser layout, animation and request failure handling are checked separately.
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from projectclarity import __file__ as package_file


@unittest.skipUnless(shutil.which("node"), "Node is required for browser-code unit checks")
class ActivityStatusTests(unittest.TestCase):
    """Exercise the same status interpretation shipped to every web screen."""

    def presentations(self, cases):
        """Run the real JavaScript against several saved progress snapshots.

        Args:
            cases: Lists of session snapshot, screen path and optional review stage.
        Returns:
            Label, tone and busy records. A JavaScript error fails the test;
            no browser, network request or persistent file change is made.
        """
        script = Path(package_file).parent / "ux/scripts/workflow_navigation.js"
        program = """const fs=require('fs'),vm=require('vm');
const sandbox={};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(args=>sandbox.activityPresentation(...args))));"""
        result = subprocess.run(
            ["node", "-e", program, str(script)], input=json.dumps(cases),
            capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_every_active_stage_and_waiting_state(self):
        """Show activity only when the relevant step reports ongoing work.

        Args: None.
        Returns: None; verifies green active labels and non-animated clarification.
        """
        cases = [
            [{"status": "working"}, "/"],
            [{"status": "search_working"}, "/"],
            *[[{"run": {"status": "running", "stage": stage}}, "/run/id"]
              for stage in ("searching", "downloading", "extracting")],
            *[[{key: {"status": "running"}}, "/" + route + "/id"]
              for key, route in (("selection", "evidence"), ("triage", "triage"),
                                 ("investigation", "investigation"), ("link_ranking", "links"), ("report", "report"))],
        ]
        for result in self.presentations(cases):
            self.assertEqual(result["tone"], "working")
            self.assertTrue(result["busy"])
        waiting = self.presentations([[{"status": "clarification"}, "/"]])[0]
        self.assertEqual(waiting["tone"], "idle")
        self.assertFalse(waiting["busy"])

    def test_warning_failure_and_connection_independent_outcomes(self):
        """Keep partial evidence separate from interruptions and failed whole stages.

        Args: None.
        Returns: None; verifies partial/stopped states and healthy progress despite batch failures.
        """
        cases = [[{"selection": {"status": status}}, "/evidence/id"]
                 for status in ("partial", "failed", "cancelled", "interrupted", "completed")]
        results = self.presentations(cases)
        self.assertEqual([r["tone"] for r in results],
                         ["success", "error", "warning", "warning", "success"])
        self.assertTrue(all(not r["busy"] for r in results))
        warning = self.presentations([[{
            "run": {"status": "running", "stage": "searching",
                    "facts_unresolved": 20, "failed": 2,
                    "sources": [{"error": "Unreadable", "warnings": ["Missing text"]}],
                    "searches": [{"status": "completed", "primary_failure": {"error": "Blocked"}}]}
        }, "/run/id"]])[0]
        self.assertEqual(warning["tone"], "working")
        self.assertTrue(warning["busy"])

    def test_other_stage_work_and_rejected_claims_do_not_imply_activity_or_failure(self):
        """Avoid alarming users about ordinary research outcomes or later work.

        Args: None.
        Returns: None; a finished earlier step stays still; rejected claims alone
            do not become errors, while individual review failures also leave ongoing work green.
        """
        results = self.presentations([
            [{"status": "report_running", "run": {"status": "completed"},
              "report": {"status": "running"}}, "/run/id"],
            [{"investigation": {"status": "running", "rejected": 3}}, "/investigation/id"],
            [{"investigation": {"status": "running", "failed": 1}}, "/investigation/id"],
            [{"run": {"status": "running"}}, "/stage/id", "searches"],
        ])
        self.assertFalse(results[0]["busy"])
        self.assertEqual(results[1]["tone"], "working")
        self.assertEqual(results[2]["tone"], "working")
        self.assertFalse(results[3]["busy"])
        self.assertTrue(results[3]["label"].startswith("Saved "))

    def test_global_budget_pause_overrides_every_live_view_but_not_restart(self):
        """Make the stopped spending state visible regardless of the viewed step.

        Args: None.
        Returns: None; all live pages show a stationary warning; interrupted
            sessions do not pretend that a vanished worker can be continued.
        """
        state = {"status": "report_running", "model_budget": {"paused": True},
                 "report": {"status": "running"}}
        paths = ["/", "/run/id", "/evidence/id", "/triage/id", "/investigation/id", "/links/id", "/report/id"]
        for result in self.presentations([[state, path] for path in paths]):
            self.assertEqual((result["tone"], result["busy"]), ("warning", False))
            self.assertIn("budget", result["label"])
        state.update(status="report_interrupted", report={"status": "interrupted"})
        result = self.presentations([[state, "/report/id"]])[0]
        self.assertNotIn("budget", result["label"])
