"""Rule-based relevance + category + city tagging. No LLM, no summaries: it only
decides whether a result is worth showing and which bucket it belongs to."""
import re
from urllib.parse import urlsplit

CITY_ALIASES = {
    "Mumbai": ["mumbai", "navi mumbai", "bombay", "thane", "powai", "bandra", "andheri", "vashi", "bkc"],
    "Pune": ["pune", "pimpri", "chinchwad", "hinjewadi", "hinjawadi", "kothrud", "baner", "viman nagar"],
}
# Other big Indian cities: used to REJECT city-bound results that clearly belong elsewhere.
OTHER_CITIES = ["bangalore", "bengaluru", "delhi", "new delhi", "hyderabad", "chennai", "kolkata",
                "ahmedabad", "noida", "gurgaon", "gurugram", "jaipur", "kochi", "indore", "lucknow",
                "chandigarh", "coimbatore", "bhubaneswar", "nagpur"]
# Words that place a job in India. Used for jobs only.
INDIA_PLACES = ([a for aliases in CITY_ALIASES.values() for a in aliases] + OTHER_CITIES + [
    "india", "indian", "bharat", "karnataka", "maharashtra", "telangana", "tamil nadu", "uttar pradesh",
    "haryana", "gujarat", "kerala", "west bengal", "rajasthan", "madhya pradesh", "odisha", "punjab"])


def _rx(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(w).replace(r"\ ", r"[\s\-]") for w in words) + r")\b", re.I)


HACK_STRONG = _rx(["hackathon", "hackathons", "hackfest", "datathon", "ideathon", "buildathon", "codeathon",
                   "hack-a-thon", "game jam", "hack week", "hackday", "hack day", "hacks"])
HACK_WEAK = _rx(["competition", "challenge", "kaggle", "contest", "codefest", "grand challenge"])
ONLINE = _rx(["online", "virtual", "remote", "work from home", "wfh", "worldwide", "global"])
GOV = _rx(["government", "govt", "ministry", "gov.in", "nic.in", "meity", "isro", "drdo", "niti aayog",
           "smart india hackathon", "digital india", "startup india", "mygov", "innovate india", "maharashtra"])
INDIA = _rx(INDIA_PLACES)
REMOTE = _rx(["remote", "work from home", "wfh", "anywhere"])

# Hackathon titles that are guides, lists, articles or listing pages, not events.
NOT_EVENT = _rx(["ideas", "idea", "tips", "guide", "how to", "examples", "example", "vs", "versus", "meaning",
                 "template", "templates", "best", "top", "list of", "news", "blog", "course", "courses",
                 "roadmap", "questions", "interview", "view all"])
# A real event page says something about taking part.
EVENT_SIGNAL = _rx(["register", "registration", "registrations", "prize", "prizes", "deadline", "apply",
                    "venue", "participants", "teams", "team", "last date", "submission", "submit",
                    "reward", "rewards", "stipend", "on-site", "onsite", "offline"])
# Full month names or standard 3-letter abbreviations only, so "decision" or "junior" do not match.
DATE_RE = re.compile(
    r"\b(20\d{2})\b|\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
    r"sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)\b", re.I)
# Generic link labels from listing pages. These are navigation, not titles.
GENERIC_LABEL = re.compile(
    r"^\s*(know more|view all|read more|learn more|click here|apply now|register now|see all|explore|"
    r"more|details|view details|view more|sign up|login)\W*$", re.I)
# Files that open a download instead of a web page.
FILE_EXT = re.compile(r"\.(pdf|zip|rar|7z|apk|exe|msi|dmg|doc|docx|xls|xlsx|ppt|pptx|csv|mp3|mp4|png|jpe?g|gif)$",
                      re.I)

AI_TOPIC = _rx(["ai", "ml", "machine learning", "artificial intelligence", "data science", "data scientist",
                "deep learning", "nlp", "computer vision", "llm", "llms", "generative ai", "genai",
                "data analyst", "data analytics", "mlops", "python developer"])
ENTRY = _rx(["fresher", "freshers", "entry level", "entry-level", "intern", "interns", "internship",
             "internships", "graduate", "graduates", "trainee", "apprentice", "campus", "junior",
             "new grad", "early career", "0-1 years", "0-2 years", "0 to 1 year", "0 to 2 years", "no experience"])
SENIOR = _rx(["senior", "sr", "lead", "principal", "staff", "manager", "director", "head of", "architect",
              "5+ years", "7+ years", "10+ years", "3+ years", "4+ years", "experienced"])

EVENT_WORDS = _rx(["meetup", "meet-up", "conference", "summit", "workshop", "webinar", "bootcamp", "devfest",
                   "conclave", "expo", "tech talk", "techfest", "tech fest", "hands-on session", "gdg",
                   "community day", "developer day", "devday", "symposium", "fest"])
