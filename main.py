"""
Daily Telegram curator: fetches top YouTube videos on AI, Psychology, and Growth,
curates the best 3 per category using Groq LLM, and posts to Telegram.

Runs daily at 7 AM IST via GitHub Actions.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from groq import Groq
from isodate import parse_duration

# ---------- Configuration ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GROQ_MODEL = "llama-3.3-70b-versatile"
SEEN_FILE = Path("seen_videos.json")
SEEN_RETENTION_DAYS = 30
MAX_DURATION_SECONDS = 600  # 10 minutes
MIN_LIKE_RATIO = 0.02       # 2% likes/views — relaxed; YouTube hides like counts often
LOOKBACK_DAYS = 7           # search videos from last 7 days

CATEGORIES = {
    "AI Usage": {
        "emoji": "🤖",
        "tagline": "Best tips & real-world use cases",
        "queries": [
            "AI productivity tips",
            "ChatGPT use cases",
            "Claude AI tutorial",
            "AI workflow automation",
            "prompt engineering tips",
        ],
    },
    "Human Psychology": {
        "emoji": "🧠",
        "tagline": "Behavioral science & how the mind works",
        "queries": [
            "human psychology insights",
            "cognitive bias explained",
            "behavioral science",
            "decision making psychology",
            "how the brain works",
        ],
    },
    "Personal Growth": {
        "emoji": "🌱",
        "tagline": "Habits, mindset, and self-improvement",
        "queries": [
            "personal growth habits",
            "self improvement tips",
            "discipline mindset",
            "productivity habits science",
            "stoic philosophy practical",
        ],
    },
}


# ---------- Seen-videos persistence ----------

def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception as e:
        log.warning(f"Could not read seen file: {e}")
        return {}


def save_seen(seen: dict) -> None:
    # Prune anything older than retention window
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    pruned = {vid: ts for vid, ts in seen.items() if ts >= cutoff}
    SEEN_FILE.write_text(json.dumps(pruned, indent=2))


# ---------- YouTube fetching ----------

def youtube_search(query: str, max_results: int = 10) -> list[str]:
    """Search YouTube and return video IDs."""
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": YOUTUBE_API_KEY,
        "q": query,
        "part": "id",
        "type": "video",
        "maxResults": max_results,
        "order": "viewCount",
        "publishedAfter": published_after,
        "relevanceLanguage": "en",
        "videoDuration": "medium",  # 4-20 min; we'll filter <10 min later
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    items = r.json().get("items", [])
    return [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]


def youtube_video_details(video_ids: list[str]) -> list[dict]:
    """Fetch details (stats, duration, title) for video IDs."""
    if not video_ids:
        return []
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "key": YOUTUBE_API_KEY,
        "id": ",".join(video_ids),
        "part": "snippet,contentDetails,statistics",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def collect_candidates(category: str, seen: dict) -> list[dict]:
    """Search across category queries, dedupe, fetch details, filter."""
    queries = CATEGORIES[category]["queries"]
    all_ids: set[str] = set()

    for q in queries:
        try:
            ids = youtube_search(q, max_results=8)
            all_ids.update(ids)
            time.sleep(0.2)  # be gentle
        except Exception as e:
            log.warning(f"Search failed for '{q}': {e}")

    # Remove already-seen
    fresh_ids = [vid for vid in all_ids if vid not in seen]
    log.info(f"[{category}] {len(all_ids)} found, {len(fresh_ids)} fresh")

    if not fresh_ids:
        return []

    # Batch fetch details (50 per call max)
    details: list[dict] = []
    for i in range(0, len(fresh_ids), 50):
        batch = fresh_ids[i:i + 50]
        try:
            details.extend(youtube_video_details(batch))
        except Exception as e:
            log.warning(f"Details fetch failed: {e}")

    # Filter by duration and engagement
    candidates = []
    for v in details:
        try:
            duration_s = parse_duration(v["contentDetails"]["duration"]).total_seconds()
            if duration_s > MAX_DURATION_SECONDS or duration_s < 60:
                continue

            stats = v.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0)) if "likeCount" in stats else 0
            if views < 1000:
                continue

            like_ratio = (likes / views) if views and likes else 0

            candidates.append({
                "id": v["id"],
                "title": v["snippet"]["title"],
                "channel": v["snippet"]["channelTitle"],
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "duration_seconds": int(duration_s),
                "views": views,
                "likes": likes,
                "like_ratio": round(like_ratio, 4),
                "published_at": v["snippet"]["publishedAt"],
                "description": v["snippet"].get("description", "")[:300],
            })
        except Exception as e:
            log.warning(f"Could not parse video: {e}")

    # Sort: prioritize like_ratio, then views
    candidates.sort(key=lambda c: (c["like_ratio"], c["views"]), reverse=True)
    return candidates[:12]  # send top 12 to LLM for final curation


# ---------- Groq curation ----------

def curate_with_groq(category: str, candidates: list[dict]) -> str:
    """Ask Groq to pick top 3 and write Telegram-ready summaries."""
    if not candidates:
        return f"_No fresh picks for {category} today — try again tomorrow._"

    cat_info = CATEGORIES[category]
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")

    candidates_json = json.dumps(
        [{
            "id": c["id"],
            "title": c["title"],
            "channel": c["channel"],
            "url": c["url"],
            "duration_min": round(c["duration_seconds"] / 60, 1),
            "views": c["views"],
            "like_ratio_pct": round(c["like_ratio"] * 100, 2),
            "description": c["description"],
        } for c in candidates],
        indent=2,
    )

    system = (
        "You are a curator for high-signal YouTube content. "
        "You select the most insightful, non-obvious videos and write punchy summaries "
        "that make people want to click. You never invent URLs or modify them."
    )

    user = f"""Below are {len(candidates)} candidate YouTube videos for the "{category}" category.

