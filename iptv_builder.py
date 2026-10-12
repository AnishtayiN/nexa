#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd
import requests

LOG = logging.getLogger("nexa")

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0

# --- URLهای پیش‌فرض (اگر env خالی باشد از این‌ها استفاده می‌شود) ---
FALLBACK_FEEDS_URL = "https://raw.githubusercontent.com/itsyebekhe/PSG/main/feeds.csv"
FALLBACK_STREAMS_URL = "https://raw.githubusercontent.com/itsyebekhe/PSG/main/streams.json"

def env_or_default(name: str, default: str) -> str:
    """اگر متغیر محیطی وجود داشت و خالی نبود، همان را برگردان؛ وگرنه پیش‌فرض."""
    val = os.environ.get(name, "").strip()
    return val if val else default

DEFAULT_FEEDS_URL = env_or_default("FEEDS_URL", FALLBACK_FEEDS_URL)
DEFAULT_STREAMS_URL = env_or_default("STREAMS_URL", FALLBACK_STREAMS_URL)
DEFAULT_OUTPUT_DIR = env_or_default("OUTPUT_DIR", "output")
DEFAULT_LANGS = env_or_default("LANGS", "")
DEFAULT_LOG_LEVEL = env_or_default("LOG_LEVEL", "INFO")

def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

def fetch_url(url: str, timeout: int, retries: int, retry_delay: float) -> requests.Response:
    if not url or not url.strip():
        raise ValueError("URL خالی است. مقدار FEEDS_URL یا STREAMS_URL را تنظیم کن.")
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            LOG.info("دریافت %s (تلاش %d/%d)", url, attempt, retries)
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            LOG.warning("خطا در دریافت %s: %s", url, exc)
            if attempt < retries:
                time.sleep(retry_delay * attempt)
    raise RuntimeError(f"دریافت {url} پس از {retries} تلاش ناموفق بود: {last_exc}") from last_exc

def fetch_dataframe(
    url: str,
    data_type: str,
    timeout: int,
    retries: int,
    retry_delay: float,
) -> pd.DataFrame:
    resp = fetch_url(url, timeout, retries, retry_delay)
    try:
        if data_type == "csv":
            return pd.read_csv(io.StringIO(resp.text))
        elif data_type == "json":
            data = resp.json()
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    data = data["data"]
                else:
                    data = [data]
            return pd.DataFrame(data)
        else:
            raise ValueError(f"نوع داده ناشناخته: {data_type}")
    except Exception as exc:
        raise RuntimeError(f"تبدیل پاسخ {url} به DataFrame ناموفق بود: {exc}") from exc

def normalize_channel_column(df: pd.DataFrame) -> pd.DataFrame:
    if "channel" not in df.columns:
        raise KeyError("ستون 'channel' در DataFrame وجود ندارد.")
    df = df.copy()
    df["channel"] = df["channel"].astype(str).str.strip()
    return df

def filter_languages(df: pd.DataFrame, langs: Optional[Sequence[str]]) -> pd.DataFrame:
    if not langs:
        return df
    if "language" not in df.columns:
        LOG.warning("ستون 'language' وجود ندارد؛ فیلتر زبان اعمال نمی‌شود.")
        return df
    lang_set = {l.strip().lower() for l in langs if l.strip()}
    mask = df["language"].astype(str).str.lower().isin(lang_set)
    return df[mask].copy()

def get_first_existing(row: pd.Series, candidates: Iterable[str], default: str = "") -> str:
    for col in candidates:
        if col in row.index:
            val = row[col]
            if pd.notna(val) and str(val).strip():
                return str(val).strip()
    return default

