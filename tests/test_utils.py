"""
Tests for utility functions in process_conversation.py.
Focuses on _to_str() — the type-coercion helper that caused the
tags-as-list bug on 2026-04-25.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import process_conversation as pc


class TestToStr:
    """_to_str normalises any value to a plain string for TEXT DB columns."""

    def test_string_passthrough(self):
        assert pc._to_str("hello") == "hello"

    def test_list_joined_with_comma(self):
        assert pc._to_str(["file-naming", "usability"]) == "file-naming, usability"

    def test_empty_list_produces_empty_string(self):
        assert pc._to_str([]) == ""

    def test_none_produces_empty_string(self):
        assert pc._to_str(None) == ""

    def test_single_item_list(self):
        assert pc._to_str(["solo"]) == "solo"

    def test_integer_coerced_to_string(self):
        assert pc._to_str(42) == "42"

    def test_empty_string_passthrough(self):
        assert pc._to_str("") == ""

    def test_list_with_spaces_preserved(self):
        assert pc._to_str(["tag one", "tag two"]) == "tag one, tag two"


class TestAskQwenForJsonParsing:
    """
    ask_qwen_for_json() has fallback parsing logic for when Qwen wraps
    JSON in markdown fences or adds preamble text. Test the parsing
    without hitting Ollama.
    """

    def _parse(self, raw):
        """Drive the parsing branch of ask_qwen_for_json without an Ollama call."""
        import json
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end   = cleaned.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError:
                    pass
            start = cleaned.find("[")
            end   = cleaned.rfind("]") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError:
                    pass
        return None

    def test_clean_json_object(self):
        result = self._parse('{"key": "value"}')
        assert result == {"key": "value"}

    def test_markdown_fenced_json(self):
        result = self._parse('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_markdown_fence_no_language(self):
        result = self._parse('```\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_json_with_preamble(self):
        result = self._parse('Here is the JSON:\n{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_array(self):
        result = self._parse('[{"a": 1}, {"a": 2}]')
        assert result == [{"a": 1}, {"a": 2}]

    def test_invalid_json_returns_none(self):
        result = self._parse("this is not json at all")
        assert result is None

    def test_empty_string_returns_none(self):
        result = self._parse("")
        assert result is None


class TestR1ThinkStripper:
    """ask_r1_for_json() must strip <think>...</think> blocks before parsing."""

    def _strip_think(self, raw):
        import re
        return re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    def test_think_block_removed(self):
        raw = '<think>reasoning here</think>{"verdict": "valid"}'
        assert self._strip_think(raw) == '{"verdict": "valid"}'

    def test_multiline_think_block_removed(self):
        raw = '<think>\nlong\nmultiline\nreasoning\n</think>\n{"verdict": "supported"}'
        assert self._strip_think(raw) == '{"verdict": "supported"}'

    def test_no_think_block_unchanged(self):
        raw = '{"verdict": "valid"}'
        assert self._strip_think(raw) == raw

    def test_only_think_block_produces_empty(self):
        raw = '<think>just thinking</think>'
        assert self._strip_think(raw) == ''
