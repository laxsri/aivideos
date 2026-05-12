# Daily Telegram Curator

Sends 3 curated YouTube videos to your Telegram chat every morning at **7 AM IST**, across three categories: AI Usage, Human Psychology, and Personal Growth. All videos are under 10 minutes.

## How it works

1. **YouTube Data API** searches for recent videos in each category
2. Python filters by duration (<10 min), engagement, and freshness
3. **Groq LLM** (Llama 3.3 70B) curates the top 3 per category and writes summaries
4. **Telegram Bot API** posts to your chat
5. Seen videos are tracked in `seen_videos.json` to avoid repeats for 30 days

## Setup

### 1. Get a YouTube Data API key

1. Go to https://console.cloud.google.com
2. Create a new project
3. Enable **YouTube Data API v3** in the API Library
4. Create an API key under Credentials
5. (Optional) Restrict it to YouTube Data API v3 only

### 2. Set up GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

Add these four secrets:
- `YOUTUBE_API_KEY`
- `GROQ_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 3. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/telegram-curator.git
git push -u origin main
```

### 4. Test it

In your repo → **Actions** tab → "Daily Telegram Curator" → **Run workflow**

You should receive 3 Telegram messages within ~1 minute.

## Schedule

The cron `30 1 * * *` runs at **01:30 UTC = 07:00 IST** daily.

GitHub Actions cron is best-effort and can be delayed by 5–15 minutes during high load. This is normal.

## Customization

Edit `main.py` → `CATEGORIES` dict to change topics or search queries.

## Cost

Everything is free:
- YouTube Data API: 10,000 units/day quota (you'll use ~900)
- Groq free tier: ample for 3 calls/day
- GitHub Actions: free for public repos, 2,000 min/month for private
- Telegram Bot API: free