CANDIDATES (JSON):
{candidates_json}

YOUR TASK: Pick the TOP 3 videos. Then write a Telegram post in this EXACT format using Telegram MarkdownV1 (use *bold* and _italic_, no other markdown).

SELECTION RULES:
- Prefer specific, actionable, or insight-rich content over generic motivational fluff
- Prefer videos with higher like_ratio_pct (signals genuine value)
- Reject clickbait, reaction videos, or recycled listicles
- If two videos cover the same idea, pick the better one

OUTPUT FORMAT (copy this structure exactly, replace bracketed parts):

{cat_info["emoji"]} *{category} — {today}*
_{cat_info["tagline"]}_

*1. [Title — keep it short, max 60 chars]*
🎬 [Channel] · ⏱ [X.X] min · 👁 [views formatted like 12.3K or 1.2M]
[URL exactly as given]
💡 _[ONE sentence, max 22 words, that teases the insight. Create curiosity. No fluff.]_

*2. [same format]*

*3. [same format]*

🧵 _Today's thread: [ONE sentence connecting the 3 picks — what idea do they collectively reveal?]_

RULES:
- Use ONLY URLs from the candidate list. Never invent.
- Format view counts as 1.2K, 45.6K, 1.2M etc.
- The "Why" line must be specific and curiosity-driven, not "Learn about X"
- Output ONLY the post. No preamble, no explanation."""

    client = Groq(api_key=GROQ_API_KEY)
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
        max_tokens=1500,
    )
    return resp.choices[0].message.content.strip()


# ---------- Telegram ----------

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=30)
    if not r.ok:
        log.error(f"Telegram error: {r.status_code} — {r.text}")
        # Retry without markdown parsing as fallback
        payload.pop("parse_mode", None)
        r2 = requests.post(url, json=payload, timeout=30)
        r2.raise_for_status()
    else:
        log.info("Telegram message sent")


# ---------- Main pipeline ----------

def extract_video_ids(text: str) -> list[str]:
    """Pull video IDs from any youtube URLs in the message."""
    import re
    return re.findall(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", text)


def main():
    log.info("=== Daily curator starting ===")
    seen = load_seen()
    now_iso = datetime.now(timezone.utc).isoformat()
    newly_used: list[str] = []

    for category in CATEGORIES.keys():
        log.info(f"--- Processing: {category} ---")
        try:
            candidates = collect_candidates(category, seen)
            if not candidates:
                log.info(f"No candidates for {category}, skipping")
                continue

            message = curate_with_groq(category, candidates)
            send_telegram(message)
            newly_used.extend(extract_video_ids(message))
            time.sleep(2)  # spacing between Telegram sends
        except Exception as e:
            log.exception(f"Failed for {category}: {e}")
            try:
                send_telegram(f"⚠️ Curator failed for *{category}*: `{str(e)[:200]}`")
            except Exception:
                pass

    # Persist seen
    for vid in newly_used:
        seen[vid] = now_iso
    save_seen(seen)
    log.info(f"=== Done. {len(newly_used)} videos marked as seen ===")


if __name__ == "__main__":
    main()
