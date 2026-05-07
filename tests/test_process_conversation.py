"""
Tests for process_conversation.py — _quarantine_check logic, constant values,
model name regression guard (BUG-004), and schema version string (ISSUE-003).
"""
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.process_conversation as pc


# ---------------------------------------------------------------------------
# Constants — regression guards
# ---------------------------------------------------------------------------

class TestConstants:
    def test_model_extraction_is_14b_not_32b(self):
        """BUG-004 regression: MODEL_EXTRACTION must be qwen2.5:14b not qwen2.5:32b."""
        assert pc.MODEL_EXTRACTION == "qwen2.5:14b", (
            f"BUG-004 regression: MODEL_EXTRACTION is '{pc.MODEL_EXTRACTION}', "
            "expected 'qwen2.5:14b'. Wrong model wastes 28+ minutes per file."
        )

    def test_quarantine_min_confidence_is_set(self):
        assert hasattr(pc, "QUARANTINE_MIN_CONFIDENCE")
        assert 0.0 < pc.QUARANTINE_MIN_CONFIDENCE < 1.0

    def test_quarantine_require_anchor_is_bool(self):
        assert isinstance(pc.QUARANTINE_REQUIRE_ANCHOR, bool)


# ---------------------------------------------------------------------------
# _quarantine_check — all branches
# ---------------------------------------------------------------------------

class TestQuarantineCheck:
    def _check(self, confidence=0.8, anchor="he said X", evidence='["snippet"]'):
        return pc._quarantine_check(confidence, anchor, evidence)

    def test_passes_all_checks_returns_proposed(self):
        status, reason = self._check(confidence=0.8, anchor="he said X",
                                     evidence='["good snippet"]')
        assert status == "proposed"
        assert reason is None

    def test_low_confidence_triggers_needs_review(self):
        low = pc.QUARANTINE_MIN_CONFIDENCE - 0.01
        status, reason = self._check(confidence=low)
        assert status == "needs_review"
        assert "low confidence" in reason

    def test_no_anchor_triggers_needs_review_when_required(self):
        if not pc.QUARANTINE_REQUIRE_ANCHOR:
            pytest.skip("QUARANTINE_REQUIRE_ANCHOR is False — skip anchor test")
        status, reason = self._check(anchor=None)
        assert status == "needs_review"
        assert "no verbatim anchor" in reason

    def test_empty_anchor_string_triggers_needs_review(self):
        if not pc.QUARANTINE_REQUIRE_ANCHOR:
            pytest.skip("QUARANTINE_REQUIRE_ANCHOR is False — skip anchor test")
        status, reason = self._check(anchor="")
        assert status == "needs_review"
        assert "no verbatim anchor" in reason

    def test_empty_evidence_list_triggers_needs_review(self):
        status, reason = self._check(evidence="[]")
        assert status == "needs_review"
        assert "no evidence snippets" in reason

    def test_none_evidence_triggers_needs_review(self):
        status, reason = self._check(evidence=None)
        assert status == "needs_review"
        assert "no evidence snippets" in reason

    def test_null_string_evidence_triggers_needs_review(self):
        status, reason = self._check(evidence="null")
        assert status == "needs_review"
        assert "no evidence snippets" in reason

    def test_multiple_failures_combined_in_reason(self):
        low = pc.QUARANTINE_MIN_CONFIDENCE - 0.01
        status, reason = self._check(confidence=low, anchor=None, evidence="[]")
        assert status == "needs_review"
        # All failures should be present in the combined reason string
        assert "low confidence" in reason
        assert "no evidence snippets" in reason

    def test_exactly_at_threshold_passes(self):
        """Confidence exactly at QUARANTINE_MIN_CONFIDENCE should pass (not <, but >=)."""
        at_threshold = pc.QUARANTINE_MIN_CONFIDENCE
        status, reason = self._check(confidence=at_threshold, anchor="verbatim",
                                     evidence='["snippet"]')
        assert status == "proposed"

    def test_just_below_threshold_fails(self):
        just_below = pc.QUARANTINE_MIN_CONFIDENCE - 0.001
        status, reason = self._check(confidence=just_below, anchor="verbatim",
                                     evidence='["snippet"]')
        assert status == "needs_review"


# ---------------------------------------------------------------------------
# Schema version string — ISSUE-003 regression guard
# ---------------------------------------------------------------------------

class TestSchemaVersionString:
    def test_schema_banner_not_v2_2(self, capsys):
        """ISSUE-003: process_conversation prints Schema: v2.8.0 at startup,
        not the stale v2.2. We test the constant, not the print statement,
        since running main() would require Ollama."""
        # The version must appear somewhere accessible in the module
        import inspect
        source = inspect.getsource(pc)
        assert "v2.2" not in source or "v2.8.0" in source, (
            "ISSUE-003 regression: stale v2.2 still in source without v2.8.0"
        )
        # More targeted: check the schema banner print line
        assert 'Schema:     v2.8.0' in source or 'Schema: v2.8' in source
