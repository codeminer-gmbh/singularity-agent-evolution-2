import unittest

from evolving_agent.mcp_client import ToolOutcome
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.session import Deadline, ToolAgentSession


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.conversations = []

    def reply(self, *, conversation, tools=()):
        self.conversations.append(list(conversation))
        return next(self.replies)


class ScriptedTools:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def call(self, name, arguments):
        return next(self.outcomes)


def call(identifier, name):
    return ModelReply(
        text="",
        tool_calls=(ToolCall(identifier, name, "{}"),),
        output=({"type": "function_call", "call_id": identifier},),
    )


def answer(text):
    return ModelReply(text=text, tool_calls=())


class FinalEvidenceReviewTests(unittest.TestCase):
    def session(self, replies, outcomes, max_steps=10):
        model = ScriptedModel(replies)
        session = ToolAgentSession(
            model,
            ScriptedTools(outcomes),
            deadline=Deadline(30),
            max_steps=max_steps,
        )
        return model, session

    def test_successful_write_challenges_first_final_and_allows_verification(self):
        model, session = self.session(
            [
                call("write", "write_file"),
                answer("Implemented; trust me."),
                call("test", "run_command"),
                answer("Implemented; focused test passed."),
            ],
            [ToolOutcome("Wrote file.", False), ToolOutcome("exit code 0", False)],
        )

        outcome = session.run(instructions="system", opening="contract")

        self.assertEqual(outcome.summary, "Implemented; focused test passed.")
        self.assertEqual(outcome.steps, 4)
        review = model.conversations[2][-1]
        self.assertEqual(review["role"], "user")
        self.assertIn("Do not finalize yet", review["content"])
        self.assertIn("focused behavioral check", review["content"])
        self.assertIn("Implemented; trust me.", str(model.conversations[2]))
        self.assertEqual(model.conversations[2][0]["content"], "system")
        self.assertEqual(model.conversations[2][1]["content"], "contract")

    def test_failed_write_does_not_trigger_checkpoint(self):
        model, session = self.session(
            [call("write", "write_file"), answer("The write failed; unverified.")],
            [ToolOutcome("permission denied", True)],
        )

        outcome = session.run(instructions="system", opening="contract")

        self.assertEqual(outcome.steps, 2)
        self.assertEqual(len(model.conversations), 2)
        self.assertEqual(outcome.summary, "The write failed; unverified.")

    def test_new_write_rearms_checkpoint(self):
        model, session = self.session(
            [
                call("one", "write_file"),
                answer("first final"),
                call("two", "delete_path"),
                answer("second final"),
                answer("honest final"),
            ],
            [ToolOutcome("written", False), ToolOutcome("removed", False)],
        )

        outcome = session.run(instructions="system", opening="contract")

        self.assertEqual(outcome.steps, 5)
        self.assertEqual(outcome.summary, "honest final")
        self.assertIn("Do not finalize yet", model.conversations[2][-1]["content"])
        self.assertIn("Do not finalize yet", model.conversations[4][-1]["content"])


if __name__ == "__main__":
    unittest.main()
