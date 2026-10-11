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
# Lines that announce a secret ("password: ...", "pin: 1234"). The label is kept, the value dropped.
SECRET_LINE_RE = re.compile(
    r"(password|passwd|passcode|pass:|pwd|pw:|login:|pin:|cvv|ssn|secret|api key|token|routing|account #|acct)",
    re.I,
)
PASSWORD_LIKE_RE = re.compile(r"^\S{8,40}$")

REDACTED = "[REDACTED]"


def looks_like_password(line: str) -> bool:
    """A bare 8-40 char token with letters + digits and (mixed case or symbols), not an email/URL/path.

    Mixed case needs >= 2 digits so product names like "W3Schools" survive; a symbol plus one digit is enough.
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
    if SECRET_LINE_RE.search(line):
        label = re.split(r"[:=]", line, maxsplit=1)[0].strip()
        return f"{label}: {REDACTED}"
    if looks_like_password(line):
        return REDACTED
    return TOKEN_RE.sub(REDACTED, line)


def redact(text: str) -> str:
    return "\n".join(redact_line(line) for line in (text or "").splitlines())
