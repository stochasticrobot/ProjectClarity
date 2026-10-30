"""Check that every task uses its user-owned prompt file without hidden copies.

These tests load real task modules, including the opt-in connection check, but do
not invoke models. File text is the authority; no test freezes the user's wording.
"""
import ast
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from projectclarity.prompt_loader import PROMPT_DIRECTORY, load_prompt


PROMPTS = {
    'projectclarity.coverage.contract_assessment': ('DIGEST_PROMPT', 'summarize_evidence.txt'),
    'projectclarity.nodes.design_answer.node': ('SYSTEM_PROMPT', 'design_answer.txt'),
    "projectclarity.nodes.plan_research.research_planner": ("SYSTEM_PROMPT", "research_planner.txt"),
    "projectclarity.nodes.select_sources.node": ("SYSTEM_PROMPT", "select_search_sources.txt"),
    "projectclarity.nodes.select_followup_sources.node": ("SYSTEM_PROMPT", "select_followup_sources.txt"),
    "projectclarity.extraction.knowledge_extraction_messages": ("SYSTEM_PROMPT", "extract_knowledge.txt"),
    "projectclarity.claim_review.claim_review_messages": ("SYSTEM_PROMPT", "review_claims.txt"),
    "projectclarity.nodes.assess_research_gaps.node": ("SYSTEM_PROMPT", "assess_research_gaps.txt"),
    "projectclarity.nodes.study_coverage.node": ("SYSTEM_PROMPT", "study_coverage.txt"),
    "projectclarity.nodes.accumulate_knowledge.node": ("SYSTEM_PROMPT", "capture_knowledge.txt"),
    "tests.llm_connection.live_connection_check": ("PROMPT", "connection_test.txt"),
}


class PromptFileTests(unittest.TestCase):
    """Verify loading and ownership boundaries without asserting particular prompt wording."""

    def test_relocated_planner_keeps_repository_credential_location(self):
        """Keep credential lookup at the checkout root after moving the planning node.

        Args:
            self: Test case comparing resolved paths without opening a credential file.

        Returns:
            None. Fail if moving the module redirects .env lookup into a node package.
        """
        from projectclarity.nodes.plan_research.research_planner import PROJECT_ENV_FILE

        repository = Path(__file__).resolve().parents[2]
        self.assertEqual(PROJECT_ENV_FILE, repository / '.env')

    def test_all_tasks_load_their_exact_file_without_inline_fallbacks(self):
        """Require each standalone task prompt to come from its named file.

        Args:
            self: Test case inspecting actual module constants and their assignments.

        Returns:
            None. Fail if a task has a copied/modified prompt, skips its loader, or
            if the directory contains a prompt omitted from this inventory.
        """
        self.assertEqual({path.name for path in PROMPT_DIRECTORY.glob('*.txt')},
                         {filename for _, filename in PROMPTS.values()})
        for module_name, (variable, filename) in PROMPTS.items():
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertEqual(getattr(module, variable),
                                 (PROMPT_DIRECTORY / filename).read_bytes().decode('utf-8'))
                tree = ast.parse(Path(module.__file__).read_text())
                assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets))
                self.assertIsInstance(assignment.value, ast.Call)
                self.assertEqual(assignment.value.func.id, 'load_prompt')
                self.assertEqual(ast.literal_eval(assignment.value.args[0]), filename)

    def test_verbatim_loading_and_missing_file_has_no_fallback(self):
        """Preserve user whitespace and report missing resources instead of substituting text.

        Args:
            self: Test case using an isolated temporary prompt directory.

        Returns:
            None. Fail if decoding changes line endings, trims text, expands braces,
            or a missing file silently supplies a default prompt.
        """
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            content = '  Wizard-owned {wording}\r\n\tKeep this.  \n'
            (directory / 'custom.txt').write_bytes(content.encode('utf-8'))
            with patch('projectclarity.prompt_loader.PROMPT_DIRECTORY', directory):
                self.assertEqual(load_prompt('custom.txt'), content)
                with self.assertRaises(FileNotFoundError):
                    load_prompt('missing.txt')

    def test_task_imports_work_outside_repository_working_directory(self):
        """Prove a fresh process resolves application resources, not working-directory files.

        Args:
            self: Test case spawning an isolated import-only Python process.

        Returns:
            None. Fail if any task prompt depends on launch location or importing
            a task unexpectedly tries a network operation. No model calls occur.
        """
        repository = PROMPT_DIRECTORY.parent.parent
        script = ('import importlib, socket\n'
                  'from unittest.mock import patch\n'
                  "with patch.object(socket.socket, 'connect', side_effect=AssertionError('No network')):\n"
                  f'    for name in {list(PROMPTS)!r}: importlib.import_module(name)\n')
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, '-c', script], cwd=temporary,
                env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(repository)},
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
