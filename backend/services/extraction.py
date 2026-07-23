"""
Turns a raw speech transcript (or, on the S2S branch, an already-extracted
value the model handed to confirm_slot) into a clean slot value.

Every extractor here is a plain function - regex and string rules, no
network call, no LLM. It can never hallucinate a price or plan name
because prices/plans are never run through it at all - see
dialogue/state_machine.py and services/plan_matcher.py.
"""
import difflib
import re

FILLER_PREFIXES = [
    r"^(my (first |last )?name is|it'?s|it is|i'?m|i am|call me|"
    r"you can call me|i go by|this is|the name'?s|people call me)\s+",
]
SPELLING_HINT_RE = re.compile(
    r"\bwith\s+(?:a |an )?([a-z])(?:\s+instead of\s+(?:a |an )?([a-z]))?\b",
    re.I,
)
INLINE_SPELLING_HINT_RE = re.compile(
    r"\b([a-z]+)\s+with\s+(?:a |an )?([a-z])(?:\s+instead of\s+(?:a |an )?([a-z]))?\b",
    re.I,
)
EMBEDDED_SPELLING_HINT_RE = re.compile(
    r"\b([a-z]+?)with(?:a|an)?([a-z])(?:insteadof([a-z]))?\b",
    re.I,
)

# Plain zero-nine words, used for the email local-part (letters dominate
# there, so we keep this list conservative to avoid mangling real words).
DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

# Wider net for phone/zip, which are pure-digit contexts so aggressive
# homophone correction is safe: Whisper commonly mishears these as
# ordinary words ("for" instead of "four", "to"/"too" instead of "two").
ONES_WORDS = {
    **DIGIT_WORDS,
    "won": "1", "to": "2", "too": "2", "for": "4", "fore": "4", "ate": "8",
    "nil": "0", "niner": "9",
}
TEEN_WORDS = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
}
TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
MULTIPLIER_WORDS = {"double": 2, "triple": 3, "treble": 3}


def _collapse_spelled_letter_runs(text: str) -> str:
    """Turns 's-h-a-r-i-q' or 's h a r i q' into 'shariq'.

    Whisper often transcribes carefully spelled names/emails as individual
    letters separated by hyphens, spaces, or commas. Collapse only 3+ letter
    runs so normal short words like 'a' and email suffixes like 'm2004s' are
    not touched.
    """
    pattern = re.compile(r"(?<![a-z])(?:[a-z][\s,.-]+){2,}[a-z](?![a-z])", re.I)

    def repl(match):
        return re.sub(r"[^a-z]", "", match.group(0), flags=re.I)

    return pattern.sub(repl, text)


def _correct_token_with_hint(token: str, new_letter: str, old_letter: str = "") -> str:
    low = token.lower()
    if old_letter and old_letter in low:
        idx = low.rfind(old_letter)
        return token[:idx] + new_letter + token[idx + 1:]
    if new_letter == "q":
        if low.endswith(("k", "c")):
            return token[:-1] + "q"
        if not low.endswith("q"):
            return token + "q"
    return token


def _normalize_inline_spelling_hints(text: str) -> str:
    """Turns nearby correction phrases into corrected tokens.

    Examples:
    - "Sharik with a Q Mukadam" -> "Shariq Mukadam"
    - "sharikwithaq.mukadam" -> "shariq.mukadam"
    """
    def embedded(match):
        return _correct_token_with_hint(
            match.group(1),
            match.group(2).lower(),
            (match.group(3) or "").lower(),
        )

    def inline(match):
        # If the hint is at the very end after multiple words (e.g.
        # "sharik mukadam with q instead of k"), leave it for the global
        # spelling-hint pass, which can choose the first likely misspelled
        # token. Inline handling is for nearby forms like
        # "Sharik with a Q Mukadam".
        if match.end() == len(text.rstrip()) and re.search(r"[a-z]", text[:match.start()].strip(), re.I):
            return match.group(0)
        return _correct_token_with_hint(
            match.group(1),
            match.group(2).lower(),
            (match.group(3) or "").lower(),
        )

    text = EMBEDDED_SPELLING_HINT_RE.sub(embedded, text)
    return INLINE_SPELLING_HINT_RE.sub(inline, text)


def _apply_spelling_hint(value: str, original: str) -> str:
    """Applies trailing phrases like 'with q instead of k'."""
    match = SPELLING_HINT_RE.search(original)
    if not match:
        return value

    new_letter = match.group(1).lower()
    old_letter = (match.group(2) or "").lower()

    if "@" in value:
        local, domain = value.split("@", 1)
        first, sep, rest = local.partition(".")
        corrected_first = _apply_spelling_hint(first, original)
        return f"{corrected_first}{sep}{rest}@{domain}"

    parts = re.split(r"([.\s_-]+)", value)
    word_indexes = [i for i, part in enumerate(parts) if re.search(r"[a-z]", part, re.I)]
    if old_letter:
        for i in word_indexes:
            if parts[i].lower().endswith(old_letter):
                parts[i] = _correct_token_with_hint(parts[i], new_letter, old_letter)
                return "".join(parts)
        for i in word_indexes:
            if old_letter in parts[i].lower():
                parts[i] = _correct_token_with_hint(parts[i], new_letter, old_letter)
                return "".join(parts)
    elif new_letter == "q":
        for i in word_indexes:
            if parts[i].lower().endswith(("k", "c")):
                parts[i] = _correct_token_with_hint(parts[i], new_letter)
                return "".join(parts)
    return value


