#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build Nexa M3U playlists from the public iptv-org API."""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("nexa")

CHANNELS_URL = os.environ.get(
    "CHANNELS_URL", "https://iptv-org.github.io/api/channels.json"
).strip()
FEEDS_URL = os.environ.get(
    "FEEDS_URL", "https://iptv-org.github.io/api/feeds.json"
).strip()
STREAMS_URL = os.environ.get(
    "STREAMS_URL", "https://iptv-org.github.io/api/streams.json"
).strip()
DEFAULT_LANGS = os.environ.get("LANGS", "fas").strip()
DEFAULT_EXTENDED_LANGS = os.environ.get("EXTENDED_LANGS", "fas,kur").strip()
DEFAULT_OUTPUT_DIR = os.environ.get("OUTPUT_DIR", ".").strip() or "."
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0


@dataclass(frozen=True)
class ChannelEntry:
    channel_id: str
    name: str
    url: str
    group: str
    languages: Tuple[str, ...]
    quality: str
    country: str = ""

    @property
    def is_hd(self) -> bool:
        return is_hd_quality(self.quality)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def normalize_language_codes(value: Optional[Iterable[str]]) -> Optional[Set[str]]:
    """Return normalized language codes; None means no language filter (all)."""
    if value is None:
        return None
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    else:
        values = list(value)
    codes = {str(code).strip().lower() for code in values if str(code).strip()}
    if not codes or codes == {"all"}:
        return None
    if "all" in codes:
        raise ValueError("'all' cannot be combined with individual language codes")
    return codes


def _language_list(value: object) -> Tuple[str, ...]:
    if isinstance(value, str):
        raw = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result = []
    seen = set()
    for item in raw:
        code = str(item).strip().lower()
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return tuple(result)


def _matches_languages(feed: dict, selected: Optional[Set[str]]) -> bool:
    if selected is None:
        return True
    return bool(selected.intersection(_language_list(feed.get("languages"))))


def _safe_http_url(value: object) -> str:
    url = str(value or "").strip()
    if not url or any(char.isspace() for char in url):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _feed_for_stream(stream: dict, candidates: Sequence[dict]) -> Optional[dict]:
    """Find the exact feed for a stream, or a safe channel-level fallback."""
    stream_feed = str(stream.get("feed") or "").strip()
    if stream_feed:
        exact = next(
            (feed for feed in candidates if str(feed.get("id") or "").strip() == stream_feed),
            None,
        )
        if exact is not None:
            return exact
    if not candidates:
        return None
    # Some valid upstream streams use an HD feed that is not yet listed in
    # feeds.json. In that case use the selected-language channel feed as metadata.
    return sorted(
        candidates,
        key=lambda feed: (
            not bool(feed.get("is_main")),
            str(feed.get("id") or "").upper() != "SD",
            str(feed.get("id") or ""),
        ),
    )[0]


def infer_quality(stream: dict, feed: Optional[dict]) -> str:
    """Use stream quality first, then feed format, while retaining HD/SD labels."""
    stream_quality = str(stream.get("quality") or "").strip()
    if stream_quality:
        return stream_quality

    stream_feed = str(stream.get("feed") or "").strip()
    if re.search(r"\b(?:uhd|hd|4k|8k)\b", stream_feed, re.IGNORECASE):
        return stream_feed

    feed_format = str((feed or {}).get("format") or "").strip()
    if feed_format:
        return feed_format
    if stream_feed:
        return stream_feed
    return "Unknown"


def is_hd_quality(value: object) -> bool:
    text = str(value or "").strip().lower()
    if re.search(r"\b(?:uhd|hd|4k|8k)\b", text):
        return True
    match = re.search(r"(?<!\d)(\d{3,4})\s*[pi]?\b", text)
    return bool(match and int(match.group(1)) >= 720)


def _category_label(channel: dict, country: str) -> str:
    categories = channel.get("categories") or []
    if isinstance(categories, str):
        categories = [item for item in re.split(r"[,;]", categories) if item.strip()]
    if categories:
        label = str(categories[0]).strip().replace("_", " ")
        return label[:1].upper() + label[1:] if label else "General"
    return country or "General"


