# Opportunity Radar

A local dashboard that discovers hackathons, fresher AI/ML jobs and internships, Mumbai/Pune tech events and free
tech resources. You press **Refresh / Search**; it finds them, and every card links to the original page.
Nothing runs in the background, and the app never generates summaries.

Stack: Python, Flask, SQLite (standard library), one static HTML page. No build step.

## Run it (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env          # add at least one free search key (see below)
python app.py
```

Open http://127.0.0.1:5000. Stop with Ctrl+C. The laptop and the app must be running for a refresh to happen.

## Search keys (all free tiers, no billing attached)

| Provider | Env var | Notes |
|---|---|---|
| Tavily | `TAVILY_API_KEY` | Quota errors: HTTP 429, 432, 433 |


Add two or three so there is somewhere to fall back to. **Check each provider's current free-tier limits and terms
before relying on them, and do not attach a card or billing.** Free tiers change, and some sign-ups ask for payment
details. This is the maintenance item from the spec: review them periodically.

## How discovery works (no source list)

There is no list of websites anywhere in the code. The bot works from goals:

1. **Goals become queries.** Templates such as "{city} hackathon {month} {year}" or "free GPU credits for students"
   are filled in from your cities and today's date. Each refresh rotates through different queries so free quota is
   spent on new ground, not repeats.
2. **A search provider answers.** The first eligible provider is used; on a confirmed quota limit or a failure it
   falls back to the next.
3. **Each result is judged** by rules: category (hackathon, job, event, resource), city, and relevance. Events must
   be in your cities. Hackathons may be in your cities, online, or government/national. Jobs must be AI/ML and entry
   level, and senior roles are dropped. Irrelevant results are discarded.
4. **Sources are learned.** Every domain that appears is recorded with hits and misses. Domains that only produce
   noise are muted automatically.
5. **Pages are read politely** (robots.txt respected, rate-limited, size-capped). From a page the bot takes only
   what the page states: schema.org Event/JobPosting data, publish dates in metadata, and a clearly labelled
   "deadline: 20 November 2026" in the text. Anything missing stays blank. A date is never guessed.
6. **Feeds are discovered, not configured.** If a good source advertises an RSS/Atom feed, the bot remembers it and
   checks it on later refreshes, which costs no search quota. This is why a refresh still brings in items when every
   provider is at its limit.
7. **Listing pages are mined.** A relevant page full of links to similar entries has those entries added too.

The only built-in domain list is `NO_FETCH_DOMAINS` in `fetch.py`: platforms whose terms forbid automated access
(LinkedIn, Naukri, Indeed and similar). Their pages are never fetched; a search API may still return a link to them.

## Provider status (API Usage panel)

- **AVAILABLE**: requests are working.
- **LIMIT REACHED**: the provider itself confirmed a quota or rate limit. It is not used again until its reported
  reset time passes, or you press **Recheck now**.
- **ERROR**: key rejected, server error, timeout, or any other non-quota problem. Server errors and timeouts are
  retried on later refreshes. A rejected key stops being used until you fix it and recheck.
- **UNKNOWN**: nothing has confirmed the state yet.
- **ALL PROVIDERS LIMIT REACHED** is shown only when every configured provider is at a confirmed limit, and then no
  search requests are made. If providers are failing for other reasons, the banner says so instead.

Remaining quota is shown as exact only when the provider reports it (Brave does). Otherwise you can set
`<NAME>_EST_LIMIT` and `<NAME>_EST_WINDOW` in `.env` to see an estimate from this app's own request log, labelled
"estimated". With neither, it reads "Unknown".

## Using the dashboard

- **Pin / Pinned** saves an item. Pins survive restarts, and rediscovery never unpins.
- Filter by category, city and deadline; sort by newest or nearest deadline.
- "Nearest deadline" uses the deadline when one is known, otherwise the event's own date. Each is labelled
  separately on the card, and items with neither sort last.
- **Search settings** changes your cities and whether online hackathons count.
- **Back up database** writes a copy to the `backups` folder.

## Tests

```
python -m pytest
```

The suite covers URL dedupe, strict date parsing, classification, page and feed extraction, status mapping
(429 vs 5xx vs timeouts), fallback, the all-limited stop, pins, the API, robots.txt handling and an end-to-end
discovery run with fake providers.

## Known limits

- Live provider calls were not exercised during the build because no API keys were available. Provider
  request/response handling is tested against simulated responses. Run one refresh with your real keys and look at
  the API Usage panel; if a provider shows ERROR with an unexpected HTTP status, that row tells you what to fix.
- Classification is keyword rules, so some misses and some noise are expected. Muting handles repeat offenders.
- Hackathons hosted by "companies" are not detected as such, since that would need a company list. Government ones are
  tagged from wording. Everything is still shown.
- Many deadlines live in page text or images and will stay blank. That is intended.
- Search results vary day to day. Press Refresh a few times across days to let the learned sources build up.