def clean_name(text: str) -> str:
    original = text.strip().lower()
    low = _collapse_spelled_letter_runs(original)
    for pat in FILLER_PREFIXES:
        low = re.sub(pat, "", low)
    low = _normalize_inline_spelling_hints(low)
    low = SPELLING_HINT_RE.sub("", low)
    low = re.sub(r"\s+", " ", low).strip(" .,!")
    low = _apply_spelling_hint(low, original)
    return low.title() if low else ""


def extract_digits(text: str) -> str:
    """Pulls a run of digits out of natural speech for phone/zip fields.
    Handles plain digits, misheard homophones ('for' -> 4), teens/tens
    ('twenty three' -> 23), and 'double'/'triple' prefixes ('double
    seven' -> 77)."""
    tokens = re.findall(r"[a-z]+|\d+", text.lower())
    digits = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.isdigit():
            digits.append(tok)
            i += 1
            continue
        if tok in MULTIPLIER_WORDS and i + 1 < n:
            nxt = tokens[i + 1]
            one = nxt if nxt.isdigit() and len(nxt) == 1 else ONES_WORDS.get(nxt)
            if one:
                digits.append(one * MULTIPLIER_WORDS[tok])
                i += 2
                continue
        if tok in TENS_WORDS:
            nxt = tokens[i + 1] if i + 1 < n else None
            if nxt in ONES_WORDS and ONES_WORDS[nxt] != "0":
                digits.append(str(TENS_WORDS[tok] + int(ONES_WORDS[nxt])))
                i += 2
                continue
            digits.append(str(TENS_WORDS[tok]))
            i += 1
            continue
        if tok in TEEN_WORDS:
            digits.append(TEEN_WORDS[tok])
            i += 1
            continue
        if tok in ONES_WORDS:
            digits.append(ONES_WORDS[tok])
            i += 1
            continue
        i += 1
    return "".join(digits)


def clean_phone(text: str):
    digits = extract_digits(text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def clean_zip(text: str):
    # BUG FIX: 4-10 was wide enough to silently accept a doubled-up
    # repeat (caller re-says their zip after a stall, both attempts
    # concatenate into one 10-digit run) with zero rejection/re-prompt.
    # Valid lengths: 5 or 9 (US ZIP/ZIP+4), 6 (India PIN code).
    digits = extract_digits(text)
    return digits if len(digits) in (5, 6, 9) else None


# Common ways people (especially outside the US) say "@", plus a few
# provider names Whisper tends to mangle.
_EMAIL_DOMAIN_FIXES = {
    r"\bjee ?mail\b": "gmail", r"\bg ?mail\b": "gmail", r"\bgee mail\b": "gmail",
    r"\bhot ?mail\b": "hotmail", r"\bout ?look\b": "outlook", r"\byahoo mail\b": "yahoo",
}


def clean_email(text: str):
    original = text.lower().strip()
    t = _collapse_spelled_letter_runs(original)
    at_split = re.split(r"(\bat the rate\b|\bat da rate\b|\bat the red\b|\bat rate\b|\bat\b|@)", t, maxsplit=1)
    if len(at_split) >= 3:
        t = _normalize_inline_spelling_hints(at_split[0]) + "".join(at_split[1:])
    else:
        t = _normalize_inline_spelling_hints(t)
    t = re.sub(r"\bmy email is\b|\bemail is\b", "", t)
    t = re.sub(r"[,]+", " ", t)
    t = SPELLING_HINT_RE.sub("", t)
    for word, digit in DIGIT_WORDS.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    t = re.sub(r"\bat the rate\b|\bat da rate\b|\bat the red\b|\bat rate\b", "@", t)  # common Indian-English idiom for "@"
    t = re.sub(r"\bat\b", "@", t)
    t = re.sub(r"\bdot\b", ".", t)
    t = re.sub(r"\bunderscore\b", "_", t)
    t = re.sub(r"\bdash\b|\bhyphen\b", "-", t)
    t = re.sub(r"\bplus\b", "+", t)
    for pat, fix in _EMAIL_DOMAIN_FIXES.items():
        t = re.sub(pat, fix, t)
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"\s*\.\s*", ".", t)
    t = re.sub(r"\s+", "", t)
    t = t.strip(".,")
    t = _apply_spelling_hint(t, original)
    return t if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", t) else None


ORDINAL_WORDS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}


