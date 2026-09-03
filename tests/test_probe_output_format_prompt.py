"""Regression tests for the probe's machine-readable answer instruction."""

import unittest

from evolving_agent.prompts import probe_instructions


class ProbeOutputFormatPromptTests(unittest.TestCase):
    def test_json_contract_overrides_the_general_prose_report(self) -> None:
        instructions = probe_instructions()

        self.assertIn("Output format is a task-facing contract", instructions)
        self.assertIn("final reply must be exactly that value", instructions)
        self.assertIn("produce one parseable JSON value", instructions)
        self.assertIn("This format rule overrides", instructions)
        self.assertIn("usual request to state what you ran", instructions)


if __name__ == "__main__":
    unittest.main()
