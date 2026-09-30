"""Structured workflow output compatibility stays narrow and schema checked."""
import unittest
from types import SimpleNamespace

from runtime.api_workflow import (
    AnswerReview,
    RequestAnalysis,
    review_evidence,
    structured_output,
)


class StructuredWorkflowOutputTests(unittest.TestCase):
    def test_analysis_list_field_accepts_gateway_object_shape(self):
        result = structured_output({
            "disposition": "execute",
            "objective": "search official docs",
            "completion_criteria": "include source URLs",
            "context_requirements": {
                "tools_needed": ["anysearch_batch_search"],
                "source": "connected MCP",
            },
        }, RequestAnalysis)

        self.assertEqual(result.completion_criteria, ["include source URLs"])
        self.assertEqual(result.context_requirements, [
            "tools_needed: anysearch_batch_search",
            "source: connected MCP",
        ])

    def test_review_null_optional_text_uses_schema_default(self):
        result = structured_output({
            "verdict": "complete",
            "supplement_prompt": None,
            "supplement_requires_tools": None,
            "reason": None,
        }, AnswerReview)

        self.assertEqual(result.supplement_prompt, "")
        self.assertFalse(result.supplement_requires_tools)
        self.assertEqual(result.reason, "")

    def test_invalid_required_decision_remains_rejected(self):
        with self.assertRaises(ValueError):
            structured_output({
                "objective": "search official docs",
                "disposition": "maybe",
            }, RequestAnalysis)

    def test_review_keeps_bounded_external_search_details_for_citations(self):
        result_text = ("search output " + ("x" * 900)
                       + " https://docs.langchain.com/oss/python/langgraph/graph-api "
                       + ("y" * 4000))
        view = review_evidence(SimpleNamespace(tool_calls=[{
            "name": "anysearch_batch_search",
            "status": "executed",
            "output_head": result_text,
        }], new_files=[], approvals_pending=0, approvals_decided=False,
            persistence_evidence=lambda: False))

        self.assertIn("https://docs.langchain.com/oss/python/langgraph/graph-api",
                      view["tool_calls"][0]["result"])
        self.assertLessEqual(len(view["tool_calls"][0]["result"]), 2600)


if __name__ == "__main__":
    unittest.main()
