"""Check that relevance selection can choose existing facts but cannot invent replacements.

The tests supply stored extraction results and controlled selection replies. They
check accepted input formats, fact identifiers, reasons and the copying of original
records into a separate selected-information file. No live AI judgment is evaluated;
the purpose is to protect source integrity when a selection is applied.
"""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from projectclarity.evidence_selection.relevance_selection import main, prepare_knowledge, relevant_knowledge, selection_request, validate_selection, ROOT

DATA = {"entities": [{"id": "acme", "name": "Acme", "type": "organization", "subtype": "business"},
                     {"id": "fund", "name": "South Fund", "type": "organization", "subtype": None}],
        "facts": [{"subject_id": "acme", "predicate": "did_not_receive_funding", "value": "South Fund",
                   "object_id": "fund", "statement": "Acme did not receive funding from South Fund.",
                   "quote": "Acme did not receive funding from South Fund."}]}
PLAN = {"information_needed": [{"kind": "relationship", "name": "Funding", "description": "Identify who funded Acme."}]}


class SelectionTests(unittest.TestCase):
    """Exercise input compatibility, reference validation and non-destructive output."""

    def test_stable_ids_and_namespaces(self):
        """Check unchanged artifacts keep IDs and distinct artifacts cannot share them.

        Args: None.
        Returns: None; asserts determinism and preserved original fact contents.
        """
        a = prepare_knowledge(DATA, "a" * 64)
        self.assertEqual(a, prepare_knowledge(DATA, "a" * 64))
        self.assertNotEqual(a["facts"][0]["fact_id"], prepare_knowledge(DATA, "b" * 64)["facts"][0]["fact_id"])
        self.assertEqual(a["facts"][0]["fact"], DATA["facts"][0])

    def test_receipt_quotes_are_rechecked(self):
        """Accept successful extraction receipts while rejecting bad literal evidence.

        Args: None.
        Returns: None; checks that a valid receipt and an altered quote differ.
        """
        receipt = {"status": "checks_pass", "parsed": DATA, "request": {"messages": [{"content": json.dumps({"document_chunk": DATA['facts'][0]['quote']})}]}}
        self.assertTrue(prepare_knowledge(receipt, "a" * 64)["literal_quotes_rechecked"])
        broken = copy.deepcopy(receipt)
        broken["parsed"]["facts"][0]["quote"] = "Not in source"
        with self.assertRaises(ValueError):
            prepare_knowledge(broken, "a" * 64)

    def test_failed_receipt_and_dangling_entities_rejected(self):
        """Block failed upstream data and ambiguous or dangling entity references.

        Args: None.
        Returns: None; invalid inputs raise before any model call.
        """
        with self.assertRaises(ValueError):
            prepare_knowledge({"status": "failed", "parsed": DATA}, "a" * 64)
        broken = copy.deepcopy(DATA)
        broken["facts"][0]["object_id"] = "missing"
        with self.assertRaises(ValueError):
            prepare_knowledge(broken, "a" * 64)

    def test_unknown_duplicate_and_blank_selections_rejected(self):
        """Ensure the model cannot invent references or return ambiguous selections.

        Args: None.
        Returns: None; malformed selection lists fail local validation.
        """
        knowledge = prepare_knowledge(DATA, "a" * 64)
        item = {"fact_id": knowledge["facts"][0]["fact_id"], "reason": "Contradicts funding claim."}
        for items in ([dict(item, fact_id="invented")], [item, item], [dict(item, reason=" ")]):
            with self.assertRaises(ValueError):
                validate_selection(json.dumps({"selected_facts": items}), knowledge)

    def test_selected_originals_and_negation_preserved(self):
        """Check selected facts are copied by code, with evidence and related entities.

        Args: None.
        Returns: None; verifies source immutability and empty/nonempty selections.
        """
        original = copy.deepcopy(DATA)
        knowledge = prepare_knowledge(DATA, "a" * 64)
        selected = validate_selection(json.dumps({"selected_facts": [{"fact_id": knowledge["facts"][0]["fact_id"], "reason": "A funding denial is relevant."}]}), knowledge)
        store = relevant_knowledge(selected, knowledge)
        self.assertEqual(store["facts"][0]["fact"], DATA["facts"][0])
        self.assertEqual(store["entities"], DATA["entities"])
        empty = relevant_knowledge(validate_selection('{"selected_facts":[]}', knowledge), knowledge)
        self.assertEqual(empty["facts"], [])
        self.assertEqual(len(empty["unselected_fact_ids"]), 1)
        self.assertEqual(DATA, original)

    def test_request_uses_exact_prompt_and_entire_batch(self):
        """Verify prompt ownership and lossless chunk-level input construction.

        Args: None.
        Returns: None; asserts the separate prompt file and full fact data are used.
        """
        knowledge = prepare_knowledge(DATA, "a" * 64)
        request = selection_request("Who funded Acme?", PLAN["information_needed"], knowledge, "test-model", 4096)
        self.assertEqual(request["system"], (ROOT / "prompts/select_relevant_evidence.txt").read_text())
        payload = json.loads(request["messages"][0]["content"])
        self.assertEqual(payload["facts"], knowledge["facts"])
        self.assertEqual(payload["entities"], DATA["entities"])

    def test_empty_chunk_skips_provider_and_secrets(self):
        """Prove empty extraction produces an empty store without a model call.

        Args: None.
        Returns: None; temporary input files stay unchanged and artifacts show zero calls.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge, requirements = root / "knowledge.json", root / "requirements.json"
            knowledge.write_text('{"entities":[],"facts":[]}')
            requirements.write_text(json.dumps(PLAN))
            before = knowledge.read_bytes()
            with patch("projectclarity.evidence_selection.relevance_selection.Anthropic") as client, patch("projectclarity.evidence_selection.relevance_selection.dotenv_values") as secrets, contextlib.redirect_stdout(io.StringIO()):
                code = main([str(knowledge), "--question", "Who funded Acme?", "--requirements", str(requirements), "--output-root", str(root / "runs")])
            self.assertEqual(code, 0)
            client.assert_not_called()
            secrets.assert_not_called()
            folder = next((root / "runs").iterdir())
            self.assertEqual(json.loads((folder / "summary.json").read_text())["attempted_calls"], 0)
            self.assertEqual(json.loads((folder / "relevant_knowledge.json").read_text())["facts"], [])
            self.assertEqual(knowledge.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
