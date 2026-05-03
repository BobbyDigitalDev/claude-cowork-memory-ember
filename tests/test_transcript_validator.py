"""
Tests for transcript_validator.py.

Public API: validate_one(filename) → (exit_code, warnings, errors)
exit_code: 0 = clean, 1 = warnings, 2 = errors/not found.
The validator requires at least 3 Claude blocks — test transcripts must reflect that.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import tempfile
import pytest
import transcript_validator as tv

_LONG_RESPONSE = (
    "The retrieval system uses three strategies. The first is semantic search, "
    "which embeds the query using nomic-embed-text and computes cosine similarity "
    "against stored memory chunks. The second is structural search, using keyword "
    "matching across beliefs, goals, questions, entities, and concepts with a "
    "stop-word tokenizer. The third is temporal search, surfacing recent high-value "
    "items weighted by age decay. All three results are merged and deduplicated "
    "by source type and ID before being returned to the caller."
)

_LONG_RESPONSE_2 = (
    "The three-role model stack assigns distinct responsibilities to each local model. "
    "Qwen 2.5 14B handles fast structured extraction. DeepSeek R1 14B handles deep "
    "reasoning tasks like belief verification and validation. Nomic Embed Text generates "
    "the 768-dimension semantic embeddings stored in memory_chunks. This separation "
    "keeps extraction fast while reserving the reasoning model for tasks that genuinely "
    "require it, matching the local-first cost discipline."
)

_LONG_RESPONSE_3 = (
    "The checkpoint system uses a processing_jobs table to record the status of each "
    "extraction call. Before running a Qwen call, the script checks for a completed "
    "row matching the call name and source file. If found and the per-step debug file "
    "exists, it loads the saved result and skips the call entirely. If not, it inserts "
    "a started row, runs the extraction, writes the debug file, and marks completed. "
    "On exception it marks failed and re-raises so the user sees the error."
)


def _write_and_validate(content):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                     prefix="test_2026_01_01_001_") as f:
        f.write(content)
        path = f.name
    try:
        code, warnings, errors = tv.validate_one(path)
    finally:
        os.unlink(path)
    return code, warnings, errors


def _make_transcript(exchanges):
    lines = [
        "# Conversation test_2026_01_01_001",
        "**Date:** 2026-01-01",
        "",
        "---",
        "",
    ]
    for speaker, text in exchanges:
        lines.append(f"**{speaker}:** {text}")
        lines.append("")
    return "\n".join(lines)


def _full_session(extra_exchanges=None):
    """Build a clean 3-exchange transcript (minimum for validator)."""
    exchanges = [
        ("Bobby", "Can you explain how the retrieval system works?"),
        ("Claude", _LONG_RESPONSE),
        ("Bobby", "And the model stack?"),
        ("Claude", _LONG_RESPONSE_2),
        ("Bobby", "How do the checkpoints work?"),
        ("Claude", _LONG_RESPONSE_3),
    ]
    if extra_exchanges:
        exchanges.extend(extra_exchanges)
    return _make_transcript(exchanges)


class TestCleanTranscript:

    def test_clean_transcript_exits_zero(self):
        code, warnings, errors = _write_and_validate(_full_session())
        assert code == 0, f"Expected clean exit, got warnings={warnings} errors={errors}"

    def test_clean_transcript_no_warnings(self):
        code, warnings, _ = _write_and_validate(_full_session())
        assert len(warnings) == 0


class TestSummaryDetection:

    def test_summary_style_opening_phrase_flagged(self):
        content = _full_session([
            ("Bobby", "What did we accomplish?"),
            ("Claude", "Implemented the checkpoint system with five helpers."),
        ])
        code, _, _ = _write_and_validate(content)
        assert code != 0

    def test_very_short_claude_block_flagged(self):
        content = _full_session([
            ("Bobby", "Did it work?"),
            ("Claude", "Yes."),
        ])
        code, _, _ = _write_and_validate(content)
        assert code != 0

    def test_short_bobby_message_not_flagged(self):
        """Short Bobby messages are fine — only Claude blocks are length-checked."""
        content = _full_session([
            ("Bobby", "go"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"Short Bobby message should not trigger warnings: {warnings}"


class TestPlaceholderDetection:

    def test_bracketed_placeholder_in_bobby_block_flagged(self):
        content = _full_session([
            ("Bobby", "[ran the script on his Mac]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, _, _ = _write_and_validate(content)
        assert code != 0


class TestTerminalOutputRecognition:
    """
    Bobby's messages sometimes contain bracketed terminal output summaries instead
    of actual typed text. These are legitimate — we agreed not to paste full terminal
    output but to use bracketed summaries for it. The validator should recognize these
    and not flag them as placeholders.

    Terminal output markers are distinguished from genuine placeholders by the presence
    of: a .py/.sh script name, numeric results, command-line flags (--), or the word
    "output" following a script reference.
    """

    def test_script_run_marker_not_flagged(self):
        """[ran refresh_recent_memory.py — version 22] is terminal output, not a placeholder."""
        content = _full_session([
            ("Bobby", "[ran refresh_recent_memory.py -- version 22, 1581 words]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"Script run marker should not be flagged: {warnings}"

    def test_embed_memories_output_not_flagged(self):
        """[ran embed_memories.py -- 182 new chunks] is terminal output."""
        content = _full_session([
            ("Bobby", "[ran embed_memories.py -- 182 new chunks, corpus 198 to 380]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"embed_memories output marker should not be flagged: {warnings}"

    def test_verify_beliefs_output_not_flagged(self):
        """[verify_beliefs.py output -- 20 beliefs checked] is terminal output."""
        content = _full_session([
            ("Bobby", "[verify_beliefs.py output -- 20 beliefs, 8 verified, 1 disputed]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"verify_beliefs output marker should not be flagged: {warnings}"

    def test_process_research_output_not_flagged(self):
        """[ran process_research.py --all -- 9 processed, 0 failed] is terminal output."""
        content = _full_session([
            ("Bobby", "[ran process_research.py --all -- 9 processed, 1 skipped, 0 failed]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"process_research output marker should not be flagged: {warnings}"

    def test_genuine_placeholder_still_flagged(self):
        """[paste the terminal output here] is a real placeholder and should still be flagged."""
        content = _full_session([
            ("Bobby", "[paste the terminal output here when you have it]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, _, _ = _write_and_validate(content)
        assert code != 0

    def test_vague_action_placeholder_still_flagged(self):
        """[ran the script on his Mac] has no script name and should still be flagged."""
        content = _full_session([
            ("Bobby", "[ran the script on his Mac and it worked fine]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, _, _ = _write_and_validate(content)
        assert code != 0

    def test_session_resumed_marker_not_flagged(self):
        """[session resumed after context compaction] is a structural marker, not a placeholder."""
        content = _full_session([
            ("Bobby", "[session resumed after context compaction — continuing from bobby_2026_04_25_001]"),
            ("Claude", _LONG_RESPONSE),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"Session resumed marker should not be flagged: {warnings}"


class TestCodeBlockExemption:

    def test_short_response_with_code_block_not_flagged(self):
        """A one-liner + code block is legitimately short — the code is the content."""
        short_with_code = (
            "Good. Now run the deep memory one:\n\n"
            "```bash\n"
            "python3 ~/claude_memory/scripts/refresh_deep_memory.py\n"
            "```"
        )
        content = _full_session([
            ("Bobby", "ok"),
            ("Claude", short_with_code),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"Short response with code block should not be flagged: {warnings}"

    def test_short_response_without_code_block_still_flagged(self):
        """A short response with no code block is still suspicious."""
        content = _full_session([
            ("Bobby", "Did it work?"),
            ("Claude", "Yes it worked fine."),
        ])
        code, _, _ = _write_and_validate(content)
        assert code != 0

    def test_noted_opener_not_flagged(self):
        """'Noted.' is a legitimate response opener, not a summary marker."""
        noted_response = (
            "Noted. From here on, before we build any new feature or script, "
            "we write at least a skeleton test first that defines what success "
            "looks like. When the feature stabilizes, we flesh the tests out. "
            "The rule: no new script or feature ships without a corresponding "
            "test file. If we are mid-build and realize we missed it, we stop "
            "and write the test before continuing. I will flag it if we are "
            "about to build something and I have not mentioned tests yet."
        )
        content = _full_session([
            ("Bobby", "ok let's fold tests in earlier in our processes from here on out. That is a directive."),
            ("Claude", noted_response),
        ])
        code, warnings, _ = _write_and_validate(content)
        assert code == 0, f"'Noted.' opener should not be flagged: {warnings}"


class TestFileNotFound:

    def test_nonexistent_file_returns_error_code(self):
        code, warnings, errors = tv.validate_one("/tmp/does_not_exist_ever.md")
        assert code == 2
        assert len(errors) > 0
