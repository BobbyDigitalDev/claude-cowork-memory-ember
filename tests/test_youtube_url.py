"""
Tests for URL parsing and filename generation in fetch_youtube_transcript.py.
These were tested manually during development but never codified.
"""
import sys, os
from datetime import datetime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import fetch_youtube_transcript as yt


class TestExtractVideoId:

    def test_standard_watch_url(self):
        assert yt.extract_video_id("https://www.youtube.com/watch?v=8kNv3rjQaVA") == "8kNv3rjQaVA"

    def test_short_url(self):
        assert yt.extract_video_id("https://youtu.be/8kNv3rjQaVA") == "8kNv3rjQaVA"

    def test_embed_url(self):
        assert yt.extract_video_id("https://www.youtube.com/embed/8kNv3rjQaVA") == "8kNv3rjQaVA"

    def test_shorts_url(self):
        assert yt.extract_video_id("https://www.youtube.com/shorts/8kNv3rjQaVA") == "8kNv3rjQaVA"

    def test_bare_video_id(self):
        assert yt.extract_video_id("8kNv3rjQaVA") == "8kNv3rjQaVA"

    def test_watch_url_with_timestamp(self):
        assert yt.extract_video_id("https://www.youtube.com/watch?v=8kNv3rjQaVA&t=120s") == "8kNv3rjQaVA"

    def test_watch_url_timestamp_first(self):
        assert yt.extract_video_id("https://www.youtube.com/watch?t=30&v=8kNv3rjQaVA") == "8kNv3rjQaVA"

    def test_invalid_url_returns_none(self):
        assert yt.extract_video_id("https://www.google.com") is None

    def test_empty_string_returns_none(self):
        assert yt.extract_video_id("") is None


class TestTitleToFilename:

    def test_basic_title(self):
        result = yt.title_to_filename("Hello World")
        assert result == "hello_world"

    def test_special_characters_stripped(self):
        result = yt.title_to_filename("21 INSANE Use Cases For OpenClaw...")
        assert "!" not in result
        assert "." not in result

    def test_spaces_become_underscores(self):
        result = yt.title_to_filename("one two three")
        assert result == "one_two_three"

    def test_hyphens_become_underscores(self):
        result = yt.title_to_filename("step-by-step guide")
        assert result == "step_by_step_guide"

    def test_no_duplicate_underscores(self):
        result = yt.title_to_filename("hello   world")
        assert "__" not in result

    def test_lowercased(self):
        result = yt.title_to_filename("ALL CAPS TITLE")
        assert result == result.lower()

    def test_max_60_chars(self):
        long_title = "a" * 100
        result = yt.title_to_filename(long_title)
        assert len(result) <= 60

    def test_empty_title_returns_empty(self):
        result = yt.title_to_filename("")
        assert result == ""


class TestBuildTranscriptFilename:
    """
    build_transcript_filename(title, video_id, dt) should return a filename
    of the form: YYYY_MM_DD_<slug>_transcript.txt
    The date prefix makes the transcript folder scannable by ingestion date.
    """

    def _dt(self):
        return datetime(2026, 4, 25, 21, 45)

    def test_date_prefix_format(self):
        result = yt.build_transcript_filename("OpenClaw 4.24 Update", "abc123", self._dt())
        assert result.startswith("2026_04_25_")

    def test_title_slug_present(self):
        result = yt.build_transcript_filename("OpenClaw 4.24 Update", "abc123", self._dt())
        assert "openclaw" in result

    def test_ends_with_transcript_txt(self):
        result = yt.build_transcript_filename("OpenClaw 4.24 Update", "abc123", self._dt())
        assert result.endswith("_transcript.txt")

    def test_falls_back_to_video_id_when_no_title(self):
        result = yt.build_transcript_filename("", "abc1234wxyz", self._dt())
        assert "abc1234wxyz" in result
        assert result.startswith("2026_04_25_")

    def test_no_spaces_in_filename(self):
        result = yt.build_transcript_filename("Hello World Video", "abc123", self._dt())
        assert " " not in result

    def test_full_example(self):
        result = yt.build_transcript_filename(
            "OpenClaw 4.24: New AI Voice + Browser Updates", "4nqtyCSS7Fg", self._dt()
        )
        assert result == "2026_04_25_openclaw_424_new_ai_voice_browser_updates_transcript.txt"


class TestExtractChannelId:
    """extract_channel_id_from_url: pull UC... id from author_url when present."""

    def test_channel_url_format(self):
        url = "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx"
        assert yt.extract_channel_id_from_url(url) == "UCxxxxxxxxxxxxxxxxxxxxxx"

    def test_handle_format_returns_none(self):
        # @handle URLs don't contain the channel ID — can't extract without API
        url = "https://www.youtube.com/@somechannel"
        assert yt.extract_channel_id_from_url(url) is None

    def test_empty_string_returns_none(self):
        assert yt.extract_channel_id_from_url("") is None

    def test_none_returns_none(self):
        assert yt.extract_channel_id_from_url(None) is None

    def test_unrelated_url_returns_none(self):
        assert yt.extract_channel_id_from_url("https://www.google.com") is None

    def test_channel_id_starts_with_uc(self):
        url = "https://www.youtube.com/channel/UC_abc123XYZ"
        result = yt.extract_channel_id_from_url(url)
        assert result is not None
        assert result.startswith("UC")


class TestGetVideoMetadata:
    """get_video_metadata: returns dict with title, channel_name, channel_url, channel_id."""

    def test_returns_dict(self):
        from unittest.mock import patch
        mock_data = {
            "title": "Test Video",
            "author_name": "Test Channel",
            "author_url": "https://www.youtube.com/channel/UCtest123",
        }
        with patch("urllib.request.urlopen") as mock_open:
            import json
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            result = yt.get_video_metadata("abc123")
        assert isinstance(result, dict)

    def test_returns_required_keys(self):
        from unittest.mock import patch
        mock_data = {
            "title": "Test Video",
            "author_name": "Test Channel",
            "author_url": "https://www.youtube.com/channel/UCtest123",
        }
        with patch("urllib.request.urlopen") as mock_open:
            import json
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            result = yt.get_video_metadata("abc123")
        assert "title" in result
        assert "channel_name" in result
        assert "channel_url" in result
        assert "channel_id" in result

    def test_channel_id_extracted_from_channel_url(self):
        from unittest.mock import patch
        mock_data = {
            "title": "Test Video",
            "author_name": "Test Channel",
            "author_url": "https://www.youtube.com/channel/UCtest123abc",
        }
        with patch("urllib.request.urlopen") as mock_open:
            import json
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            result = yt.get_video_metadata("abc123")
        assert result["channel_id"] == "UCtest123abc"

    def test_channel_id_none_for_handle_url(self):
        from unittest.mock import patch
        mock_data = {
            "title": "Test Video",
            "author_name": "Test Channel",
            "author_url": "https://www.youtube.com/@testhandle",
        }
        with patch("urllib.request.urlopen") as mock_open:
            import json
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp
            result = yt.get_video_metadata("abc123")
        assert result["channel_id"] is None

    def test_returns_empty_strings_on_network_failure(self):
        from unittest.mock import patch
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = yt.get_video_metadata("abc123")
        assert result["title"] == ""
        assert result["channel_name"] == ""
        assert result["channel_url"] == ""
        assert result["channel_id"] is None