TECH_TOPIC = _rx(["tech", "technology", "developer", "developers", "ai", "ml", "data", "cloud", "software",
                  "startup", "coding", "python", "devops", "web", "cyber", "cybersecurity", "open source",
                  "programming", "engineering", "gen ai", "llm", "blockchain", "iot", "robotics"])

FREE_WORDS = _rx(["free", "open source", "open-source", "no cost", "free tier", "credits", "scholarship",
                  "student", "students", "public domain", "creative commons", "gratis"])
RES_WORDS = _rx(["api", "apis", "dataset", "datasets", "model", "models", "tool", "tools", "course",
                 "courses", "cloud", "gpu", "github", "hugging face", "huggingface", "library",
                 "framework", "sdk", "notebook", "kaggle", "colab", "learning", "ai", "ml"])
PAID_ONLY = _rx(["pricing plans", "buy now", "enterprise pricing", "add to cart"])


def match_city(text, cities):
    t = text.lower()
    for city in cities:
        for alias in CITY_ALIASES.get(city, [city.lower()]):
            if re.search(r"\b" + re.escape(alias) + r"\b", t):
                return city
    return None


def _url_words(url):
    p = urlsplit(url)
    return re.sub(r"[-_/.]+", " ", f"{p.hostname or ''} {p.path}").lower()


def _score_hackathon(text, intent, cities, include_online, ctx):
    title = ctx.get("title", "")
    strong = bool(HACK_STRONG.search(title))
    weak = bool(HACK_WEAK.search(title)) and bool(TECH_TOPIC.search(title)) and intent == "hackathon"
    if not (strong or weak):
        return None  # the title itself must name a hackathon
    if NOT_EVENT.search(title) or GENERIC_LABEL.match(title or ""):
        return None
    if not (EVENT_SIGNAL.search(text) or DATE_RE.search(text)):
        return None  # no sign of an actual event
    city = match_city(text, cities)
    online = bool(ONLINE.search(text))
    gov = bool(GOV.search(text))
    other = any(re.search(r"\b" + c + r"\b", text) for c in OTHER_CITIES)
    if city:
        loc = city
    elif online and include_online:
        loc = "Online"
    elif other and not gov and not online:
        return None  # clearly a hackathon somewhere else
    else:
        loc = None
    ctx["tags"] = [t for t, on in (("government", gov), ("online", online)) if on]
    return {"city": loc, "score": 2 + int(strong) + int(bool(city)) + int(gov)}


def _score_job(text, intent, cities, include_online, ctx):
    if not AI_TOPIC.search(text) or not ENTRY.search(text):
        return None
    if SENIOR.search(text) and not re.search(r"\bintern", text):
        return None
    city = match_city(text, cities)
    india = bool(INDIA.search(text)) or ctx.get("host", "").endswith(".in")
    remote = bool(REMOTE.search(text))
    # Indian jobs only, plus remote roles from anywhere. Non-remote jobs abroad are dropped,
    # and so are jobs with no location clue at all.
    if not (city or india or remote):
        return None
    if not city:
        city = "India" if india else "Remote"
    return {"city": city, "score": 2 + int(city in cities) + int(intent == "job")}


def _score_event(text, intent, cities, include_online, ctx):
    if not EVENT_WORDS.search(text) or not TECH_TOPIC.search(text):
        return None
    city = match_city(text, cities)
    if not city:
        return None  # spec: events are Mumbai/Pune only
    return {"city": city, "score": 3 + int(intent == "event")}


def _score_resource(text, intent, cities, include_online, ctx):
    if not FREE_WORDS.search(text) or not RES_WORDS.search(text):
        return None
    if PAID_ONLY.search(text) and not re.search(r"\bfree\b", text):
        return None
    return {"city": None, "score": 2 + int(intent == "resource")}


SCORERS = {"hackathon": _score_hackathon, "event": _score_event, "job": _score_job, "resource": _score_resource}
PRIORITY = ["hackathon", "event", "job", "resource"]


def classify(title, snippet, url, intent, cities, include_online=True, min_score=2):
    """Return {category, city, score, tags} or None if the result is not relevant."""
    # Downloads and navigation labels are never shown, whatever the rest of the text says.
    if FILE_EXT.search(urlsplit(url).path or "") or GENERIC_LABEL.match(title or ""):
        return None
    text = " ".join(x for x in (title, snippet, _url_words(url)) if x).lower()
    host = (urlsplit(url).hostname or "").lower()
    best = None
    for cat in PRIORITY:
        ctx = {"title": (title or "").lower(), "host": host}
        res = SCORERS[cat](text, intent, cities, include_online, ctx)
        if not res or res["score"] < min_score:
            continue
        res.update(category=cat, tags=ctx.get("tags", []))
        # prefer the query's own intent on ties, then the fixed priority order
        rank = (res["score"] + (0.5 if cat == intent else 0))
        if best is None or rank > best[0]:
            best = (rank, res)
    return best[1] if best else None