import tempfile
import unittest
from pathlib import Path

from iptv_builder import (
    ChannelEntry,
    build_entries,
    build_m3u_lines,
    infer_quality,
    is_hd_quality,
    normalize_language_codes,
    validate_playlist,
    write_playlist,
)


class LanguageAndQualityTests(unittest.TestCase):
    def test_language_normalization(self):
        self.assertEqual(normalize_language_codes("FAS, kur;ARA"), {"fas", "kur", "ara"})
        self.assertIsNone(normalize_language_codes("all"))
        with self.assertRaises(ValueError):
            normalize_language_codes("all,fas")

    def test_quality_detection(self):
        self.assertTrue(is_hd_quality("720p"))
        self.assertTrue(is_hd_quality("1080i"))
        self.assertTrue(is_hd_quality("HD"))
        self.assertFalse(is_hd_quality("576i"))
        self.assertFalse(is_hd_quality("540p"))
        self.assertEqual(infer_quality({"feed": "HD"}, {"format": "576i"}), "HD")
        self.assertEqual(infer_quality({}, {"format": "576i"}), "576i")


class PlaylistBuilderTests(unittest.TestCase):
    def setUp(self):
        self.channels = [
            {"id": "news.ir", "name": "News, Persian", "country": "IR", "categories": ["news"]},
            {"id": "music.ir", "name": "Kurdish Music", "country": "IR", "categories": ["music"]},
            {"id": "arabic.ir", "name": "Arabic TV", "country": "IR", "categories": ["general"]},
            {"id": "adult.ir", "name": "Adult channel", "is_nsfw": True},
        ]
        self.feeds = [
            {"channel": "news.ir", "id": "SD", "languages": ["fas"], "format": "576i", "is_main": True},
            {"channel": "music.ir", "id": "SD", "languages": ["kur", "fas"], "format": "576i", "is_main": True},
            {"channel": "arabic.ir", "id": "SD", "languages": ["ara"], "format": "720p", "is_main": True},
            {"channel": "adult.ir", "id": "SD", "languages": ["fas"], "format": "720p", "is_main": True},
        ]
        self.streams = [
            {"channel": "news.ir", "feed": "SD", "title": "News feed", "url": "https://example.test/news.m3u8"},
            {"channel": "news.ir", "feed": "HD", "title": "News HD", "url": "https://example.test/news-hd.m3u8"},
            {"channel": "music.ir", "feed": "SD", "title": "Kurdish stream", "url": "https://example.test/music.m3u8"},
            {"channel": "arabic.ir", "feed": "SD", "title": "Arabic stream", "url": "https://example.test/arabic.m3u8"},
            {"channel": "adult.ir", "feed": "SD", "title": "Adult stream", "url": "https://example.test/adult.m3u8"},
            {"channel": "news.ir", "feed": "SD", "title": "Duplicate URL", "url": "https://example.test/news.m3u8"},
            {"channel": "news.ir", "feed": "SD", "title": "Unsafe URL", "url": "javascript:alert(1)"},
        ]

    def test_language_filter_fallback_nsfw_and_deduplication(self):
        entries = build_entries(self.channels, self.feeds, self.streams, langs=("fas",))
        self.assertEqual([entry.url for entry in entries], [
            "https://example.test/music.m3u8",
            "https://example.test/news-hd.m3u8",
            "https://example.test/news.m3u8",
        ])
        by_url = {entry.url: entry for entry in entries}
        self.assertEqual(by_url["https://example.test/news.m3u8"].name, "News, Persian")
        self.assertEqual(by_url["https://example.test/news.m3u8"].group, "News")
        self.assertFalse(by_url["https://example.test/news.m3u8"].is_hd)
        self.assertTrue(by_url["https://example.test/news-hd.m3u8"].is_hd)
        self.assertEqual(by_url["https://example.test/music.m3u8"].languages, ("kur", "fas"))

    def test_multilingual_filter_adds_channels(self):
        entries = build_entries(self.channels, self.feeds, self.streams, langs=("fas", "ara"))
        self.assertIn("https://example.test/arabic.m3u8", {entry.url for entry in entries})

    def test_m3u_output_and_atomic_validation(self):
        entry = ChannelEntry(
            channel_id='demo".ir',
            name="News, Persian",
            url="https://example.test/live.m3u8",
            group="News",
            languages=("fas",),
            quality="720p",
            country="IR",
        )
        lines = build_m3u_lines([entry])
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertIn("tvg-id=\"demo'.ir\"", lines[1])
        self.assertTrue(lines[1].endswith(",News, Persian"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "playlist.m3u"
            self.assertEqual(write_playlist([entry], path), 1)
            self.assertEqual(validate_playlist(path), 1)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_empty_main_playlist_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                write_playlist([], Path(directory) / "empty.m3u")


if __name__ == "__main__":
    unittest.main()
