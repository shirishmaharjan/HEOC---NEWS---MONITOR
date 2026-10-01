# HEOC News Monitor

Automates the manual news search done at the Health Emergency Operation Centre (HEOC).
It scans national and local news sources for health-related keywords (English and Nepali),
saves the matching articles, and produces a `meta.csv` with source, title, date, time and link.

## Features
- Sources can be RSS feeds or HTML listing pages, editable in the UI
- Keyword list in English and Nepali, editable in the UI
- Searches titles, summaries and full article text
- Skips links already found in earlier runs
- Output per run: `runs/<timestamp>/meta.csv` and `runs/<timestamp>/articles/*.txt`
- Streamlit web interface, plus a command-line mode for scheduled runs

## Install
```bash
git clone https://github.com/<your-username>/heoc-news-monitor.git
cd heoc-news-monitor
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux / macOS
pip install -r requirements.txt
```

## Run the web interface
```bash
streamlit run app.py
```
1. Edit sources and keywords, then click **Save settings**.
2. Click **Run scan**.
3. Download the meta CSV or the zip of all articles.

## Run automatically (recommended)
The command-line mode needs no browser:
```bash
python app.py --cli
```
- **Windows:** Task Scheduler > Create Basic Task > run `python app.py --cli` at 07:00, 12:00 and 17:00.
- **Linux:** `crontab -e` and add `0 7,12,17 * * * cd /path/to/heoc-news-monitor && venv/bin/python app.py --cli`

## Configuration
Settings are saved in `heoc_config.json`:

| Setting | Meaning |
|---|---|
| `sources` | list of `name`, `url`, `type` (`rss` or `html`), `link_contains` |
| `keywords` | words to search for (English and Nepali) |
| `days_back` | only keep articles from the last N days |
| `fetch_full_text` | open each article and search the full text |
| `max_per_source` | maximum articles checked per source per run |
| `delay_sec` | pause between requests |
| `skip_seen` | skip links found in earlier runs |

## Responsible use
- Keep the scan frequency modest and the delay between requests above 1 second.
- Check each website's terms of use and `robots.txt`.
- The tool is a first filter. Results should be reviewed by a researcher.

## Limitations
- Websites that load content with JavaScript may not work in `html` mode.
- Social media is not covered.
- Keyword matching can produce false positives.

## Ideas for next steps
Email/Telegram alerts, keyword priority tiers, district tagging, a page to browse previous runs.
