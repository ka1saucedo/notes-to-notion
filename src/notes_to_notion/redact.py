"""Strip credentials from note text before it goes anywhere (AI provider, Notion, logs).

Redaction runs on your machine, before any network call. It is a safety net, not a guarantee:
keep real secrets in a password manager, not your notes app.
"""
import re

# Token-shaped strings: JWT/Discord-style, common API key prefixes, card-like digit runs.
TOKEN_RE = re.compile(
    r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"
    r"|\b(?:sk|ntn|ghp|gho|xox[abp]|AKIA|pk_live|sk_live|secret)[-_][A-Za-z0-9_-]{12,}"
    r"|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"
)
# Words that only ever show up on a line that holds a credential. Any line with one is redacted:
# a `label: value` line keeps its label, anything else is dropped whole.
_STRONG = r"password|passwd|passcode|passphrase|pwd|cvv|ssn"
# Everyday words that mean a credential only when used as a label ("pin: 1234", "token = ...").
# "secret santa gift" and "spin: class at 6" are left alone.
_WEAK = r"pass|pw|pin|login|secret|token|api[ _-]?key|routing|account ?#|acct"
STRONG_RE = re.compile(rf"(?<!\w)(?:{_STRONG})(?!\w)", re.I)
KEYWORD_RE = re.compile(rf"(?<!\w)(?:{_STRONG}|{_WEAK})(?!\w)", re.I)
SEPARATOR_RE = re.compile(r"[:=]")
PASSWORD_LIKE_RE = re.compile(r"^\S{8,40}$")

REDACTED = "[REDACTED]"


def looks_like_password(line: str) -> bool:
    """A bare 8-40 char token with letters and digits plus mixed case or symbols.

    Emails, URLs and paths are not passwords. Mixed case needs >= 2 digits so product names like
    "W3Schools" survive; a symbol plus one digit is enough.
    """
    s = line.strip()
    if not PASSWORD_LIKE_RE.match(s) or "@" in s or "://" in s or s.startswith(("/", "#", "$")):
        return False
    if s.count(".") >= 2 or s.count("-") >= 2:  # domains, UUIDs, phone-ish
        return False
    digits = sum(c.isdigit() for c in s)
    has_alpha = any(c.isalpha() for c in s)
    mixed = any(c.isupper() for c in s) and any(c.islower() for c in s)
    symbol = any(not c.isalnum() for c in s)
    return has_alpha and ((digits >= 2 and mixed) or (symbol and digits >= 1))


def redact_line(line: str) -> str:
    if KEYWORD_RE.search(line) and SEPARATOR_RE.search(line):
        label = SEPARATOR_RE.split(line, maxsplit=1)[0].strip()
        return f"{label}: {REDACTED}"
    if STRONG_RE.search(line) or looks_like_password(line):
        return REDACTED
    return TOKEN_RE.sub(REDACTED, line)


def redact(text: str) -> str:
    return "\n".join(redact_line(line) for line in (text or "").splitlines())
