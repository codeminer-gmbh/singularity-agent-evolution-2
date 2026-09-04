import tempfile
import unittest
from pathlib import Path

from evolving_agent.commands import CommandRunner
from evolving_agent.tools import ToolFailureError, WorkspaceTools
from evolving_agent.workspace import Workspace


class ReadFilePagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tools = WorkspaceTools(Workspace(self.root), CommandRunner(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_later_page_is_reachable_with_reported_byte_offset(self) -> None:
        content = "A" * 7_000 + "DECISIVE-LATER-EVIDENCE"
        (self.root / "ledger.md").write_text(content, encoding="utf-8")

        first = self.tools.call("read_file", {"path": "ledger.md"})
        second = self.tools.call(
            "read_file", {"path": "ledger.md", "offset_bytes": 7_000}
        )

        self.assertNotIn("DECISIVE-LATER-EVIDENCE", first)
        self.assertIn("continue with offset_bytes=7000", first)
        self.assertEqual(second, "DECISIVE-LATER-EVIDENCE")

    def test_page_size_is_selectable_and_out_of_range_offset_is_actionable(self) -> None:
        (self.root / "short.txt").write_text("0123456789", encoding="utf-8")

        page = self.tools.call(
            "read_file", {"path": "short.txt", "offset_bytes": 3, "max_bytes": 4}
        )
        self.assertTrue(page.startswith("3456\n"))
        self.assertIn("continue with offset_bytes=7", page)

        with self.assertRaisesRegex(ToolFailureError, "past the end.*10 bytes"):
            self.tools.call(
                "read_file", {"path": "short.txt", "offset_bytes": 11}
            )

    def test_tool_contract_publishes_paging_arguments(self) -> None:
        definition = next(
            item for item in self.tools.definitions() if item.name == "read_file"
        )
        properties = definition.input_schema["properties"]
        self.assertIn("offset_bytes", properties)
        self.assertEqual(properties["max_bytes"]["maximum"], 7_000)


if __name__ == "__main__":
    unittest.main()
