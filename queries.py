"""Goal-driven query generation.

These are *search goals*, not sources: they describe what the user wants, never
where to find it. Which sites answer them is learned at run time.
"""
from datetime import datetime

CATEGORIES = ["hackathon", "job", "event", "resource"]


def build_pool(cities, include_online=True, now=None):
    now = now or datetime.now()
    month, year = now.strftime("%B"), now.year
    nxt = datetime(year + (now.month == 12), now.month % 12 + 1, 1).strftime("%B")
    pool = {c: [] for c in CATEGORIES}

    h = pool["hackathon"]
    for city in cities:
        h += [f"{city} hackathon {month} {year}", f"hackathon in {city} registration open {year}",
              f"{city} AI hackathon students register", f"upcoming hackathons {city} {year}"]
    if include_online:
        h += [f"online hackathon {month} {year} register", f"virtual AI hackathon {year} open to students India",
              f"global online hackathon {nxt} {year}", f"upcoming online hackathons {year}"]
    h += [f"upcoming hackathons India {year}", f"government hackathon India {year}",
          f"Smart India Hackathon {year}", f"company hosted hackathon India {year} students",
          f"AI ML competition India {year} students prize"]

    j = pool["job"]
    j += [f"AI ML fresher jobs India {year}", f"machine learning intern fresher apply {year}",
          f"entry level data science jobs freshers India", f"AI engineer internship graduates apply {year}",
          f"generative AI fresher hiring India {year}", f"NLP computer vision intern apply India {year}"]
    for city in cities:
        j += [f"machine learning intern {city} freshers", f"AI ML fresher jobs {city} {year}"]

    e = pool["event"]
    for city in cities:
        e += [f"tech events {city} {month} {year}", f"developer meetup {city} {month}",
              f"AI conference {city} {year}", f"{city} tech workshop register {year}",
              f"GDG {city} event {year}", f"{city} startup tech summit {year}"]

    r = pool["resource"]
    r += [f"free cloud credits for students developers {year}", f"free GPU for machine learning students {year}",
          f"free public APIs for developers {year}", f"open datasets for machine learning free {year}",
          f"open source AI projects beginners {year}", f"free developer tools for students",
          f"free AI models open weights download {year}", f"student developer benefits free tools credits",
          f"free open data portal India datasets", f"free courses machine learning certificate {year}"]
    return pool


def pick_queries(pool, limit, offset=0):
    """Take a balanced slice across categories. `offset` rotates the slice each run so
    repeated refreshes explore new queries instead of re-spending quota on the same ones."""
    per = max(1, limit // len(CATEGORIES))
    chosen = []
    for cat in CATEGORIES:
        qs = pool[cat]
        if not qs:
            continue
        for i in range(min(per, len(qs))):
            chosen.append((cat, qs[(offset * per + i) % len(qs)]))
    # use leftover budget on categories with more queries
    i = 0
    while len(chosen) < limit and i < 200:
        cat = CATEGORIES[i % len(CATEGORIES)]
        qs = pool[cat]
        if qs:
            cand = (cat, qs[(offset * per + per + i // len(CATEGORIES)) % len(qs)])
            if cand not in chosen:
                chosen.append(cand)
        i += 1
    return chosen[:limit]