def _words_to_numbers(t: str) -> str:
    """Normalizes spoken numbers to digits so choice options (labeled
    with digits, e.g. '2.5 Ton') match regardless of whether the caller
    said the number as a word: 'two and a half ton' -> '2.5 ton'."""
    ones = {k: v for k, v in DIGIT_WORDS.items() if k not in ("zero", "oh")}

    def half(m):
        word = m.group(1)
        return f"{ones.get(word, word)}.5"

    t = re.sub(r"\b(\w+) and (?:a )?half\b", half, t)

    def compound(m):
        return str(TENS_WORDS[m.group(1)] + int(ones[m.group(2)]))

    t = re.sub(r"\b(twenty|thirty|forty|fifty)[\s-](one|two|three|four|five|six|seven|eight|nine)\b", compound, t)
    for word, val in TEEN_WORDS.items():
        t = re.sub(rf"\b{word}\b", val, t)
    for word, val in TENS_WORDS.items():
        t = re.sub(rf"\b{word}\b", str(val), t)
    for word, val in ones.items():
        t = re.sub(rf"\b{word}\b", val, t)
    return t


_STOPWORDS = {
    "the", "a", "an", "is", "its", "it's", "im", "i'm", "i", "want", "need",
    "think", "with", "for", "and", "or", "of", "to", "just", "please", "um",
    "uh", "that", "this", "one", "looking", "go", "choice", "option", "would",
    "like", "lets", "let's", "my", "system", "please",
}
# Words that appear in *every* option within a category (e.g. every tonnage
# label contains "ton") carry no distinguishing power on their own, so they're
# dropped before scoring - otherwise "ton" alone would look like a match for
# every single tonnage option instead of none of them.
_GENERIC_WORDS = {"ton", "tons"}


def _tokenize(text: str) -> set:
    return set(re.findall(r"[a-z0-9.]+", text.lower())) - _STOPWORDS


def _option_tokens(opt: dict) -> set:
    raw = f"{opt['label']} {opt['value']}".lower()
    raw = re.sub(r"\$[\d,.]+.*", "", raw)  # plan labels carry a price - not part of what's "said"
    raw = raw.replace("_", " ").replace("-", " ")
    return _tokenize(raw) - _GENERIC_WORDS


def _fuzzy_token_overlap(said: set, option_tokens: set) -> int:
    """Counts option tokens found in what was said, allowing prefix matches
    ('heat' said for an option tokenized as 'heating') so clipped or
    informally spoken words still count."""
    used, count = set(), 0
    for ot in option_tokens:
        for st in said:
            if st in used:
                continue
            if st == ot or (len(st) >= 3 and len(ot) >= 3 and (st.startswith(ot) or ot.startswith(st))):
                count += 1
                used.add(st)
                break
    return count


def match_choice(text: str, options):
    t = _words_to_numbers(text.lower())
    # Spoken option lists get answered positionally too ("the second one") -
    # check that before label matching, since it's unambiguous when present.
    for word, idx in ORDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", t) and idx < len(options):
            return options[idx]["value"]

    said = _tokenize(t)
    if said:
        for use_fuzzy in (False, True):
            scored = []
            for opt in options:
                o_tokens = _option_tokens(opt)
                if not o_tokens:
                    continue
                overlap = _fuzzy_token_overlap(said, o_tokens) if use_fuzzy else len(said & o_tokens)
                if overlap:
                    scored.append((overlap / len(o_tokens), opt))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                top_score = scored[0][0]
                tied = [opt for score, opt in scored if score == top_score]
                # Two options equally match a short/ambiguous answer (e.g.
                # "better" when both a Cooling and a Heating plan are named
                # "Better") - guessing wrong here is worse than asking again.
                return tied[0]["value"] if len(tied) == 1 else None

    # No clean token overlap at all (typos, odd phrasing) - fall back to a
    # whole-string fuzzy match as a last resort.
    best, best_score = None, 0.0
    for opt in options:
        score = difflib.SequenceMatcher(None, t, opt["label"].lower()).ratio()
        if score > best_score:
            best, best_score = opt, score
    return best["value"] if best and best_score >= 0.45 else None


def extract(stage: str, transcript: str, stage_meta: dict):
    """Dispatch to the right deterministic extractor. No LLM fallback on
    this branch (that used to be a small Groq call, gone along with the
    rest of the cascaded pipeline) - the value reaching here on the S2S
    path already came out of the model's own audio understanding, so a
    second text-LLM guess on top of it isn't needed. If the regex/fuzzy
    match fails, the caller gets None and asks the caller to repeat."""
    if not transcript:
        return None
    kind = (stage_meta or {}).get("kind", "text")

    if kind == "text":
        value = clean_name(transcript)
    elif kind == "phone":
        value = clean_phone(transcript)
    elif kind == "email":
        value = clean_email(transcript)
    elif kind == "zip":
        value = clean_zip(transcript)
    elif kind == "choice":
        value = match_choice(transcript, stage_meta["options"])
    else:
        value = None

    return value