def build_entries(
    channels: Sequence[dict],
    feeds: Sequence[dict],
    streams: Sequence[dict],
    langs: Optional[Iterable[str]] = ("fas",),
    include_nsfw: bool = False,
) -> List[ChannelEntry]:
    """Join iptv-org channel/feed/stream records into playable M3U entries.

    Streams are joined by both channel and feed when possible. A channel-level
    fallback is intentionally supported for upstream HD feeds that have a stream
    but no corresponding feeds.json row.
    """
    selected_languages = normalize_language_codes(langs)
    channels_by_id: Dict[str, dict] = {}
    for channel in channels:
        channel_id = str(channel.get("id") or "").strip()
        if channel_id:
            channels_by_id[channel_id] = channel

    feeds_by_channel: Dict[str, List[dict]] = {}
    for feed in feeds:
        channel_id = str(feed.get("channel") or "").strip()
        if channel_id:
            feeds_by_channel.setdefault(channel_id, []).append(feed)

    entries: List[ChannelEntry] = []
    seen_urls = set()
    for stream in streams:
        channel_id = str(stream.get("channel") or "").strip()
        if not channel_id:
            continue

        channel = channels_by_id.get(channel_id, {"id": channel_id, "name": channel_id})
        if not include_nsfw and bool(channel.get("is_nsfw")):
            continue

        channel_feeds = feeds_by_channel.get(channel_id, [])
        selected_feeds = [
            feed for feed in channel_feeds if _matches_languages(feed, selected_languages)
        ]
        if not selected_feeds:
            continue

        stream_feed_id = str(stream.get("feed") or "").strip()
        matching_feed = next(
            (
                feed
                for feed in channel_feeds
                if stream_feed_id
                and str(feed.get("id") or "").strip() == stream_feed_id
            ),
            None,
        )
        # If an exact feed exists but is for another language, do not leak it into
        # the selected-language playlist. Otherwise allow the documented fallback.
        if matching_feed is not None:
            if not _matches_languages(matching_feed, selected_languages):
                continue
            feed = matching_feed
        else:
            feed = _feed_for_stream(stream, selected_feeds)

        url = _safe_http_url(stream.get("url"))
        if not feed or not url or url in seen_urls:
            continue
        seen_urls.add(url)

        languages = _language_list(feed.get("languages"))
        country = str(channel.get("country") or "").strip().upper()
        stream_title = str(stream.get("title") or "").strip()
        channel_name = str(channel.get("name") or "").strip()
        name = channel_name or stream_title or channel_id
        group = _category_label(channel, country)
        quality = infer_quality(stream, feed)

        entries.append(
            ChannelEntry(
                channel_id=channel_id,
                name=name,
                url=url,
                group=group,
                languages=languages,
                quality=quality,
                country=country,
            )
        )

    entries.sort(key=lambda item: (item.name.casefold(), item.channel_id.casefold(), item.url))
    return entries


