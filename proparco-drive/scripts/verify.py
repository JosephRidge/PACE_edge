#!/usr/bin/env python3
"""
verify.py - check model output against its sources, outside the model ("generate,
verify, gate"). Used by generate.py; no AI involved.

Checks, per claim:
  citation   cited block numbers exist in the batch
  number     every number in the output appears in the cited source text
             (25 MUSD, $25 million and 25,000,000 all count as 25)
  name       every capitalised name in the output appears in the source text
             (accent- and case-insensitive; loose enough for Tanzanie/Tanzania)
  format     plain text, no markdown, history entry at most 2 sentences / 60 words

Number, name and citation failures set "review": True; format notes are informational
(the text is cleaned automatically). A failed check never deletes anything. It marks the email "needs_review" with the
exact claim that failed, so a person decides.
"""
import re
import unicodedata

# Words that may appear capitalised in English output without being in the source.
ALLOWED_WORDS = {
    # domain terms and abbreviations
    "proparco", "afd", "client", "clients", "usd", "eur", "musd", "meur", "kes", "tzs", "ugx",
    "esg", "nda", "ceo", "cfo", "sme", "smes", "loi", "mou", "ifc", "dfi", "dfis", "crm",
    "board", "committee", "credit", "investment", "project", "bank", "group", "team",
    "not", "stated", "new", "update", "awaiting", "email", "emails", "proparco's",
    # calendar words
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "q1", "q2", "q3", "q4",
    # common English words that start sentences or clauses after punctuation
    "the", "this", "that", "these", "those", "a", "an", "it", "its", "they", "he", "she",
    "we", "i", "in", "on", "at", "for", "to", "of", "and", "or", "but", "as", "after",
    "before", "following", "regarding", "per", "via", "no", "yes", "all", "both",
}

NUMBER_RE = re.compile(r"\d+(?:[.,\u202f\u00a0 ]\d{3})*(?:[.,]\d+)?")
WORD_RE = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)
MARKDOWN_RE = re.compile(r"(\*\*|__|`|^#+\s|^\s*[-*]\s)", re.M)


def strip_markdown(text):
    """Remove formatting the model may add; Notion fields are plain text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = text.replace("`", "").replace("**", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    return re.sub(r"[ \t]+", " ", text).strip()


def _norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _numbers(text):
    """Every number as plain digits, with separators removed: '25 000' -> '25000',
    '33.6' -> '33.6' and also its parts '33', '6'."""
    out = set()
    for m in NUMBER_RE.finditer(text or ""):
        raw = m.group(0)
        compact = re.sub(r"[\u202f\u00a0 ]", "", raw)
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", compact):   # 25,000,000 or 25.000.000
            out.add(re.sub(r"[.,]", "", compact))
            continue
        out.add(compact.replace(",", "."))
        out.update(re.findall(r"\d+", compact))          # 33.6 -> also 33 and 6
    return out


def _number_grounded(n, source_numbers):
    if n in source_numbers:
        return True
    try:
        value = float(n)
    except ValueError:
        return False
    for scale in (1e3, 1e6, 1e9):             # 25000000 vs "25 M"
        scaled = value / scale
        if scaled >= 1 and scaled == int(scaled) and str(int(scaled)) in source_numbers:
            return True
    return False


def _looks_like_name_start(words):
    """The first word of a sentence is usually an ordinary word ("Forwarded", "Signing"),
    so it is only checked when it looks like a name: ALL CAPS, hyphenated (Jean-Luc),
    or followed by another capitalised word (Anna Weber)."""
    w = words[0]
    return ((w.isupper() and len(w) > 2) or
            bool(re.fullmatch(r"[^\W\d_]+(-[^\W\d_]+)+", w) and w[0].isupper()) or
            (len(words) > 1 and words[1][0].isupper()))


def _names(text):
    """Capitalised words that are likely names."""
    names = []
    for sentence in re.split(r"(?<=[.!?:;])\s+|\n", text or ""):
        words = WORD_RE.findall(sentence)
        if not words:
            continue
        candidates = words if _looks_like_name_start(words) else words[1:]
        for w in candidates:
            if w[0].isupper() and _norm(w).strip("'’") not in ALLOWED_WORDS and len(w) > 1:
                names.append(w)
    return names


def _name_grounded(word, source_words):
    w = _norm(word).strip("'’s")
    if len(w) <= 2:
        return True
    key = w[:5] if len(w) >= 5 else w
    return any(sw.startswith(key) for sw in source_words)


def check_claim(field, text, source_text):
    """Return a list of flags for one generated text against its source text."""
    flags = []
    if not text:
        return flags
    src_numbers = _numbers(source_text)
    for n in _numbers(text):
        if not _number_grounded(n, src_numbers):
            flags.append({"field": field, "check": "number", "claim": n, "review": True})
    src_words = {_norm(w) for w in WORD_RE.findall(source_text or "")}
    seen = set()
    for name in _names(text):
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        if not _name_grounded(name, src_words):
            flags.append({"field": field, "check": "name", "claim": name, "review": True})
    return flags


def check_format(field, raw_text, max_sentences=None, max_words=None):
    flags = []
    if raw_text and MARKDOWN_RE.search(raw_text):
        flags.append({"field": field, "check": "format", "claim": "markdown removed"})
    clean = strip_markdown(raw_text)
    if max_sentences:
        n = len([s for s in re.split(r"(?<=[.!?])\s+", clean) if s.strip()])
        if n > max_sentences:
            flags.append({"field": field, "check": "format", "claim": f"{n} sentences"})
    if max_words and len(clean.split()) > max_words:
        flags.append({"field": field, "check": "format",
                      "claim": f"{len(clean.split())} words"})
    return flags


def check_citations(field, cited, n_blocks):
    bad = [c for c in (cited or []) if not isinstance(c, int) or not 1 <= c <= n_blocks]
    flags = [{"field": field, "check": "citation", "claim": f"block {c} does not exist",
              "review": True} for c in bad]
    if cited is not None and not cited:
        flags.append({"field": field, "check": "citation", "claim": "no source cited",
                      "review": True})
    return flags