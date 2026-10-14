"""Conservative date parsing. Only explicit dates are accepted; nothing is guessed.

Relative strings like "2 days ago" return None on purpose, because they cannot
be turned into a real date without inventing one.
"""
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

_MONTHS = ("january february march april may june july august september "
           "october november december").split()
_MON_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_ORD = r"(?:st|nd|rd|th)?"

DMY = re.compile(rf"\b(\d{{1,2}}){_ORD}\s+({_MON_RE})\.?,?\s+(\d{{4}})\b", re.I)
MDY = re.compile(rf"\b({_MON_RE})\.?\s+(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b", re.I)
ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

_MONTH_NUM = {m[:3]: i + 1 for i, m in enumerate(_MONTHS)}

DEADLINE_LABELS = re.compile(
    r"(registration[s]?\s+(?:close[s]?|end[s]?|deadline)|apply\s+(?:by|before)|last\s+date(?:\s+to\s+(?:apply|register))?"
    r"|application\s+deadline|registration\s+deadline|deadline|register\s+(?:by|before)|closes?\s+on|applications?\s+close[s]?)",
    re.I,
)


def _ymd(y, m, d):
    try:
        return datetime(int(y), int(m), int(d)).date().isoformat()
    except ValueError:
        return None


def parse_date(value):
    """Parse an explicit date string to YYYY-MM-DD, or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    m = ISO.search(s)
    if m:
        return _ymd(*m.groups())
    m = DMY.search(s)
    if m:
        return _ymd(m.group(3), _MONTH_NUM[m.group(2)[:3].lower()], m.group(1))
    m = MDY.search(s)
    if m:
        return _ymd(m.group(3), _MONTH_NUM[m.group(1)[:3].lower()], m.group(2))
    try:  # RFC 822 as used by RSS pubDate
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def find_labeled_date(text, window=60):
    """Find 'deadline: 25 Oct 2026' style statements. Requires an explicit year."""
    if not text:
        return None
    for lab in DEADLINE_LABELS.finditer(text):
        chunk = text[lab.end(): lab.end() + window]
        for rx in (DMY, MDY, ISO):
            m = rx.search(chunk)
            if m:
                return parse_date(m.group(0))
    return None