def _clean_attribute(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace('"', "'").strip()


def build_m3u_lines(entries: Sequence[ChannelEntry]) -> List[str]:
    lines = ["#EXTM3U"]
    for entry in entries:
        attributes = [
            ("tvg-id", entry.channel_id),
            ("tvg-name", entry.name),
            ("group-title", entry.group),
            ("tvg-language", ";".join(entry.languages)),
            ("tvg-quality", entry.quality),
        ]
        if entry.country:
            attributes.append(("tvg-country", entry.country))
        attr_text = " ".join(
            '{}="{}"'.format(key, _clean_attribute(value))
            for key, value in attributes
            if _clean_attribute(value)
        )
        lines.append("#EXTINF:-1 {},{}".format(attr_text, _clean_attribute(entry.name)))
        lines.append(entry.url)
    return lines


def write_playlist(
    entries: Sequence[ChannelEntry], path: Path, allow_empty: bool = False
) -> int:
    if not entries and not allow_empty:
        raise ValueError("Refusing to write an empty main playlist: {}".format(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(build_m3u_lines(entries)) + "\n"
    temporary_path = path.with_name(path.name + ".tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8", newline="\n")
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return len(entries)


def validate_playlist(path: Path, allow_empty: bool = False) -> int:
    if not path.exists():
        raise FileNotFoundError("Playlist was not created: {}".format(path))
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "#EXTM3U":
        raise ValueError("Playlist is missing its #EXTM3U header: {}".format(path))
    channel_count = sum(1 for line in lines if line.startswith("#EXTINF:"))
    if channel_count == 0 and not allow_empty:
        raise ValueError("Playlist contains no channels: {}".format(path))
    if channel_count != sum(1 for line in lines if line.startswith("http://") or line.startswith("https://")):
        raise ValueError("Playlist has an EXTINF entry without a corresponding HTTP(S) URL: {}".format(path))
    return channel_count


def write_profile(entries: Sequence[ChannelEntry], output_dir: Path, prefix: str = "") -> Dict[str, int]:
    stem = "playlist" + ("_" + prefix if prefix else "")
    all_path = output_dir / (stem + ".m3u")
    hd_path = output_dir / (stem + "_hd.m3u")
    sd_path = output_dir / (stem + "_sd.m3u")

    write_playlist(entries, all_path)
    write_playlist([entry for entry in entries if entry.is_hd], hd_path, allow_empty=True)
    write_playlist([entry for entry in entries if not entry.is_hd], sd_path, allow_empty=True)

    return {
        all_path.name: validate_playlist(all_path),
        hd_path.name: validate_playlist(hd_path, allow_empty=True),
        sd_path.name: validate_playlist(sd_path, allow_empty=True),
    }


def fetch_json_list(url: str, timeout: int, retries: int, retry_delay: float) -> List[dict]:
    if not url or not url.strip():
        raise ValueError("API URL is empty")

    last_error = None
    headers = {
        "User-Agent": "nexa-iptv-builder/2.0 (+https://github.com/AnishtayiN/nexa)",
        "Accept": "application/json",
    }
    for attempt in range(1, retries + 1):
        try:
            LOG.info("Downloading %s (attempt %d/%d)", url, attempt, retries)
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
            data = json.loads(payload.decode("utf-8-sig"))
            if isinstance(data, dict):
                data = data.get("data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                raise ValueError("Expected a JSON array of objects from {}".format(url))
            return data
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            LOG.warning("Could not download %s: %s", url, exc)
            if attempt < retries:
                time.sleep(retry_delay * attempt)

    raise RuntimeError(
        "Failed to fetch {} after {} attempt(s): {}".format(url, retries, last_error)
    ) from last_error


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Persian and multilingual Nexa IPTV M3U playlists from public data."
    )
    parser.add_argument("--channels-url", default=CHANNELS_URL, help="iptv-org channels.json URL")
    parser.add_argument("--feeds-url", default=FEEDS_URL, help="iptv-org feeds.json URL")
    parser.add_argument("--streams-url", default=STREAMS_URL, help="iptv-org streams.json URL")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for generated M3U files")
    parser.add_argument("--langs", default=DEFAULT_LANGS, help="Main playlist languages, comma-separated (default: fas)")
    parser.add_argument(
        "--extended-langs",
        default=DEFAULT_EXTENDED_LANGS,
        help="Extended playlist languages, comma-separated (default: fas,kur)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="HTTP retry count")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Base delay between retries")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.retries <= 0:
        parser.error("--retries must be greater than zero")
    if args.retry_delay < 0:
        parser.error("--retry-delay cannot be negative")
    try:
        normalize_language_codes(args.langs)
        normalize_language_codes(args.extended_langs)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    output_dir = Path(args.output_dir)

    try:
        channels = fetch_json_list(args.channels_url, args.timeout, args.retries, args.retry_delay)
        feeds = fetch_json_list(args.feeds_url, args.timeout, args.retries, args.retry_delay)
        streams = fetch_json_list(args.streams_url, args.timeout, args.retries, args.retry_delay)

        LOG.info("Loaded channels=%d, feeds=%d, streams=%d", len(channels), len(feeds), len(streams))
        main_entries = build_entries(channels, feeds, streams, args.langs)
        extended_entries = build_entries(channels, feeds, streams, args.extended_langs)
        if not main_entries:
            raise RuntimeError("No playable channels found for LANGS={!r}".format(args.langs))
        if not extended_entries:
            raise RuntimeError("No playable channels found for EXTENDED_LANGS={!r}".format(args.extended_langs))

        main_counts = write_profile(main_entries, output_dir)
        extended_counts = write_profile(extended_entries, output_dir, prefix="extended")
        LOG.info("Main playlist: %d streams; files: %s", len(main_entries), main_counts)
        LOG.info("Extended playlist: %d streams; files: %s", len(extended_entries), extended_counts)
    except Exception as exc:
        LOG.error("Playlist generation failed: %s", exc)
        return 1

    LOG.info("All Nexa playlists were generated and validated successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