def build_m3u_lines(df: pd.DataFrame) -> list:
    lines = ["#EXTM3U"]
    for _, row in df.iterrows():
        name = get_first_existing(
            row,
            ["name", "channel", "title", "name_feed", "name_stream", "channel_feed", "channel_stream"],
            "Unknown",
        )
        tvg_id = get_first_existing(
            row,
            ["tvg_id", "tvg-id", "id", "tvg_id_feed", "tvg_id_stream"],
            "",
        )
        tvg_logo = get_first_existing(
            row,
            ["tvg_logo", "tvg-logo", "logo", "tvg_logo_feed", "tvg_logo_stream"],
            "",
        )
        group = get_first_existing(
            row,
            ["group_title", "group-title", "group", "category", "group_feed", "group_stream"],
            "",
        )
        url = get_first_existing(
            row,
            ["url_stream", "stream_url", "url", "link", "stream", "url_feed"],
            "",
        )
        if not url:
            LOG.warning("ردیف بدون URL رد شد: %s", name)
            continue

        attrs = []
        if tvg_id:
            attrs.append(f'tvg-id="{tvg_id}"')
        if tvg_logo:
            attrs.append(f'tvg-logo="{tvg_logo}"')
        if group:
            attrs.append(f'group-title="{group}"')
        attr_str = " " + " ".join(attrs) if attrs else ""

        lines.append(f"#EXTINF:-1{attr_str},{name}")
        lines.append(url)

    return lines

def validate_m3u(path: Path) -> bool:
    if not path.exists():
        LOG.error("فایل خروجی ساخته نشد: %s", path)
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    if "#EXTM3U" not in text:
        LOG.error("فایل %s هدر #EXTM3U ندارد.", path)
        return False
    if "#EXTINF" not in text:
        LOG.error("فایل %s هیچ کانال معتبری ندارد.", path)
        return False
    return True

def write_playlist(df: pd.DataFrame, path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = build_m3u_lines(df)
    if len(lines) <= 1:
        LOG.error("هیچ کانال معتبری برای نوشتن در %s وجود ندارد.", path)
        return False
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOG.info("نوشته شد: %s (%d خط)", path, len(lines))
    return validate_m3u(path)

def build_playlists(
    feeds_df: pd.DataFrame,
    streams_df: pd.DataFrame,
    langs: Optional[Sequence[str]],
    output_dir: Path,
) -> bool:
    feeds_df = normalize_channel_column(feeds_df)
    streams_df = normalize_channel_column(streams_df)

    feeds_df = filter_languages(feeds_df, langs)

    if "language" in feeds_df.columns and "language" in streams_df.columns:
        streams_df = streams_df.drop(columns=["language"])

    merged = pd.merge(
        feeds_df,
        streams_df,
        on="channel",
        how="inner",
        suffixes=("_feed", "_stream"),
    )

    if merged.empty:
        LOG.error("پس از ادغام، هیچ کانالی پیدا نشد. نوع ستون channel یا فیلتر زبان را بررسی کنید.")
        return False

    ok = True
    all_path = output_dir / "playlist.m3u"
    ok = write_playlist(merged, all_path) and ok

    return ok

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="سازنده پلی‌لیست IPTV برای Nexa")
    parser.add_argument("--feeds-url", default=DEFAULT_FEEDS_URL, help="آدرس feeds.csv")
    parser.add_argument("--streams-url", default=DEFAULT_STREAMS_URL, help="آدرس streams.json")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="پوشه خروجی")
    parser.add_argument("--langs", default=DEFAULT_LANGS, help="کدهای زبان جدا شده با کاما")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY)
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser.parse_args(argv)

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)

    LOG.info("FEEDS_URL=%s", args.feeds_url)
    LOG.info("STREAMS_URL=%s", args.streams_url)
    LOG.info("OUTPUT_DIR=%s", args.output_dir)
    LOG.info("LANGS=%s", args.langs or "(همه)")

    langs = [x.strip() for x in args.langs.split(",") if x.strip()] if args.langs else None

    try:
        feeds_df = fetch_dataframe(
            args.feeds_url, "csv", args.timeout, args.retries, args.retry_delay
        )
        streams_df = fetch_dataframe(
            args.streams_url, "json", args.timeout, args.retries, args.retry_delay
        )
    except Exception as exc:
        LOG.error("خطا در دریافت داده‌ها: %s", exc)
        return 1

    output_dir = Path(args.output_dir)
    success = build_playlists(feeds_df, streams_df, langs, output_dir)

    if not success:
        LOG.error("ساخت پلی‌لیست با خطا مواجه شد.")
        return 1

    LOG.info("تمام پلی‌لیست‌ها با موفقیت ساخته شدند.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
