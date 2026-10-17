# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic local preparation and review gates for brain harvest (spec §1, §3, §5, §7, §10).

Parses a raw harvest corpus (format v1, v2, or structurally normalized v3) into a private, opaque-ID manifest, a dumb work-
partitioning clustering with a mandatory `mixed` bucket, stratified per-cluster reading plans,
a risk-marker distribution report, a reserved holdout, and a machine-verified coverage ledger.
Everything lives under one gitignored scratch root; raw subjects/filenames never leave it.

Stdlib only: run with `uv run --no-project python prepare_harvest.py` or plain `python3`.

    prepare_harvest.py preflight --scratch .replypen/harvest/<tag>
    prepare_harvest.py prepare --corpus corpus.md --scratch .replypen/harvest/<tag>
    prepare_harvest.py verify --scratch .replypen/harvest/<tag>
    prepare_harvest.py ledger apply --scratch .replypen/harvest/<tag> drafts/C01.report.json
    prepare_harvest.py review --scratch .replypen/harvest/<tag> --agent-report ... \
      --reduction reduced.json --evaluation evaluation.json --metrics metrics.json
    prepare_harvest.py record --scratch .replypen/harvest/<tag> --approved \
      --out notes/harvest-records/YYYY-MM-DD.json
    prepare_harvest.py cleanup --scratch .replypen/harvest/<tag> --yes

Determinism: manifest/clusters/ledger/holdout/replay-cases/run are byte-identical on re-run over the
same corpus bytes; opaque IDs are content-derived and stable across full/delta overlap; every
timestamp is sourced from the corpus `harvested_at`, never wall clock.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"


# All numeric knobs are tunable defaults, not spec constants; override via --config JSON.
DEFAULTS: dict[str, Any] = {
    "era_recent_months": 24,        # <= this -> recent
    "era_mid_months": 72,           # <= this -> mid; else old
    "sample_cap": 50,               # per-cluster single-pass stratified reading plan cap
    "min_cluster_size": 3,          # smaller subject-family groups fall back to mixed
    "holdout_count": 8,             # reserved eval threads (spec range 5-10)
    "holdout_min_external_chars": 200,
    "prose_reply_min_chars": 40,    # §5 presence-without-prose-reply evidence threshold
    "risk_cap": 0.15,               # flagged share above this -> report over_cap (never auto-expand)
    "deep_thread_min_messages": 6,  # reply-depth risk marker
    "seed": 0,                      # stable pseudo-random tie-break for sampling/holdout spread
}

OUTPUT_NAMES = ("threads", "manifest.jsonl", "clusters.json", "ledger.json", "holdout.json",
                "replay-cases.json", "run.json", "templates.json", "diagnostics.json", "diagnostics.md")
PRIMARY_STATUSES = ("assigned", "holdout", "excluded_noise")
READ_STATES = ("none", "sampled", "deep")
# 128-bit truncated SHA-256. It is opaque, stable across corpus ordering/subsets, and leaves enough
# collision resistance for overlap reconciliation without embedding mailbox content in the handle.
OPAQUE_ID_RE = re.compile(r"\bH[0-9a-f]{32}\b")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
SUCCESS_STATUSES = {"succeeded", "success", "completed"}

HEADER_RE = re.compile(r"^## (.*) — #(\d+)\s*$")
MSG_RE = re.compile(r"^\*\*(.+?) \((\d{4}-\d{2}-\d{2})\):\*\*\s*$")
PART_RE = re.compile(r"^\*\*Participants:\*\*\s*(.*)$")
SPAN_RE = re.compile(r"^\*\*Span:\*\*\s*(.*)$")
OCC_RE = re.compile(r"^\*\*Occurrences:\*\*\s*(\d+)\s*$")
ATT_RE = re.compile(r"^_\[attachment:\s*(.*?)\]_\s*$")
# Colon-only: a hyphen separator would false-strip real subjects ("re-order confirmation").
REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|wg|antw|antwort|ref|rép|rep|tr|sv|vs|r)\s*:\s*", re.I)
WORD_RE = re.compile(r"[a-zà-ÿ']+")
DIAGNOSTIC_FIELDS = ("accepted_count", "rejection_reasons", "actor_types", "initiation_types",
                     "candidate_bands", "deduplicated")
SAFE_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
V3_MESSAGE_ROLES = {("inbound", "contact"), ("outbound", "human_admin")}

# Generic subject families must never determine a topic cluster alone (spec §1); localized too.
GENERIC_FAMILIES = {
    "", "contact", "contact-form", "order", "orders", "invoice", "invoices", "info",
    "information", "support", "help", "question", "questions", "inquiry", "enquiry",
    "enquiries", "request", "requests", "hello", "hi", "hey", "general", "website",
    "feedback", "message", "kontakt", "kontaktformular", "contacto", "bestellung",
    "bestellungen", "commande", "commandes", "bestelling", "bestellingen", "rechnung",
    "rechnungen", "facture", "factures", "factuur", "facturen", "anfrage", "anfragen",
    "vraag", "vragen", "demande", "demandes", "aanvraag", "frage", "fragen", "hallo",
    "bonjour", "salut", "informatie", "renseignement", "renseignements",
}

FORM_RE = re.compile(
    r"contact\s*form|contactformulier|kontaktformular|via (?:the |our |your )?website|"
    r"submitted through|no[- ]?reply|do not reply|niet beantwoorden|this is an automated|"
    r"automated (?:message|notification|reply)|newsletter|unsubscribe|notification",
    re.I,
)

AUTOMATED_RE = re.compile(
    r"do not reply|do-not-reply|no[- ]?reply|automated (?:message|notification|reply)|"
    r"this (?:is|was) (?:an? )?automated|unsubscribe|delivery status notification|"
    r"out of office|vacation responder|autorepl(?:y|ies)|bounce(?:d)? message",
    re.I,
)

QUESTION_RE = re.compile(
    r"\?|\b(?:question|could you|can you|would you|would like to know|want to know|"
    r"please (?:tell|explain|confirm|advise)|how (?:can|do|should|would)|what (?:is|are|do|should)|"
    r"when (?:is|are|will|can)|where (?:is|are|can)|why (?:is|are|did)|"
    r"vraag|graag weten|kunt u|kan u|hoe |wat |wanneer |waarom |"
    r"question|voudr(?:ais|ions) savoir|pourriez-vous|comment |quel(?:le|s)? |"
    r"frage|möchte wissen|könnten sie|wie |was |wann |warum )\b",
    re.I,
)

REPLAY_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
REPLAY_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
REPLAY_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)")
REPLAY_NAME_RE = re.compile(
    r"\b((?i:dear|beste|geachte|hello|hi|hallo|bonjour)\s+)"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
)
REPLAY_IDENTIFIER_RE = re.compile(
    r"\b((?:order|invoice|tracking|account|policy|ticket|reference|case)\s*(?:id|number|no\.?|#)?"
    r"\s*[:#-]?\s*)[A-Z0-9][A-Z0-9._/-]{3,}\b",
    re.I,
)

RISK_MARKERS: dict[str, re.Pattern[str]] = {
    "payment_dispute": re.compile(r"chargeback|charge ?back|dispute|unauthori[sz]ed|fraudulent charge", re.I),
    "refund": re.compile(r"refund|money back|reimburse|credit note|terugbetal", re.I),
    "legal": re.compile(r"lawyer|attorney|solicitor|legal action|lawsuit|court|subpoena|liabilit|gdpr|data protection", re.I),
    "complaint_escalation": re.compile(r"complaint|escalat|unacceptable|furious|speak to (?:a )?manager|worst", re.I),
    "safety": re.compile(r"injur|unsafe|hazard|danger|safety|\bharm\b|defect", re.I),
    "credential_request": re.compile(r"password|api[- ]?key|access token|credential|secret key|login details", re.I),
    "policy_exception": re.compile(r"exception|one[- ]time|waive|special case|out of policy|as a courtesy", re.I),
}

STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "you", "your", "for", "with", "this", "that", "have", "are", "will",
           "please", "thanks", "thank", "our", "we", "is", "to", "of", "in", "on", "be", "not"},
    "nl": {"de", "het", "een", "en", "van", "ik", "je", "uw", "met", "voor", "dank", "groeten",
           "is", "op", "aan", "niet", "wij", "ook", "dat", "graag", "kan", "worden"},
    "fr": {"le", "la", "les", "un", "une", "et", "de", "vous", "votre", "pour", "avec", "merci",
           "bonjour", "cordialement", "est", "nous", "ne", "pas", "que", "dans", "sur"},
    "de": {"der", "die", "das", "und", "ein", "eine", "sie", "ihre", "für", "mit", "danke",
           "freundlichen", "grüßen", "ist", "wir", "nicht", "auch", "haben", "sehr", "eine"},
}


class HarvestError(RuntimeError):
    pass


def validate_config(cfg: Any, path: str = "config") -> dict[str, Any]:
    if not isinstance(cfg, dict) or set(cfg) != set(DEFAULTS):
        raise HarvestError(f"{path} must contain every preparation knob exactly once")
    integer_keys = {key for key, default in DEFAULTS.items() if isinstance(default, int)}
    for key, value in cfg.items():
        if key in integer_keys:
            if not isinstance(value, int) or isinstance(value, bool):
                raise HarvestError(f"{path}.{key} must be an integer")
        elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise HarvestError(f"{path}.{key} must be a finite number")
    for key in ("era_recent_months", "era_mid_months", "sample_cap", "holdout_count",
                "holdout_min_external_chars", "prose_reply_min_chars"):
        if cfg[key] < 0:
            raise HarvestError(f"{path}.{key} must be >= 0")
    for key in ("min_cluster_size", "deep_thread_min_messages"):
        if cfg[key] < 1:
            raise HarvestError(f"{path}.{key} must be >= 1")
    if cfg["era_mid_months"] < cfg["era_recent_months"]:
        raise HarvestError(f"{path}.era_mid_months must be >= era_recent_months")
    if not 0 <= cfg["risk_cap"] <= 1:
        raise HarvestError(f"{path}.risk_cap must be between 0 and 1")
    return cfg


@dataclass
class Message:
    role: str
    date: str | None
    body: str
    attachments: list[str]
    direction: str = ""
    actor_role: str = ""


@dataclass
class Thread:
    source_format: str
    section_index: int
    subject: str
    occurrence_index: int
    occurrences: int
    participants: list[str]
    span: list[str]
    messages: list[Message]
    raw: str
    # derived
    id: str = ""
    date_first: str | None = None
    date_last: str | None = None
    era: str = "old"
    subject_family: str = ""
    language: str = "und"
    message_count: int = 0
    mailbox_message_count: int = 0
    external_message_count: int = 0
    direction: str = "external_first"
    form_source: bool = False
    attachments: bool = False
    prose_reply: bool = False
    prose_reply_count: int = 0
    risk_markers: list[str] = field(default_factory=list)
    max_external_chars: int = 0
    automated: bool = False
    ambiguous: bool = False
    holdout_question: str = ""
    holdout_answer: str = ""
    origin_cluster: str | None = None
    cluster: str | None = None
    secondary_clusters: list[str] = field(default_factory=list)
    holdout: bool = False
    status: str = "assigned"
    read: str = "none"
    routed_to: str | None = None


# --- corpus parsing -------------------------------------------------------------------------

def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise HarvestError(
            "missing '---' front-matter fence; expected a harvest corpus (harvest_format: v1|v2|v3). "
            "Re-download with 'rp project corpus download --out <file>' and retry."
        )
    rest = text[4:]
    close = rest.find("\n---")
    if close == -1:
        raise HarvestError(
            "unterminated front-matter fence; expected a closing '---'. "
            "Re-download with 'rp project corpus download --out <file>' and retry."
        )
    front = rest[:close]
    after = rest[close + 4:]
    newline = after.find("\n")
    body = after[newline + 1:] if newline != -1 else ""
    meta: dict[str, str] = {}
    for line in front.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, body


def split_sections(body: str) -> list[str]:
    sections: list[str] = []
    for chunk in re.split(r"(?m)(?=^## )", body):
        if not chunk.startswith("## "):
            continue
        first = chunk.split("\n", 1)[0]
        if HEADER_RE.match(first):
            sections.append(chunk.rstrip("\n"))
        elif sections:
            # A stray '## ' inside a message body: rejoin (never mis-split on the \n## boundary).
            sections[-1] = sections[-1] + "\n" + chunk.rstrip("\n")
    return sections


def classify_role(token: str, fmt: str, mailbox: str) -> str:
    lowered = token.strip().lower()
    if fmt == "v2":
        return "mailbox" if lowered == "mailbox" else "external"
    return "mailbox" if mailbox and lowered == mailbox.strip().lower() else "external"


def classify_message(token: str, fmt: str, mailbox: str, section: int) -> tuple[str, str, str]:
    """Adapt provider headings to one internal role while making v3 structure mandatory."""
    if fmt != "v3":
        return classify_role(token, fmt, mailbox), "", ""
    parts = [part.strip().lower() for part in token.split("/")]
    if len(parts) != 2 or tuple(parts) not in V3_MESSAGE_ROLES:
        raise HarvestError(
            f"section {section} v3 message heading must be inbound/contact or "
            "outbound/human_admin; body/header inference is forbidden"
        )
    direction, actor_role = parts
    role = "external" if direction == "inbound" else "mailbox"
    return role, direction, actor_role


def parse_section(raw: str, fmt: str, mailbox: str, index: int) -> Thread:
    lines = raw.split("\n")
    match = HEADER_RE.match(lines[0])
    if not match:
        raise HarvestError(f"section {index} has no '## <subject> — #<n>' header")
    subject, occurrence_index = match.group(1), int(match.group(2))
    participants: list[str] = []
    span: list[str] = []
    occurrences = 1
    raw_messages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines[1:]:
        header = MSG_RE.match(line)
        if header:
            role, direction, actor_role = classify_message(header.group(1), fmt, mailbox, index)
            current = {"role": role, "direction": direction, "actor_role": actor_role,
                       "date": header.group(2), "body": [], "attachments": []}
            raw_messages.append(current)
            continue
        if current is None:
            if PART_RE.match(line):
                participants = [p.strip() for p in PART_RE.match(line).group(1).split(",") if p.strip()]
            elif SPAN_RE.match(line):
                span = [p.strip() for p in re.split(r"→|->", SPAN_RE.match(line).group(1)) if p.strip()]
            elif OCC_RE.match(line):
                occurrences = int(OCC_RE.match(line).group(1))
            continue
        attachment = ATT_RE.match(line)
        if attachment:
            current["attachments"].append(attachment.group(1).strip())
        else:
            current["body"].append(line)
    messages = [Message(role=m["role"], date=m["date"], body="\n".join(m["body"]).strip(),
                        attachments=m["attachments"], direction=m["direction"],
                        actor_role=m["actor_role"]) for m in raw_messages]
    if fmt == "v3":
        dates = [message.date for message in messages]
        if dates != sorted(dates):
            raise HarvestError(f"section {index} v3 messages must be chronological")
        accepted_pair = any(
            message.direction == "inbound" and message.actor_role == "contact"
            and any(later.direction == "outbound" and later.actor_role == "human_admin"
                    for later in messages[position + 1:])
            for position, message in enumerate(messages)
        )
        if not accepted_pair:
            raise HarvestError(
                f"section {index} v3 must contain contact inbound followed by verified human_admin outbound"
            )
    return Thread(source_format=fmt, section_index=index, subject=subject,
                  occurrence_index=occurrence_index, occurrences=occurrences,
                  participants=participants, span=span, messages=messages, raw=raw)


def parse_count_map(raw: str, field_name: str) -> dict[str, int]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarvestError(f"{field_name} must be a JSON object of category counts") from exc
    if not isinstance(value, dict):
        raise HarvestError(f"{field_name} must be a JSON object of category counts")
    result: dict[str, int] = {}
    for category, count in value.items():
        if not isinstance(category, str) or not SAFE_CATEGORY_RE.fullmatch(category):
            raise HarvestError(f"{field_name} contains unsafe category {category!r}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise HarvestError(f"{field_name}.{category} must be a non-negative integer")
        result[category] = count
    return dict(sorted(result.items()))


def parse_utc_timestamp(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HarvestError(f"{path} must be an RFC3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise HarvestError(f"{path} must be an RFC3339 UTC timestamp") from exc
    return value


def utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def parse_diagnostics(meta: dict[str, str], source: str) -> dict[str, Any] | None:
    present = [field_name for field_name in DIAGNOSTIC_FIELDS if field_name in meta]
    if not present:
        return None
    missing = sorted(set(DIAGNOSTIC_FIELDS) - set(present))
    if missing:
        raise HarvestError(f"{source} has partial harvest diagnostics; missing {missing}")

    integer_fields: dict[str, int] = {}
    for field_name in ("accepted_count", "deduplicated"):
        try:
            value = int(meta[field_name])
        except ValueError as exc:
            raise HarvestError(f"{source} {field_name} must be a non-negative integer") from exc
        if value < 0 or str(value) != meta[field_name]:
            raise HarvestError(f"{source} {field_name} must be a non-negative integer")
        integer_fields[field_name] = value

    try:
        bands_value = json.loads(meta["candidate_bands"])
    except json.JSONDecodeError as exc:
        raise HarvestError(f"{source} candidate_bands must be a JSON array") from exc
    if not isinstance(bands_value, list):
        raise HarvestError(f"{source} candidate_bands must be a JSON array")
    bands: list[dict[str, Any]] = []
    previous_end = ""
    for index, item in enumerate(bands_value):
        if not isinstance(item, dict) or set(item) != {"start", "end", "count"}:
            raise HarvestError(f"{source} candidate_bands[{index}] must contain start/end/count")
        start = parse_utc_timestamp(item["start"], f"candidate_bands[{index}].start")
        end = parse_utc_timestamp(item["end"], f"candidate_bands[{index}].end")
        if utc_datetime(start) >= utc_datetime(end):
            raise HarvestError(f"{source} candidate_bands[{index}] must be a non-empty [start,end) band")
        if previous_end and utc_datetime(start) < utc_datetime(previous_end):
            raise HarvestError(f"{source} candidate_bands must be sorted and non-overlapping")
        if not isinstance(item["count"], int) or isinstance(item["count"], bool) or item["count"] < 0:
            raise HarvestError(f"{source} candidate_bands[{index}].count must be non-negative")
        bands.append({"start": start, "end": end, "count": item["count"]})
        previous_end = end
    return {
        "available": True,
        **integer_fields,
        "rejection_reasons": parse_count_map(meta["rejection_reasons"], "rejection_reasons"),
        "actor_types": parse_count_map(meta["actor_types"], "actor_types"),
        "initiation_types": parse_count_map(meta["initiation_types"], "initiation_types"),
        "candidate_bands": bands,
    }


def combine_diagnostics(items: list[dict[str, Any] | None], accepted_fallback: int) -> dict[str, Any]:
    if not any(item is not None for item in items):
        return {"schema_version": 1, "available": False, "accepted_count": accepted_fallback,
                "rejection_reasons": {}, "actor_types": {}, "initiation_types": {},
                "candidate_bands": [], "deduplicated": 0}
    if any(item is None for item in items):
        raise HarvestError("cannot combine corpora with and without harvest diagnostics")
    present = [item for item in items if item is not None]
    combined: dict[str, Any] = {"schema_version": 1, "available": True,
                                "accepted_count": sum(item["accepted_count"] for item in present),
                                "rejection_reasons": {}, "actor_types": {}, "initiation_types": {},
                                "candidate_bands": [],
                                "deduplicated": sum(item["deduplicated"] for item in present)}
    for map_name in ("rejection_reasons", "actor_types", "initiation_types"):
        counts: Counter[str] = Counter()
        for item in present:
            counts.update(item[map_name])
        combined[map_name] = dict(sorted(counts.items()))
    combined["candidate_bands"] = sorted(
        (band for item in present for band in item["candidate_bands"]), key=lambda band: band["start"]
    )
    for previous, current in zip(combined["candidate_bands"], combined["candidate_bands"][1:]):
        if utc_datetime(current["start"]) < utc_datetime(previous["end"]):
            raise HarvestError("combined candidate_bands overlap")
    return combined


def load_threads(files: list[Path]) -> tuple[list[Thread], str, list[str], dict[str, Any]]:
    threads: list[Thread] = []
    harvested: list[str] = []
    formats: list[str] = []
    diagnostics: list[dict[str, Any] | None] = []
    accepted_fallback = 0
    index = 0
    for path in files:
        meta, body = parse_front_matter(path.read_bytes().decode("utf-8", "replace"))
        fmt = meta.get("harvest_format", "")
        if fmt not in ("v1", "v2", "v3"):
            raise HarvestError(
                f"unsupported harvest_format {fmt or '(missing)'!r} in {path.name}; this parser "
                "supports v1, v2, and v3 only. Re-download with 'rp project corpus download --out <file>' "
                "and confirm the server emitted a supported format before retrying (the 48h eviction window "
                "allows a fresh re-download)."
            )
        mailbox = meta.get("mailbox", "")
        harvested.append(meta.get("harvested_at", ""))
        formats.append(fmt)
        item_diagnostics = parse_diagnostics(meta, path.name)
        if fmt == "v3" and item_diagnostics is None:
            raise HarvestError(f"{path.name} v3 corpus must include the complete diagnostics bundle")
        diagnostics.append(item_diagnostics)
        sections = split_sections(body)
        raw_accepted = meta.get("accepted_interactions")
        if raw_accepted is None:
            accepted_fallback += len(sections)
        else:
            try:
                accepted = int(raw_accepted)
            except ValueError as exc:
                raise HarvestError(f"{path.name} accepted_interactions must be non-negative") from exc
            if accepted < 0 or str(accepted) != raw_accepted:
                raise HarvestError(f"{path.name} accepted_interactions must be non-negative")
            accepted_fallback += accepted
        for section in sections:
            threads.append(parse_section(section, fmt, mailbox, index))
            index += 1
    if not threads:
        raise HarvestError("corpus contains no threads (no '## <subject> — #<n>' sections found)")
    harvested_at = max(h for h in harvested if h) if any(harvested) else ""
    fmt_label = "+".join(sorted(set(formats)))
    return threads, harvested_at, [fmt_label], combine_diagnostics(diagnostics, accepted_fallback)


# --- metadata -------------------------------------------------------------------------------

def nonspace_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def subject_family(subject: str) -> str:
    text = subject.strip()
    if text.lower() in ("(no subject)", ""):
        return ""
    previous = None
    while text != previous:
        previous = text
        text = REPLY_PREFIX.sub("", text)
    text = text.lower()
    text = re.sub(r"#\s*\d+", " ", text)
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^a-zà-ÿ]+", " ", text)
    return "-".join(text.split()[:6])


def detect_language(text: str) -> str:
    words = WORD_RE.findall(text.lower())
    if len(words) < 3:
        return "und"
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in STOPWORDS.items()}
    ranked = sorted(scores.values(), reverse=True)
    best = max(scores, key=lambda k: (scores[k], k))
    if ranked[0] < 2 or (len(ranked) > 1 and ranked[0] == ranked[1]):
        return "und"
    return best


def detect_risk(text: str, message_count: int, cfg: dict[str, Any]) -> list[str]:
    markers = [name for name, pattern in RISK_MARKERS.items() if pattern.search(text)]
    if message_count >= cfg["deep_thread_min_messages"]:
        markers.append("deep_thread")
    return sorted(markers)


def months_between(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month) - (1 if later.day < earlier.day else 0)


def era_band(date_last: str | None, harvested: date | None, cfg: dict[str, Any]) -> str:
    if not date_last or harvested is None:
        return "old"
    try:
        months = months_between(harvested, date.fromisoformat(date_last))
    except ValueError:
        return "old"
    if months <= cfg["era_recent_months"]:
        return "recent"
    if months <= cfg["era_mid_months"]:
        return "mid"
    return "old"


def compute_metadata(thread: Thread, cfg: dict[str, Any], harvested: date | None) -> None:
    messages = thread.messages
    dates = [m.date for m in messages if m.date]
    if dates:
        thread.date_first, thread.date_last = min(dates), max(dates)
    elif thread.span:
        thread.date_first, thread.date_last = thread.span[0], thread.span[-1]
    thread.message_count = len(messages)
    thread.mailbox_message_count = sum(1 for m in messages if m.role == "mailbox")
    thread.external_message_count = sum(1 for m in messages if m.role == "external")
    thread.direction = (messages[0].role + "_first") if messages else "external_first"
    thread.attachments = any(m.attachments for m in messages)
    prose = [m for m in messages if m.role == "mailbox" and nonspace_len(m.body) >= cfg["prose_reply_min_chars"]]
    thread.prose_reply_count = len(prose)
    thread.prose_reply = bool(prose)
    thread.max_external_chars = max([nonspace_len(m.body) for m in messages if m.role == "external"] or [0])
    thread.subject_family = subject_family(thread.subject)
    corpus_text = " ".join([thread.subject] + [m.body for m in messages])
    thread.language = detect_language(corpus_text)
    thread.form_source = bool(FORM_RE.search(corpus_text))
    thread.automated = bool(AUTOMATED_RE.search(corpus_text))
    thread.risk_markers = detect_risk(corpus_text, thread.message_count, cfg)
    thread.era = era_band(thread.date_last, harvested, cfg)
    generic = not thread.subject_family or thread.subject_family in GENERIC_FAMILIES
    thread.ambiguous = bool(generic and not thread.automated and
                            (thread.prose_reply or thread.max_external_chars >=
                             cfg["prose_reply_min_chars"]))
    thread.secondary_clusters = [
        f"direction:{thread.direction}",
        f"form:{'form' if thread.form_source else 'direct'}",
        *(f"risk:{marker}" for marker in thread.risk_markers),
        *(["risk:none"] if not thread.risk_markers else []),
        *(["ambiguity:generic-subject"] if thread.ambiguous else []),
    ]
    pair = holdout_pair(thread, cfg)
    if pair:
        thread.holdout_question, thread.holdout_answer = pair


def normalized_identity_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def thread_identity(thread: Thread) -> bytes:
    """Canonical private identity: stable when the same thread appears in a full or delta export."""
    payload = {
        "subject": normalized_identity_text(thread.subject),
        "messages": [{
            "role": message.role,
            "date": message.date,
            "body": normalized_identity_text(message.body),
            "attachments": sorted(normalized_identity_text(name) for name in message.attachments),
        } for message in thread.messages],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def assign_ids(threads: list[Thread]) -> None:
    identities: dict[str, bytes] = {}
    for thread in threads:
        identity = thread_identity(thread)
        thread_id = "H" + hashlib.sha256(identity).hexdigest()[:32]
        previous = identities.get(thread_id)
        if previous is not None:
            if previous == identity:
                raise HarvestError("corpus contains duplicate indistinguishable threads; "
                                   "deduplicate the input before preparation")
            raise HarvestError("opaque thread-id collision; stop and report this corpus to support")
        identities[thread_id] = identity
        thread.id = thread_id


# --- clustering, holdout, sampling ----------------------------------------------------------

def cluster_threads(threads: list[Thread], cfg: dict[str, Any]) -> list[tuple[str, str, list[Thread]]]:
    groups: dict[str | None, list[Thread]] = defaultdict(list)
    for thread in threads:
        family = thread.subject_family
        key = None if (not family or family in GENERIC_FAMILIES) else family
        groups[key].append(thread)
    mixed = list(groups.pop(None, []))
    proper = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    clusters: list[tuple[str, str, list[Thread]]] = []
    number = 0
    for family, members in proper:
        if len(members) >= cfg["min_cluster_size"]:
            number += 1
            clusters.append((f"C{number:02d}", family, members))
        else:
            mixed.extend(members)
    clusters.append(("mixed", "mixed", mixed))  # mandatory bucket, always present
    for cluster_id, _, members in clusters:
        for thread in members:
            thread.origin_cluster = cluster_id
    return clusters


def sanitize_replay_text(value: str) -> str:
    lines = [line for line in value.splitlines() if not line.lstrip().startswith(">")]
    text = " ".join(" ".join(lines).split())
    text = REPLAY_EMAIL_RE.sub("[email]", text)
    text = REPLAY_URL_RE.sub("[link]", text)
    text = REPLAY_PHONE_RE.sub("[phone]", text)
    text = REPLAY_NAME_RE.sub(lambda match: match.group(1) + "[name]", text)
    text = REPLAY_IDENTIFIER_RE.sub(lambda match: match.group(1) + "[identifier]", text)
    return text[:4000].strip()


def holdout_pair(thread: Thread, cfg: dict[str, Any]) -> tuple[str, str] | None:
    """Return a substantive inbound question and the first later human prose answer."""
    if not thread.messages or thread.direction != "external_first" or thread.automated:
        return None
    for index, message in enumerate(thread.messages):
        if message.role != "external" or nonspace_len(message.body) < cfg["holdout_min_external_chars"]:
            continue
        if AUTOMATED_RE.search(message.body) or not QUESTION_RE.search(message.body):
            continue
        for answer in thread.messages[index + 1:]:
            if answer.role != "mailbox" or nonspace_len(answer.body) < cfg["prose_reply_min_chars"]:
                continue
            if AUTOMATED_RE.search(answer.body):
                continue
            question, historical = sanitize_replay_text(message.body), sanitize_replay_text(answer.body)
            if question and historical:
                return question, historical
    return None


def holdout_eligible(thread: Thread, cfg: dict[str, Any]) -> bool:
    return bool(thread.holdout_question and thread.holdout_answer)


def pseudo_rank(seed: int, tag: str, thread_id: str) -> str:
    return hashlib.sha1(f"{seed}:{tag}:{thread_id}".encode()).hexdigest()


def select_holdout(threads: list[Thread], cfg: dict[str, Any]) -> set[str]:
    eligible = [t for t in threads if holdout_eligible(t, cfg)]
    strata: dict[tuple[str, str | None], list[Thread]] = defaultdict(list)
    for thread in eligible:
        strata[(thread.era, thread.origin_cluster)].append(thread)
    order = sorted(strata, key=lambda k: (str(k[0]), str(k[1])))
    for key in order:
        strata[key].sort(key=lambda t: pseudo_rank(cfg["seed"], "hold", t.id))
    chosen: list[str] = []
    depth = 0
    target = cfg["holdout_count"]
    while len(chosen) < target:
        progressed = False
        for key in order:
            if depth < len(strata[key]):
                chosen.append(strata[key][depth].id)
                progressed = True
                if len(chosen) >= target:
                    break
        if not progressed:
            break
        depth += 1
    return set(chosen)


def depth_band(message_count: int) -> str:
    if message_count <= 1:
        return "1"
    if message_count <= 3:
        return "2-3"
    if message_count <= 6:
        return "4-6"
    return "7+"


def stratified_sample(threads: list[Thread], cap: int, seed: int) -> list[str]:
    if len(threads) <= cap:
        return sorted(t.id for t in threads)
    strata: dict[tuple[str, str, str, str], list[Thread]] = defaultdict(list)
    for thread in threads:
        strata[(thread.era, thread.language, thread.subject_family, depth_band(thread.message_count))].append(thread)
    order = sorted(strata)
    for key in order:
        strata[key].sort(key=lambda t: pseudo_rank(seed, "sample", t.id))
    chosen: list[str] = []
    depth = 0
    while len(chosen) < cap:
        progressed = False
        for key in order:
            if depth < len(strata[key]):
                chosen.append(strata[key][depth].id)
                progressed = True
                if len(chosen) >= cap:
                    break
        if not progressed:
            break
        depth += 1
    return sorted(chosen)


def finalize(threads: list[Thread], holdout: set[str]) -> None:
    for thread in threads:
        if thread.id in holdout:
            thread.holdout, thread.status, thread.cluster = True, "holdout", None
        elif thread.message_count == 0:
            thread.status, thread.cluster = "excluded_noise", None
        else:
            thread.status, thread.cluster = "assigned", thread.origin_cluster


def build_cluster_plans(clusters: list[tuple[str, str, list[Thread]]], cfg: dict[str, Any]
                        ) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for cluster_id, label, members in clusters:
        assigned = [t for t in members if t.status == "assigned"]
        deep = sorted(t.id for t in assigned if t.risk_markers or t.ambiguous)
        ordinary = [t for t in assigned if not t.risk_markers and not t.ambiguous]
        plans.append({
            "id": cluster_id, "label": label, "size": len(assigned),
            "thread_ids": sorted(t.id for t in assigned),
            "sample_ids": stratified_sample(ordinary, cfg["sample_cap"], cfg["seed"]),
            "deep_read_ids": deep,
        })
    return plans


# --- output builders ------------------------------------------------------------------------

def manifest_row(thread: Thread) -> dict[str, Any]:
    return {
        "id": thread.id, "source_format": thread.source_format, "section_index": thread.section_index,
        "date_first": thread.date_first, "date_last": thread.date_last, "era": thread.era,
        "subject_family": thread.subject_family, "language": thread.language,
        "message_count": thread.message_count, "mailbox_message_count": thread.mailbox_message_count,
        "external_message_count": thread.external_message_count, "direction": thread.direction,
        "structural_messages": thread.source_format == "v3",
        "message_directions": dict(sorted(Counter(
            message.direction for message in thread.messages if message.direction
        ).items())),
        "actor_roles": dict(sorted(Counter(
            message.actor_role for message in thread.messages if message.actor_role
        ).items())),
        "form_source": thread.form_source, "attachments": thread.attachments,
        "prose_reply": thread.prose_reply, "prose_reply_count": thread.prose_reply_count,
        "occurrences": thread.occurrences, "risk_markers": thread.risk_markers,
        "ambiguous": thread.ambiguous,
        "cluster": thread.cluster, "secondary_clusters": thread.secondary_clusters,
        "holdout": thread.holdout,
    }


def risk_report(threads: list[Thread], cfg: dict[str, Any]) -> dict[str, Any]:
    # Forced semantic deep reads share the same bounded gate: ambiguity must not bypass risk_cap.
    flagged = [t for t in threads if t.risk_markers or t.ambiguous]
    by_marker: Counter[str] = Counter()
    for thread in flagged:
        by_marker.update(thread.risk_markers)
        if thread.ambiguous:
            by_marker["ambiguous_generic_subject"] += 1
    share = round(len(flagged) / len(threads), 4) if threads else 0.0
    return {"flagged": len(flagged), "share": share, "over_cap": share > cfg["risk_cap"],
            "by_marker": dict(sorted(by_marker.items()))}


def build_outputs(threads: list[Thread], plans: list[dict[str, Any]], harvested_at: str,
                  corpus_format: str, cfg: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, str]:
    by_id = sorted(threads, key=lambda t: t.id)
    manifest = "".join(json.dumps(manifest_row(t), ensure_ascii=False) + "\n" for t in by_id)

    clusters_doc = {"generated_at": harvested_at, "clusters": plans}

    holdout_threads = [t for t in by_id if t.holdout]
    holdout_doc = {
        "count": len(holdout_threads),
        "ids": [t.id for t in holdout_threads],
        "threads": [{"id": t.id, "era": t.era, "origin_cluster": t.origin_cluster,
                     "external_chars": t.max_external_chars, "prose_reply_count": t.prose_reply_count}
                    for t in holdout_threads],
        "replay_cases": [{"id": t.id, "question": t.holdout_question,
                          "historical_answer": t.holdout_answer}
                         for t in holdout_threads],
    }
    replay_doc = {
        "schema_version": 1,
        "count": len(holdout_threads),
        "cases": holdout_doc["replay_cases"],
    }

    dated = [t for t in threads if t.date_first]
    span = ([min(t.date_first for t in dated), max(t.date_last or t.date_first for t in dated)]
            if dated else [None, None])
    assigned_count = sum(1 for t in threads if t.status == "assigned")
    ledger_doc = {
        "generated_at": harvested_at,
        "corpus": {"format": corpus_format, "threads": len(threads), "date_span": span},
        "clusters": [{"id": p["id"], "size": p["size"]} for p in plans],
        "threads": {t.id: {"cluster": t.cluster, "status": t.status, "read": t.read,
                           "routed_to": t.routed_to} for t in by_id},
        "risk": risk_report(threads, cfg),
        "holdout": {"count": len(holdout_threads), "ids": [t.id for t in holdout_threads]},
        "reading_plan": {
            "deep_read_ids": sorted(tid for plan in plans for tid in plan["deep_read_ids"]),
            "sample_ids": sorted(tid for plan in plans for tid in plan["sample_ids"]),
        },
        "assigned": assigned_count,
    }
    diagnostics_lines = [
        "# Harvest pre-synthesis diagnostics", "",
        f"Accepted: {diagnostics['accepted_count']}",
        f"Deduplicated: {diagnostics['deduplicated']}", "",
    ]
    for heading, key in (("Rejection reasons", "rejection_reasons"),
                         ("Actor types", "actor_types"),
                         ("Initiation types", "initiation_types")):
        diagnostics_lines.extend([f"## {heading}", ""])
        counts = diagnostics[key]
        diagnostics_lines.extend(f"- {category}: {count}" for category, count in counts.items())
        if not counts:
            diagnostics_lines.append("- unavailable")
        diagnostics_lines.append("")
    diagnostics_lines.extend(["## Candidate date bands", ""])
    diagnostics_lines.extend(
        f"- [{band['start']}, {band['end']}): {band['count']}"
        for band in diagnostics["candidate_bands"]
    )
    if not diagnostics["candidate_bands"]:
        diagnostics_lines.append("- unavailable")
    return {
        "manifest.jsonl": manifest,
        "clusters.json": json.dumps(clusters_doc, indent=2, ensure_ascii=False) + "\n",
        "holdout.json": json.dumps(holdout_doc, indent=2, ensure_ascii=False) + "\n",
        "replay-cases.json": json.dumps(replay_doc, indent=2, ensure_ascii=False) + "\n",
        "ledger.json": json.dumps(ledger_doc, indent=2, ensure_ascii=False) + "\n",
        "diagnostics.json": json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n",
        "diagnostics.md": "\n".join(diagnostics_lines).rstrip() + "\n",
    }


# --- scratch safety + atomic publish --------------------------------------------------------

def check_safe_output(scratch: Path,
                      git_runner: Callable[..., Any] = subprocess.run) -> Path:
    scratch = scratch.resolve()
    top = git_runner(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if top.returncode != 0:
        raise HarvestError("run from a git checkout and choose a gitignored --scratch directory")
    root = Path(top.stdout.strip()).resolve()
    try:
        scratch.relative_to(root)
    except ValueError as exc:
        raise HarvestError(f"--scratch must be inside the current git checkout ({root})") from exc
    probe = scratch / ".harvest-ignore-check"
    ignored = git_runner(["git", "check-ignore", "-q", str(probe)], cwd=root).returncode == 0
    if not ignored:
        raise HarvestError(f"refusing stageable scratch root {scratch}; add it to .gitignore first")
    return root


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def synthetic_preflight(export_id: str = "exp-test", *, repo_root: str = "/synthetic/repo",
                        project: str = "project-test", mailbox: str = "mailbox-test",
                        provider: str = "google", tenant: str = "tenant-test") -> dict[str, Any]:
    """Explicit test/library fixture. Production CLI preparation never synthesizes preflight."""
    target = {"project": project, "tenant": tenant, "mailbox": mailbox, "provider": provider,
              "export_id": export_id}
    return {
        "schema_version": 3, "repo_root": repo_root, "target": target,
        "access": {"verified": True, "capabilities": ["config:write"],
                   "read": {"persona": True, "triage_policy": True, "hard_rule": True},
                   "write": {"persona": True, "triage_policy": True, "hard_rule": True}},
        "verification": {"auth": True, "access": True, "mailbox": True,
                         "provider": True, "export": True},
        "scope_matrix": {
            "persona": {"available_scopes": ["mailbox", "tenant", "project"],
                        "narrowest_target": "tenant", "target_available": True,
                        "write_verified": True},
            "triage_policy": {"available_scopes": ["tenant", "project"],
                              "narrowest_target": "tenant", "mailbox_scope": False,
                              "target_available": True,
                              "write_verified": True},
            "hard_rules": {"available_scopes": ["tenant", "project"],
                           "narrowest_target": "tenant", "mailbox_scope": False,
                           "target_available": True,
                           "write_verified": True},
            "brain_facts": {"available_scopes": ["tenant", "project"],
                            "narrowest_target": "business_scope"},
        },
        "corpus": {"files": 1, "formats": ["v2"]}, "checks": [], "result": "pass",
    }


def clear_outputs(root: Path, names: Iterable[str]) -> None:
    for name in names:
        target = root / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def publish_stage(stage: Path, root: Path, names: Iterable[str]) -> None:
    names = list(names)
    clear_outputs(root, names)
    for name in names:
        source = stage / name
        if source.exists():
            source.replace(root / name)


# --- corpus acquisition ---------------------------------------------------------------------

def corpus_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(f for f in path.iterdir() if f.suffix in (".md", ".txt") and f.is_file())
    elif path.is_file():
        files = [path]
    else:
        raise HarvestError(f"corpus path not found: {path}")
    if not files:
        raise HarvestError(f"no corpus files (*.md/*.txt) under {path}")
    return files


# --- subcommands ----------------------------------------------------------------------------

def load_config(args: argparse.Namespace) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    config_path = getattr(args, "config", None)
    if config_path:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise HarvestError("--config must be a JSON object")
        for key, value in data.items():
            if key not in DEFAULTS:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HarvestError(f"--config {key} must be a number, got {value!r}")
            cfg[key] = value
    if getattr(args, "holdout", None) is not None:
        cfg["holdout_count"] = args.holdout
    if getattr(args, "seed", None) is not None:
        cfg["seed"] = args.seed
    return validate_config(cfg, "--config")


def corpus_digest(sources: list[Path]) -> str:
    digest = hashlib.sha256()
    for source in sources:
        payload = source.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def load_templates(path: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the host artifact and strip source draft IDs from synthesis input."""
    empty = {"templates_format": "v1", "template_count": 0, "templates": []}
    metadata = {"format": "", "count": 0, "sha256": ""}
    if path is None:
        return empty, metadata
    if not path.is_file():
        raise HarvestError(f"templates artifact not found: {path}")
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise HarvestError("templates artifact exceeds the 4 MiB safety limit")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarvestError(f"templates artifact is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("templates_format") != "v1":
        raise HarvestError("templates artifact must declare templates_format v1")
    rows = document.get("templates")
    count = document.get("template_count")
    if not isinstance(rows, list) or not isinstance(count, int) or isinstance(count, bool) or count != len(rows):
        raise HarvestError("templates artifact template_count must match templates")
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise HarvestError(f"templates[{index}] must be an object")
        source_id, subject, body = row.get("id"), row.get("subject", ""), row.get("body", "")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            raise HarvestError(f"templates[{index}].id must be nonempty and unique")
        if not isinstance(subject, str) or not isinstance(body, str) or (not subject.strip() and not body.strip()):
            raise HarvestError(f"templates[{index}] must contain a nonblank string subject or body")
        seen.add(source_id)
        normalized.append({"subject": subject.strip(), "body": body.strip()})
    prepared = {"templates_format": "v1", "template_count": len(normalized), "templates": normalized}
    return prepared, {"format": "v1", "count": len(normalized), "sha256": hashlib.sha256(raw).hexdigest()}


def prepare_scratch(corpus: Path, scratch: Path, cfg: dict[str, Any], export_id: str = "",
                    *, preflight: dict[str, Any] | None = None,
                    templates: Path | None = None) -> dict[str, Any]:
    validate_config(cfg)
    if export_id and not SAFE_EXPORT_ID_RE.fullmatch(export_id):
        raise HarvestError("export_id must be 1-128 safe handle characters (letters/digits/._:-)")
    if preflight is None:
        raise HarvestError("prepare_scratch requires an explicit verified preflight fixture")
    validate_preflight(preflight, expected_export_id=export_id)
    target = preflight["target"]
    if export_id != target["export_id"]:
        raise HarvestError("--export-id does not match the verified preflight export")
    scratch.mkdir(parents=True, exist_ok=True)
    sources = corpus_files(corpus)
    corpus_dir = scratch / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    for source in sources:
        destination = corpus_dir / source.name
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)

    threads, harvested_at, formats, diagnostics = load_threads(sources)
    harvested = date.fromisoformat(harvested_at[:10]) if harvested_at else None
    for thread in threads:
        compute_metadata(thread, cfg, harvested)
    assign_ids(threads)
    clusters = cluster_threads(threads, cfg)
    holdout = select_holdout(threads, cfg)
    if cfg["holdout_count"] > 0 and len(holdout) != cfg["holdout_count"]:
        raise HarvestError("not enough eligible substantive question/answer threads for holdout: "
                           f"requested {cfg['holdout_count']}, found {len(holdout)}; "
                           "lower --holdout deliberately or acquire a larger corpus")
    finalize(threads, holdout)
    plans = build_cluster_plans(clusters, cfg)
    outputs = build_outputs(threads, plans, harvested_at, formats[0], cfg, diagnostics)
    prepared_templates, templates_meta = load_templates(templates)
    run_doc = {
        "schema_version": 1,
        "generated_at": harvested_at,
        "export_id": export_id,
        "target": target,
        "preflight": {"sha256": document_digest(preflight), "schema_version": preflight["schema_version"]},
        "config": dict(sorted(cfg.items())),
        "inputs": {
            "corpus_files": len(sources),
            "corpus_sha256": corpus_digest(sources),
            "format": formats[0],
            "templates_format": templates_meta["format"],
            "templates_count": templates_meta["count"],
            "templates_sha256": templates_meta["sha256"],
        },
    }

    stage = Path(tempfile.mkdtemp(prefix=".prepare-stage-", dir=scratch))
    try:
        threads_dir = stage / "threads"
        threads_dir.mkdir()
        # Reserved holdouts never enter the synthesis-readable thread tree. Evaluation uses only the
        # separately generated, redacted replay cases after synthesis is complete.
        for thread in sorted((item for item in threads if not item.holdout), key=lambda t: t.id):
            (threads_dir / f"{thread.id}.md").write_text(thread.raw + "\n", encoding="utf-8")
        for name in ("manifest.jsonl", "clusters.json", "holdout.json", "replay-cases.json",
                     "ledger.json", "diagnostics.json", "diagnostics.md"):
            (stage / name).write_text(outputs[name], encoding="utf-8")
        (stage / "run.json").write_text(json.dumps(run_doc, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
        (stage / "templates.json").write_text(json.dumps(prepared_templates, indent=2, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        publish_stage(stage, scratch, OUTPUT_NAMES)
        atomic_write_text(scratch / "preflight.json",
                          json.dumps(preflight, indent=2, ensure_ascii=False) + "\n")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    for name in ("drafts", "critic", "brief"):
        (scratch / name).mkdir(exist_ok=True)
    return {"threads": len(threads), "clusters": len(plans),
            "holdout": len(holdout), "assigned": sum(1 for t in threads if t.status == "assigned")}


def cmd_prepare(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    repo_root = Path(check_safe_output(scratch)).resolve()
    cfg = load_config(args)
    export_id = getattr(args, "export_id", "")
    if export_id and not SAFE_EXPORT_ID_RE.fullmatch(export_id):
        raise HarvestError("--export-id must be 1-128 safe handle characters (letters/digits/._:-)")
    preflight = load_json(scratch / "preflight.json")
    if Path(preflight.get("repo_root", "")).resolve() != repo_root:
        raise HarvestError("preflight belongs to a different brain checkout")
    summary = prepare_scratch(Path(args.corpus), scratch, cfg, export_id=export_id,
                              preflight=preflight,
                              templates=Path(args.templates) if getattr(args, "templates", None) else None)
    violations = verify_scratch(scratch)
    if violations:
        raise HarvestError("prepared ledger failed self-verification:\n  " + "\n  ".join(violations))
    print(f"prepared {summary['threads']} threads, {summary['clusters']} clusters, "
          f"{summary['holdout']} holdout in {scratch}")
    return 0


def load_json(path: Path) -> Any:
    if not path.exists():
        raise HarvestError(f"missing {path.name}; run prepare first")
    return json.loads(path.read_text(encoding="utf-8"))


def read_manifest_ids(scratch: Path) -> list[str]:
    path = scratch / "manifest.jsonl"
    if not path.exists():
        raise HarvestError("missing manifest.jsonl; run prepare first")
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            ids.append(json.loads(line)["id"])
    return ids


def verify_scratch(scratch: Path) -> list[str]:
    return verify_docs(read_manifest_ids(scratch), load_json(scratch / "ledger.json"),
                       load_json(scratch / "clusters.json"))


def verify_docs(manifest_list: list[str], ledger: dict[str, Any],
                clusters_doc: dict[str, Any]) -> list[str]:
    manifest_ids = set(manifest_list)
    clusters = clusters_doc["clusters"]
    threads = ledger["threads"]
    violations: list[str] = []
    for tid, count in Counter(manifest_list).items():
        if count > 1:
            violations.append(f"{tid}: appears {count} times in manifest.jsonl")

    ledger_ids = set(threads)
    for missing in sorted(manifest_ids - ledger_ids):
        violations.append(f"{missing}: in manifest but absent from ledger")
    for extra in sorted(ledger_ids - manifest_ids):
        violations.append(f"{extra}: in ledger but absent from manifest")

    membership: dict[str, list[str]] = defaultdict(list)
    for cluster in clusters:
        cid = cluster["id"]
        thread_ids = cluster["thread_ids"]
        if cluster["size"] != len(thread_ids):
            violations.append(f"cluster {cid}: size {cluster['size']} != {len(thread_ids)} thread_ids")
        for tid in thread_ids:
            membership[tid].append(cid)
        for tid in cluster["sample_ids"]:
            if tid not in thread_ids:
                violations.append(f"cluster {cid}: sample_id {tid} not in thread_ids")
        for tid in cluster["deep_read_ids"]:
            if tid not in thread_ids:
                violations.append(f"cluster {cid}: deep_read_id {tid} not in thread_ids")

    assigned = 0
    for tid in sorted(ledger_ids):
        record = threads[tid]
        status = record.get("status")
        if status not in PRIMARY_STATUSES:
            violations.append(f"{tid}: invalid status {status!r}")
        if record.get("read") not in READ_STATES:
            violations.append(f"{tid}: invalid read state {record.get('read')!r}")
        homes = membership.get(tid, [])
        if status == "assigned":
            assigned += 1
            if len(homes) != 1:
                violations.append(f"{tid}: assigned but appears in {len(homes)} cluster lists {homes}")
        else:
            if homes:
                violations.append(f"{tid}: status {status} but appears in clusters {homes}")

    total_size = sum(c["size"] for c in clusters)
    if total_size != assigned:
        violations.append(f"cluster sizes sum to {total_size} but {assigned} threads are assigned")

    holdout_ids = set(ledger["holdout"]["ids"])
    status_holdout = {tid for tid, r in threads.items() if r.get("status") == "holdout"}
    if holdout_ids != status_holdout:
        violations.append(f"ledger.holdout.ids {sorted(holdout_ids)} != holdout-status threads "
                          f"{sorted(status_holdout)}")
    if ledger.get("assigned") != assigned:
        violations.append(f"ledger.assigned {ledger.get('assigned')} != {assigned} assigned-status threads")
    plan = ledger.get("reading_plan")
    if not isinstance(plan, dict) or set(plan) != {"deep_read_ids", "sample_ids"}:
        violations.append("ledger.reading_plan is missing or malformed")
    else:
        deep_plan, sample_plan = plan["deep_read_ids"], plan["sample_ids"]
        if not isinstance(deep_plan, list) or not isinstance(sample_plan, list):
            violations.append("ledger.reading_plan values must be arrays")
        else:
            if len(deep_plan) != len(set(deep_plan)) or len(sample_plan) != len(set(sample_plan)):
                violations.append("ledger.reading_plan contains duplicate ids")
            overlap = set(deep_plan) & set(sample_plan)
            if overlap:
                violations.append(f"ledger.reading_plan deep/sample overlap: {sorted(overlap)}")
            invalid = (set(deep_plan) | set(sample_plan)) - {
                tid for tid, row in threads.items() if row.get("status") == "assigned"
            }
            if invalid:
                violations.append(f"ledger.reading_plan contains non-assigned ids: {sorted(invalid)}")
    return violations


def cmd_verify(args: argparse.Namespace) -> int:
    violations = verify_scratch(Path(args.scratch).resolve())
    if violations:
        print("coverage ledger invariants FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    print("coverage ledger OK")
    return 0


def require_object(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarvestError(f"{path} must be an object")
    missing, extra = keys - set(value), set(value) - keys
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if extra:
            details.append(f"unexpected {sorted(extra)}")
        raise HarvestError(f"{path} has invalid schema: {', '.join(details)}")
    return value


def require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise HarvestError(f"{path} must be an array")
    return value


def require_string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise HarvestError(f"{path} must be a{' possibly empty' if allow_empty else ' non-empty'} string")
    return value


def require_int(value: Any, path: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise HarvestError(f"{path} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise HarvestError(f"{path} must be an integer <= {maximum}")
    return value


def require_number(value: Any, path: str, *, positive: bool = False) -> float | int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HarvestError(f"{path} must be a number")
    if not math.isfinite(value):
        raise HarvestError(f"{path} must be finite")
    if (positive and value <= 0) or (not positive and value < 0):
        op = "> 0" if positive else ">= 0"
        raise HarvestError(f"{path} must be {op}")
    return value


def opaque_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(OPAQUE_ID_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            found.update(opaque_ids(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.update(opaque_ids(key))
            found.update(opaque_ids(item))
    return found


def validate_id_list(value: Any, path: str) -> list[str]:
    ids = require_list(value, path)
    for index, thread_id in enumerate(ids):
        require_string(thread_id, f"{path}[{index}]")
        if not OPAQUE_ID_RE.fullmatch(thread_id):
            raise HarvestError(f"{path}[{index}] must match H plus 32 lowercase hex characters")
    if len(ids) != len(set(ids)):
        raise HarvestError(f"{path} contains duplicate thread ids")
    return ids


def validate_agent_report(report: Any, path: str) -> dict[str, Any]:
    report = require_object(report, path, {
        "cluster", "read_deep", "read_sampled", "route_elsewhere", "contradictions",
        "saturation", "counts",
    })
    require_string(report["cluster"], f"{path}.cluster")
    deep = validate_id_list(report["read_deep"], f"{path}.read_deep")
    sampled = validate_id_list(report["read_sampled"], f"{path}.read_sampled")
    overlap = set(deep) & set(sampled)
    if overlap:
        raise HarvestError(f"{path}: read_deep and read_sampled overlap: {sorted(overlap)}")
    for index, move in enumerate(require_list(report["route_elsewhere"], f"{path}.route_elsewhere")):
        move = require_object(move, f"{path}.route_elsewhere[{index}]",
                              {"id", "suggested_cluster", "reason"})
        validate_id_list([move["id"]], f"{path}.route_elsewhere[{index}].id")
        require_string(move["suggested_cluster"], f"{path}.route_elsewhere[{index}].suggested_cluster")
        require_string(move["reason"], f"{path}.route_elsewhere[{index}].reason")
    for index, conflict in enumerate(require_list(report["contradictions"],
                                                   f"{path}.contradictions")):
        conflict = require_object(conflict, f"{path}.contradictions[{index}]", {"ids", "topic", "note"})
        validate_id_list(conflict["ids"], f"{path}.contradictions[{index}].ids")
        require_string(conflict["topic"], f"{path}.contradictions[{index}].topic")
        require_string(conflict["note"], f"{path}.contradictions[{index}].note")
    saturation = require_object(report["saturation"], f"{path}.saturation",
                                {"still_yielding", "note"})
    if not isinstance(saturation["still_yielding"], bool):
        raise HarvestError(f"{path}.saturation.still_yielding must be boolean")
    require_string(saturation["note"], f"{path}.saturation.note", allow_empty=True)
    counts = require_object(report["counts"], f"{path}.counts", {"assigned", "read"})
    require_int(counts["assigned"], f"{path}.counts.assigned")
    require_int(counts["read"], f"{path}.counts.read")
    if counts["read"] != len(deep) + len(sampled):
        raise HarvestError(f"{path}.counts.read does not match unique read ids")
    return report


def reject_synthesis_holdout_leakage(value: Any, holdout_ids: set[str], path: str) -> None:
    leaked = opaque_ids(value) & holdout_ids
    if leaked:
        raise HarvestError(f"holdout leakage in synthesis artifact {path}: {sorted(leaked)}")


def replay_content_fingerprints(replay_cases: dict[str, Any]) -> set[str]:
    """Long normalized windows catch copied holdout content without storing new raw material."""
    fingerprints: set[str] = set()
    for case in replay_cases["cases"]:
        for field_name in ("question", "historical_answer"):
            words = re.findall(r"\w+", case[field_name].casefold())
            width = min(12, len(words))
            if width:
                fingerprints.update(" ".join(words[index:index + width])
                                    for index in range(len(words) - width + 1))
    return fingerprints


def scan_synthesis_holdout_leakage(scratch: Path, holdout_ids: set[str],
                                    replay_cases: dict[str, Any]) -> None:
    """Scan every agent/reduction/critic artifact for holdout handles or copied content."""
    fingerprints = replay_content_fingerprints(replay_cases)
    for directory_name in ("drafts", "critic"):
        directory = scratch / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            payload = path.read_bytes()
            leaked = {thread_id for thread_id in holdout_ids if thread_id.encode() in payload}
            if leaked:
                relative = path.relative_to(scratch)
                raise HarvestError(f"holdout leakage in synthesis artifact {relative}: {sorted(leaked)}")
            normalized = " ".join(re.findall(r"\w+", payload.decode("utf-8", "replace").casefold()))
            if any(fingerprint in normalized for fingerprint in fingerprints):
                relative = path.relative_to(scratch)
                raise HarvestError(f"holdout content leakage in synthesis artifact {relative}")


def scan_explicit_synthesis_inputs(paths: Iterable[Path], replay_cases: dict[str, Any]) -> None:
    """CLI inputs may live outside scratch; apply the same content gate to their exact bytes."""
    fingerprints = replay_content_fingerprints(replay_cases)
    for path in paths:
        payload = path.read_bytes()
        normalized = " ".join(re.findall(r"\w+", payload.decode("utf-8", "replace").casefold()))
        if any(fingerprint in normalized for fingerprint in fingerprints):
            raise HarvestError(f"holdout content leakage in synthesis input {path}")


def validate_reduction(value: Any, path: str = "reduction") -> dict[str, Any]:
    value = require_object(value, path,
                           {"settings_changes", "skip_proposals", "durable_rules", "contradictions"})
    for index, change in enumerate(require_list(value["settings_changes"], f"{path}.settings_changes")):
        item_path = f"{path}.settings_changes[{index}]"
        change = require_object(change, item_path,
                                {"surface", "scope", "status", "summary", "scope_authority",
                                 "verification"})
        surface = require_string(change["surface"], f"{item_path}.surface")
        scope = require_string(change["scope"], f"{item_path}.scope")
        status = require_string(change["status"], f"{item_path}.status")
        if surface not in {"persona", "triage_policy", "hard_rule"}:
            raise HarvestError(f"{item_path}.surface must be persona, triage_policy, or hard_rule")
        if scope not in {"mailbox", "tenant", "project"}:
            raise HarvestError(f"{item_path}.scope must be mailbox, tenant, or project")
        if status not in {"applied", "pending"}:
            raise HarvestError(f"{item_path}.status must be applied or pending")
        if not isinstance(change["scope_authority"], bool):
            raise HarvestError(f"{item_path}.scope_authority must be boolean")
        require_string(change["summary"], f"{item_path}.summary")
        if surface == "persona" and status == "applied" and scope == "mailbox":
            raise HarvestError(f"{item_path}: harvested persona must use tenant or project scope")
        if surface in {"triage_policy", "hard_rule"} and scope == "mailbox":
            raise HarvestError(f"{item_path}: {surface} has no mailbox scope")
        if surface in {"triage_policy", "hard_rule"} and status == "applied" \
                and not change["scope_authority"]:
            raise HarvestError(f"{item_path}: applied widened setting requires explicit scope_authority")
        if status == "pending":
            if change["verification"] is not None:
                raise HarvestError(f"{item_path}.verification must be null for a pending change")
        else:
            verification = require_object(change["verification"], f"{item_path}.verification", {
                "pre_read_at", "post_read_at", "before_sha256", "after_sha256",
                "before_file", "after_file", "resolved_scope", "resolved_target",
            })
            parsed_times = []
            for field_name in ("pre_read_at", "post_read_at"):
                raw = require_string(verification[field_name], f"{item_path}.verification.{field_name}")
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise HarvestError(f"{item_path}.verification.{field_name} must be ISO-8601") from exc
                if parsed.tzinfo is None:
                    raise HarvestError(f"{item_path}.verification.{field_name} must include a timezone")
                parsed_times.append(parsed)
            elapsed = (parsed_times[1] - parsed_times[0]).total_seconds()
            if elapsed < 0 or elapsed > 300:
                raise HarvestError(f"{item_path}.verification must re-read within five minutes of mutation")
            before = require_string(verification["before_sha256"],
                                    f"{item_path}.verification.before_sha256")
            after = require_string(verification["after_sha256"],
                                   f"{item_path}.verification.after_sha256")
            if not re.fullmatch(r"[0-9a-f]{64}", before) or not re.fullmatch(r"[0-9a-f]{64}", after):
                raise HarvestError(f"{item_path}.verification digests must be lowercase SHA-256")
            if before == after:
                raise HarvestError(f"{item_path}.verification does not prove a changed resolved value")
            if verification["resolved_scope"] != scope:
                raise HarvestError(f"{item_path}.verification.resolved_scope must equal change scope")
            require_string(verification["resolved_target"],
                           f"{item_path}.verification.resolved_target")
            for field_name in ("before_file", "after_file"):
                relative = Path(require_string(verification[field_name],
                                               f"{item_path}.verification.{field_name}"))
                if relative.is_absolute() or ".." in relative.parts:
                    raise HarvestError(f"{item_path}.verification.{field_name} must stay inside scratch")
    for index, proposal in enumerate(require_list(value["skip_proposals"], f"{path}.skip_proposals")):
        item_path = f"{path}.skip_proposals[{index}]"
        proposal = require_object(proposal, item_path,
                                  {"summary", "evidence_class", "evidence_count", "evidence_ids"})
        require_string(proposal["summary"], f"{item_path}.summary")
        if proposal["evidence_class"] != "presence_without_prose_reply":
            raise HarvestError(f"{item_path}.evidence_class must be presence_without_prose_reply")
        require_int(proposal["evidence_count"], f"{item_path}.evidence_count", minimum=1)
        validate_id_list(proposal["evidence_ids"], f"{item_path}.evidence_ids")
    for index, rule in enumerate(require_list(value["durable_rules"], f"{path}.durable_rules")):
        item_path = f"{path}.durable_rules[{index}]"
        rule = require_object(rule, item_path,
                              {"summary", "evidence_strength", "evidence_ids", "era", "stale_era"})
        require_string(rule["summary"], f"{item_path}.summary")
        require_int(rule["evidence_strength"], f"{item_path}.evidence_strength", minimum=1)
        evidence_ids = validate_id_list(rule["evidence_ids"], f"{item_path}.evidence_ids")
        if len(evidence_ids) != rule["evidence_strength"]:
            raise HarvestError(f"{item_path}.evidence_strength must equal evidence_ids count")
        if rule["era"] not in {"recent", "mid", "old", "mixed"}:
            raise HarvestError(f"{item_path}.era must be recent, mid, old, or mixed")
        if not isinstance(rule["stale_era"], bool):
            raise HarvestError(f"{item_path}.stale_era must be boolean")
        if rule["era"] == "old" and not rule["stale_era"]:
            raise HarvestError(f"{item_path}: old-only evidence must be marked stale_era")
    for index, conflict in enumerate(require_list(value["contradictions"], f"{path}.contradictions")):
        item_path = f"{path}.contradictions[{index}]"
        conflict = require_object(conflict, item_path,
                                  {"topic", "status", "resolution", "supersession"})
        require_string(conflict["topic"], f"{item_path}.topic")
        if conflict["status"] not in {"resolved", "unresolved"}:
            raise HarvestError(f"{item_path}.status must be resolved or unresolved")
        require_string(conflict["resolution"], f"{item_path}.resolution")
        require_string(conflict["supersession"], f"{item_path}.supersession", allow_empty=True)
    return value


def reconcile_settings_verification(reduction: dict[str, Any], scratch: Path,
                                    preflight: dict[str, Any]) -> None:
    target = preflight["target"]
    for index, change in enumerate(reduction["settings_changes"]):
        if change["status"] != "applied":
            continue
        if change["surface"] == "persona":
            expected_scope = "tenant" if target.get("tenant") else "project"
            if change["scope"] != expected_scope:
                raise HarvestError(f"reduction.settings_changes[{index}] persona scope must match "
                                   f"harvest business scope {expected_scope}")
        verification = change["verification"]
        expected_target = target[change["scope"]]
        if not expected_target or verification["resolved_target"] != expected_target:
            raise HarvestError(f"reduction.settings_changes[{index}] verification target does not match "
                               f"preflight {change['scope']} target")
        for kind in ("before", "after"):
            path = (scratch / verification[f"{kind}_file"]).resolve()
            try:
                path.relative_to(scratch)
            except ValueError as exc:
                raise HarvestError(f"reduction.settings_changes[{index}] verification file escaped scratch") \
                    from exc
            if not path.is_file():
                raise HarvestError(f"reduction.settings_changes[{index}] missing {kind} settings snapshot")
            if hashlib.sha256(path.read_bytes()).hexdigest() != verification[f"{kind}_sha256"]:
                raise HarvestError(f"reduction.settings_changes[{index}] {kind} settings snapshot digest changed")


def validate_evaluation(value: Any, holdout_ids: set[str], path: str = "evaluation") -> dict[str, Any]:
    value = require_object(value, path, {"holdouts", "production_replay"})
    seen: list[str] = []
    replay_ids: list[str] = []
    trace_urls: list[str] = []
    for index, score in enumerate(require_list(value["holdouts"], f"{path}.holdouts")):
        item_path = f"{path}.holdouts[{index}]"
        score = require_object(score, item_path,
                               {"id", "replay_id", "status", "trace_url", "brain_sha", "scores", "notes"})
        thread_id = validate_id_list([score["id"]], f"{item_path}.id")[0]
        seen.append(thread_id)
        replay_ids.append(require_string(score["replay_id"], f"{item_path}.replay_id"))
        status = require_string(score["status"], f"{item_path}.status").casefold()
        if status not in SUCCESS_STATUSES:
            raise HarvestError(f"{item_path}.status must be successful")
        trace_url = require_string(score["trace_url"], f"{item_path}.trace_url")
        parsed = urlparse(trace_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HarvestError(f"{item_path}.trace_url must be an absolute HTTP(S) URL")
        trace_urls.append(trace_url)
        if not SHA40_RE.fullmatch(require_string(score["brain_sha"], f"{item_path}.brain_sha")):
            raise HarvestError(f"{item_path}.brain_sha must be a resolved lowercase 40-char SHA")
        scores = require_object(score["scores"], f"{item_path}.scores",
                                {"factual_agreement", "routing", "tone"})
        for dimension in ("factual_agreement", "routing", "tone"):
            require_int(scores[dimension], f"{item_path}.scores.{dimension}", maximum=4)
        require_string(score["notes"], f"{item_path}.notes", allow_empty=True)
    if len(seen) != len(set(seen)):
        raise HarvestError(f"{path}.holdouts contains duplicate score records")
    if len(replay_ids) != len(set(replay_ids)) or len(trace_urls) != len(set(trace_urls)):
        raise HarvestError(f"{path}.holdouts must use a distinct replay id and trace URL per case")
    if set(seen) != holdout_ids:
        raise HarvestError(f"{path}.holdouts must score each reserved holdout exactly once; "
                           f"expected {sorted(holdout_ids)}, got {sorted(seen)}")
    replay = require_object(value["production_replay"], f"{path}.production_replay",
                            {"run_id", "status", "turns", "trace_url", "brain_sha", "brain_diff"})
    production_run_id = require_string(replay["run_id"], f"{path}.production_replay.run_id")
    if require_string(replay["status"], f"{path}.production_replay.status").casefold() \
            not in SUCCESS_STATUSES:
        raise HarvestError(f"{path}.production_replay.status must be successful")
    require_int(replay["turns"], f"{path}.production_replay.turns", minimum=1)
    trace_url = require_string(replay["trace_url"], f"{path}.production_replay.trace_url")
    parsed = urlparse(trace_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HarvestError(f"{path}.production_replay.trace_url must be an absolute HTTP(S) URL")
    if production_run_id in replay_ids or trace_url in trace_urls:
        raise HarvestError(f"{path}.production_replay must be distinct from every holdout replay")
    brain_sha = require_string(replay["brain_sha"], f"{path}.production_replay.brain_sha")
    if not SHA40_RE.fullmatch(brain_sha):
        raise HarvestError(f"{path}.production_replay.brain_sha must be a resolved lowercase 40-char SHA")
    require_string(replay["brain_diff"], f"{path}.production_replay.brain_diff")
    mismatched_sha = [score["id"] for score in value["holdouts"] if score["brain_sha"] != brain_sha]
    if mismatched_sha:
        raise HarvestError(f"{path} holdouts and production replay must use the same dev brain SHA")
    return value


def validate_replay_cases(value: Any, holdout_ids: set[str], path: str = "replay-cases.json"
                          ) -> dict[str, Any]:
    value = require_object(value, path, {"schema_version", "count", "cases"})
    if value["schema_version"] != 1:
        raise HarvestError(f"{path}.schema_version must be 1")
    count = require_int(value["count"], f"{path}.count", minimum=1)
    cases = require_list(value["cases"], f"{path}.cases")
    if len(cases) != count:
        raise HarvestError(f"{path}.count does not match cases")
    seen: list[str] = []
    for index, case in enumerate(cases):
        item_path = f"{path}.cases[{index}]"
        case = require_object(case, item_path, {"id", "question", "historical_answer"})
        seen.append(validate_id_list([case["id"]], f"{item_path}.id")[0])
        for field_name in ("question", "historical_answer"):
            text = require_string(case[field_name], f"{item_path}.{field_name}")
            if (REPLAY_EMAIL_RE.search(text) or REPLAY_URL_RE.search(text)
                    or REPLAY_PHONE_RE.search(text) or REPLAY_IDENTIFIER_RE.search(text)
                    or REPLAY_NAME_RE.search(text)):
                raise HarvestError(f"{item_path}.{field_name} contains unredacted private data")
    if len(seen) != len(set(seen)) or set(seen) != holdout_ids:
        raise HarvestError(f"{path} must contain each reserved holdout exactly once")
    return value


def validate_metrics(value: Any, path: str = "metrics") -> dict[str, Any]:
    value = require_object(value, path, {"turns", "wall_clock_seconds", "preparation_seconds"})
    require_int(value["turns"], f"{path}.turns", minimum=1)
    require_number(value["wall_clock_seconds"], f"{path}.wall_clock_seconds", positive=True)
    require_number(value["preparation_seconds"], f"{path}.preparation_seconds")
    if value["preparation_seconds"] > value["wall_clock_seconds"]:
        raise HarvestError(f"{path}.preparation_seconds cannot exceed wall_clock_seconds")
    return value


def load_manifest(scratch: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = scratch / "manifest.jsonl"
    if not path.exists():
        raise HarvestError("missing manifest.jsonl; run prepare first")
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not OPAQUE_ID_RE.fullmatch(str(row.get("id", ""))):
            raise HarvestError(f"manifest.jsonl:{line_number} has invalid row/id")
        rows.append(row)
    return rows


def reconcile_reduction_evidence(reduction: dict[str, Any], manifest: list[dict[str, Any]],
                                 ledger: dict[str, Any]) -> None:
    by_id = {row["id"]: row for row in manifest}
    holdout_ids = set(ledger["holdout"]["ids"])

    def checked(ids: list[str], path: str) -> list[dict[str, Any]]:
        unknown = set(ids) - set(by_id)
        if unknown:
            raise HarvestError(f"{path} references unknown evidence ids {sorted(unknown)}")
        leaked = set(ids) & holdout_ids
        if leaked:
            raise HarvestError(f"{path} references holdout evidence ids {sorted(leaked)}")
        return [by_id[thread_id] for thread_id in ids]

    for index, proposal in enumerate(reduction["skip_proposals"]):
        path = f"reduction.skip_proposals[{index}]"
        rows = checked(proposal["evidence_ids"], path)
        invalid = [row["id"] for row in rows if row.get("prose_reply") is not False]
        if invalid:
            raise HarvestError(f"{path} skip evidence must have prose_reply=false: {invalid}")
        occurrences = sum(require_int(row.get("occurrences"), f"manifest {row['id']}.occurrences",
                                      minimum=1) for row in rows)
        if occurrences != proposal["evidence_count"]:
            raise HarvestError(f"{path}.evidence_count must equal manifest occurrence sum {occurrences}")
    for index, rule in enumerate(reduction["durable_rules"]):
        path = f"reduction.durable_rules[{index}]"
        rows = checked(rule["evidence_ids"], path)
        unread = [row["id"] for row in rows if ledger["threads"][row["id"]].get("read") == "none"]
        if unread:
            raise HarvestError(f"{path} durable evidence was not semantically read: {unread}")


def reconcile_reports(ledger: dict[str, Any], clusters_doc: dict[str, Any],
                      manifest: list[dict[str, Any]], reports: list[dict[str, Any]]) -> dict[str, Any]:
    threads = ledger["threads"]
    holdout_ids = set(ledger["holdout"]["ids"])
    report_by_cluster: dict[str, dict[str, Any]] = {}
    origin_members: dict[str, set[str]] = defaultdict(set)
    for row in manifest:
        if row.get("cluster"):
            origin_members[row["cluster"]].add(row["id"])
    expected_clusters = {cluster for cluster, ids in origin_members.items() if ids}
    for report in reports:
        cluster = report["cluster"]
        if cluster in report_by_cluster:
            raise HarvestError(f"duplicate agent report for cluster {cluster}")
        report_by_cluster[cluster] = report
        reject_synthesis_holdout_leakage(report, holdout_ids, f"agent report {cluster}")
        referenced = opaque_ids(report)
        unknown = referenced - set(threads)
        if unknown:
            raise HarvestError(f"agent report {cluster} references unknown ids {sorted(unknown)}")
        nonassigned = {tid for tid in referenced if threads[tid].get("status") != "assigned"}
        if nonassigned:
            raise HarvestError(f"agent report {cluster} references non-synthesis ids {sorted(nonassigned)}")
        foreign = referenced - origin_members.get(cluster, set())
        if foreign:
            raise HarvestError(f"agent report {cluster} references ids outside its original assignment: "
                               f"{sorted(foreign)}")
        if report["counts"]["assigned"] != len(origin_members.get(cluster, set())):
            raise HarvestError(f"agent report {cluster}.counts.assigned does not match manifest assignment")
        if report["saturation"]["still_yielding"]:
            raise HarvestError(f"agent report {cluster} is still_yielding; complete the orchestrator-controlled "
                               "follow-up assignment and replace the final cluster report before review")
        read_ids = set(report["read_deep"]) | set(report["read_sampled"])
        routed_ids = {move["id"] for move in report["route_elsewhere"]}
        if routed_ids - read_ids:
            raise HarvestError(f"agent report {cluster} routes unread ids: {sorted(routed_ids - read_ids)}")
        contradiction_ids = {tid for conflict in report["contradictions"] for tid in conflict["ids"]}
        if contradiction_ids - read_ids:
            raise HarvestError(f"agent report {cluster} cites unread contradiction ids: "
                               f"{sorted(contradiction_ids - read_ids)}")
    if set(report_by_cluster) != expected_clusters:
        raise HarvestError("agent reports must cover every non-empty original cluster exactly once; "
                           f"expected {sorted(expected_clusters)}, got {sorted(report_by_cluster)}")

    reported_deep: set[str] = set()
    reported_sampled: set[str] = set()
    reported_routes: dict[str, str] = {}
    for cluster, report in report_by_cluster.items():
        for thread_id in report["read_deep"]:
            if thread_id in reported_deep or thread_id in reported_sampled:
                raise HarvestError(f"thread {thread_id} is read in multiple agent reports")
            reported_deep.add(thread_id)
        for thread_id in report["read_sampled"]:
            if thread_id in reported_deep or thread_id in reported_sampled:
                raise HarvestError(f"thread {thread_id} is read in multiple agent reports")
            reported_sampled.add(thread_id)
        for move in report["route_elsewhere"]:
            thread_id, destination = move["id"], move["suggested_cluster"]
            if thread_id in reported_routes:
                raise HarvestError(f"thread {thread_id} is routed more than once")
            if destination == cluster:
                raise HarvestError(f"thread {thread_id} route destination equals source cluster")
            reported_routes[thread_id] = destination

    ledger_deep = {tid for tid, row in threads.items() if row.get("read") == "deep"}
    ledger_sampled = {tid for tid, row in threads.items() if row.get("read") == "sampled"}
    plan = ledger["reading_plan"]
    planned_deep, planned_sampled = set(plan["deep_read_ids"]), set(plan["sample_ids"])
    out_of_plan = (reported_deep | reported_sampled) - planned_deep - planned_sampled
    if out_of_plan:
        raise HarvestError(f"agent reports contain out-of-plan reads: {sorted(out_of_plan)}")
    wrong_depth = planned_deep - reported_deep
    if wrong_depth:
        raise HarvestError(f"planned deep reads were not reported as deep: {sorted(wrong_depth)}")
    missing_plan = (planned_deep | planned_sampled) - reported_deep - reported_sampled
    if missing_plan:
        raise HarvestError(f"planned reads are missing from agent reports: {sorted(missing_plan)}")
    if ledger_deep != reported_deep or ledger_sampled != reported_sampled:
        raise HarvestError("agent report read coverage does not reconcile with ledger read states")
    ledger_routes = {tid: row["routed_to"] for tid, row in threads.items() if row.get("routed_to")}
    if ledger_routes != reported_routes:
        raise HarvestError("agent report routes do not reconcile with ledger routed_to states")

    by_id = {row["id"]: row for row in manifest}
    required_deep = {tid for tid, row in by_id.items()
                     if row.get("cluster") and (row.get("risk_markers") or row.get("ambiguous"))}
    missing_deep = required_deep - ledger_deep
    if missing_deep:
        raise HarvestError(f"mechanically flagged threads were not deep-read: {sorted(missing_deep)}")
    return {"reports": report_by_cluster, "deep": reported_deep, "sampled": reported_sampled,
            "routes": reported_routes}


def coverage_summary(ledger: dict[str, Any], clusters_doc: dict[str, Any]) -> tuple[list[dict[str, Any]],
                                                                                   dict[str, int]]:
    threads = ledger["threads"]
    rows: list[dict[str, Any]] = []
    for cluster in sorted(clusters_doc["clusters"], key=lambda item: item["id"]):
        members = cluster["thread_ids"]
        rows.append({
            "cluster": cluster["id"], "label": cluster["label"], "scanned": len(members),
            "deep_read": sum(threads[tid]["read"] == "deep" for tid in members),
            "sampled": sum(threads[tid]["read"] == "sampled" for tid in members),
            "noise_excluded": 0,
            "rerouted": sum(bool(threads[tid].get("routed_to")) for tid in members),
        })
    totals = {
        "scanned": len(threads),
        "assigned": sum(row["status"] == "assigned" for row in threads.values()),
        "deep_read": sum(row["read"] == "deep" for row in threads.values()),
        "sampled": sum(row["read"] == "sampled" for row in threads.values()),
        "noise_excluded": sum(row["status"] == "excluded_noise" for row in threads.values()),
        "holdout": sum(row["status"] == "holdout" for row in threads.values()),
        "rerouted": sum(bool(row.get("routed_to")) for row in threads.values()),
    }
    if totals["assigned"] != sum(cluster["size"] for cluster in clusters_doc["clusters"]):
        raise HarvestError("coverage assigned total does not reconcile to cluster sizes")
    if totals["scanned"] != totals["assigned"] + totals["noise_excluded"] + totals["holdout"]:
        raise HarvestError("coverage primary statuses do not reconcile to scanned total")
    if totals["deep_read"] + totals["sampled"] > totals["assigned"]:
        raise HarvestError("coverage read states exceed assigned total")
    return rows, totals


def md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def validate_diagnostics_artifact(value: Any) -> dict[str, Any]:
    value = require_object(value, "diagnostics.json", {
        "schema_version", "available", "accepted_count", "rejection_reasons", "actor_types",
        "initiation_types", "candidate_bands", "deduplicated",
    })
    if value["schema_version"] != 1 or not isinstance(value["available"], bool):
        raise HarvestError("diagnostics.json must be schema v1 with boolean availability")
    for field_name in ("accepted_count", "deduplicated"):
        require_int(value[field_name], f"diagnostics.json.{field_name}")
    for field_name in ("rejection_reasons", "actor_types", "initiation_types"):
        encoded = json.dumps(value[field_name], separators=(",", ":"))
        if parse_count_map(encoded, field_name) != value[field_name]:
            raise HarvestError(f"diagnostics.json.{field_name} must be sorted")
    bands = require_list(value["candidate_bands"], "diagnostics.json.candidate_bands")
    if not value["available"]:
        if any(value[field_name] for field_name in
               ("rejection_reasons", "actor_types", "initiation_types", "candidate_bands")) \
                or value["deduplicated"] != 0:
            raise HarvestError("unavailable diagnostics may contain only the accepted fallback count")
        return value
    # Reuse the corpus parser's half-open/sorted validation without accepting a partial bundle.
    reparsed = parse_diagnostics({
        "accepted_count": str(value["accepted_count"]),
        "rejection_reasons": json.dumps(value["rejection_reasons"]),
        "actor_types": json.dumps(value["actor_types"]),
        "initiation_types": json.dumps(value["initiation_types"]),
        "candidate_bands": json.dumps(bands),
        "deduplicated": str(value["deduplicated"]),
    }, "diagnostics.json")
    if reparsed is None:
        raise HarvestError("diagnostics.json is missing")
    return value


def build_review_brief(ledger: dict[str, Any], run: dict[str, Any], coverage_rows: list[dict[str, Any]],
                       totals: dict[str, int], reports: list[dict[str, Any]],
                       reduction: dict[str, Any], evaluation: dict[str, Any],
                       metrics: dict[str, Any], diagnostics: dict[str, Any]) -> str:
    lines = [
        "# Brain harvest operator review brief", "", "## Run inputs [local+ephemeral]", "",
        f"- Export: `{md_cell(run['export_id'])}`",
        f"- Corpus digest: `{run['inputs']['corpus_sha256']}` ({run['inputs']['format']}, "
        f"{run['inputs']['corpus_files']} file(s))",
    ]
    if run["inputs"]["templates_count"]:
        lines.append(f"- Templates digest: `{run['inputs']['templates_sha256']}` "
                     f"({run['inputs']['templates_format']}, {run['inputs']['templates_count']} template(s))")
    lines.extend([
        f"- Effective config: `{json.dumps(run['config'], sort_keys=True, separators=(',', ':'))}`",
        "", "## Pre-synthesis source diagnostics [local+ephemeral]", "",
        f"Accepted: {diagnostics['accepted_count']} · Deduplicated: {diagnostics['deduplicated']}", "",
    ])
    for heading, key in (("Rejections", "rejection_reasons"), ("Actors", "actor_types"),
                         ("Initiation", "initiation_types")):
        rendered = ", ".join(f"{category}={count}"
                             for category, count in diagnostics[key].items()) or "unavailable"
        lines.append(f"- {heading}: {rendered}")
    bands = diagnostics["candidate_bands"]
    lines.append(f"- Candidate bands: {len(bands)}; candidates: "
                 f"{sum(band['count'] for band in bands)}")
    lines.extend([
        "", "## 1. Coverage summary [local+ephemeral]", "",
        "| Cluster | Label | Scanned | Deep-read | Sampled | Noise (excluded) | Rerouted |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for row in coverage_rows:
        lines.append("| {cluster} | {label} | {scanned} | {deep_read} | {sampled} | "
                     "{noise_excluded} | {rerouted} |".format(**{k: md_cell(v) for k, v in row.items()}))
    lines.append(f"| **Total** |  | **{totals['scanned']}** | **{totals['deep_read']}** | "
                 f"**{totals['sampled']}** | **{totals['noise_excluded']}** | "
                 f"**{totals['rerouted']}** |")
    lines.extend(["", f"Assigned: {totals['assigned']} · Holdout: {totals['holdout']}", "",
                  "### Saturation", ""])
    for report in sorted(reports, key=lambda item: item["cluster"]):
        saturation = report["saturation"]
        lines.append(f"- {md_cell(report['cluster'])}: still yielding "
                     f"{'yes' if saturation['still_yielding'] else 'no'} — {md_cell(saturation['note'])}")

    lines.extend(["", "## 2. Settings changes [local+ephemeral]", ""])
    if reduction["settings_changes"]:
        lines.extend(["| Surface | Scope | Status | Authority | Verification | Change |",
                      "|---|---|---|---|---|---|"])
        for change in reduction["settings_changes"]:
            verification = change["verification"]
            verified = (f"{verification['before_sha256'][:12]}→{verification['after_sha256'][:12]} "
                        f"at {md_cell(verification['resolved_target'])}"
                        if verification else "pending")
            lines.append(f"| {md_cell(change['surface'])} | {md_cell(change['scope'])} | "
                         f"{md_cell(change['status'])} | "
                         f"{'explicit' if change['scope_authority'] else 'none'} | "
                         f"{verified} | "
                         f"{md_cell(change['summary'])} |")
    else:
        lines.append("None.")

    lines.extend(["", "## 3. Skip proposals [local+ephemeral]", ""])
    if reduction["skip_proposals"]:
        lines.extend(["| Proposal | Evidence class | Occurrences | Evidence IDs |",
                      "|---|---|---:|---|"])
        for proposal in reduction["skip_proposals"]:
            lines.append(f"| {md_cell(proposal['summary'])} | {proposal['evidence_class']} | "
                         f"{proposal['evidence_count']} | {', '.join(proposal['evidence_ids'])} |")
    else:
        lines.append("None.")

    lines.extend(["", "## 4. Notable durable rules [local+ephemeral]", ""])
    if reduction["durable_rules"]:
        lines.extend(["| Rule | Supporting threads | Evidence IDs | Era | Stale era |",
                      "|---|---:|---|---|---|"])
        for rule in reduction["durable_rules"]:
            lines.append(f"| {md_cell(rule['summary'])} | {rule['evidence_strength']} | "
                         f"{', '.join(rule['evidence_ids'])} | {rule['era']} | "
                         f"{'yes' if rule['stale_era'] else 'no'} |")
    else:
        lines.append("None.")

    lines.extend(["", "## 5. Contradictions and resolutions [local+ephemeral]", ""])
    if reduction["contradictions"]:
        lines.extend(["| Topic | Status | Resolution | Supersession |", "|---|---|---|---|"])
        for conflict in reduction["contradictions"]:
            lines.append(f"| {md_cell(conflict['topic'])} | {conflict['status']} | "
                         f"{md_cell(conflict['resolution'])} | {md_cell(conflict['supersession'])} |")
    else:
        lines.append("None.")

    lines.extend(["", "## 6. Holdout scorecard [committed subset]", "",
                  "Scores use the fixed 0–4 scale.", "",
                  "| Holdout | Replay | Status | Trace | Brain SHA | Factual agreement | Routing | Tone |",
                  "|---|---|---|---|---|---:|---:|---:|"])
    for score in sorted(evaluation["holdouts"], key=lambda item: item["id"]):
        values = score["scores"]
        lines.append(f"| {score['id']} | {md_cell(score['replay_id'])} | {score['status']} | "
                     f"{md_cell(score['trace_url'])} | `{score['brain_sha']}` | "
                     f"{values['factual_agreement']} | {values['routing']} | {values['tone']} |")
    replay = evaluation["production_replay"]
    lines.extend(["", "### Representative production replay [local+ephemeral]", "",
                  f"- Run: `{md_cell(replay['run_id'])}` · status: {md_cell(replay['status'])} · "
                  f"turns: {replay['turns']}",
                  f"- Trace: {md_cell(replay['trace_url'])}",
                  f"- Resolved brain SHA: `{replay['brain_sha']}`",
                  f"- Brain diff: {md_cell(replay['brain_diff'])}"])
    lines.extend(["", "## 7. Run effort [committed subset]", "",
                  f"- Turns: {metrics['turns']}",
                  f"- Wall clock: {metrics['wall_clock_seconds']:.3f}s "
                  f"(preparation {metrics['preparation_seconds']:.3f}s)", ""])
    return "\n".join(lines)


def sanitized_review_source(ledger: dict[str, Any], run: dict[str, Any], totals: dict[str, int],
                            evaluation: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for index, score in enumerate(sorted(evaluation["holdouts"], key=lambda item: item["id"]), start=1):
        cases.append({"case": index, "scores": dict(score["scores"])})
    return {
        "schema_version": 1,
        "export_id": run["export_id"],
        "threads": ledger["corpus"]["threads"],
        "date_span": ledger["corpus"]["date_span"],
        "coverage": totals,
        "holdout": {"count": len(cases), "cases": cases},
        "run_metrics": metrics,
    }


def build_record_candidate(source: dict[str, Any], harvest_date: str,
                           kit_version: str) -> dict[str, Any]:
    return {"harvest_record": {
        "schema_version": 1,
        "harvest_date": harvest_date,
        "export_id": source["export_id"],
        "threads": source["threads"],
        "date_span": source["date_span"],
        "coverage": source["coverage"],
        "holdout": source["holdout"],
        "run_metrics": source["run_metrics"],
        "kit_version": kit_version,
    }}


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)


def publish_text_bundle(root: Path, outputs: dict[str, str]) -> None:
    """Stage outputs and publish a hash manifest last so torn bundles fail closed."""
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".review-stage-", dir=root))
    try:
        for name, content in outputs.items():
            (stage / name).write_text(content, encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "files": {name: hashlib.sha256(content.encode()).hexdigest()
                      for name, content in sorted(outputs.items())},
        }
        (stage / "bundle-manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # The manifest is the commit marker: record refuses any old/new mixture.
        for name in outputs:
            (stage / name).replace(root / name)
        (stage / "bundle-manifest.json").replace(root / "bundle-manifest.json")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def validate_review_bundle(root: Path) -> None:
    manifest = require_object(load_json(root / "bundle-manifest.json"),
                              "bundle-manifest.json", {"schema_version", "files"})
    if manifest["schema_version"] != 1:
        raise HarvestError("review bundle manifest schema must be 1")
    files = require_object(manifest["files"], "bundle-manifest.json.files",
                           {"review-brief.md", "record-source.json", "record-candidate.json"})
    for name, expected in files.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise HarvestError(f"review bundle manifest has invalid digest for {name}")
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise HarvestError("review bundle is incomplete or mixed; rerun review before approval")


def default_privacy_linter(path: Path) -> None:
    lint_path = Path(__file__).with_name("brain_lint.py")
    spec = importlib.util.spec_from_file_location("brain_harvest_privacy_lint", lint_path)
    if spec is None or spec.loader is None:
        raise HarvestError("cannot load brain_lint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    findings = module.lint_file(path, allow_rc_cli=False, rc_only=False, scratch=False)
    hard = [finding for finding in findings if finding[1] == "HARD"]
    if hard:
        categories = sorted({finding[2] for finding in hard})
        raise HarvestError(f"privacy linter rejected {path.name}: {categories}")


def validate_review_preflight(scratch: Path, run: dict[str, Any]) -> dict[str, Any]:
    preflight = validate_preflight(load_json(scratch / "preflight.json"),
                                   expected_export_id=run["export_id"])
    recorded_root = Path(preflight["repo_root"]).resolve()
    # synthetic_preflight is an explicit library/test fixture and never emitted by the CLI.
    if str(recorded_root) != "/synthetic/repo":
        try:
            scratch.relative_to(recorded_root)
        except ValueError as exc:
            raise HarvestError("scratch/preflight belongs to a different brain checkout") from exc
        current = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=scratch,
                                 text=True, capture_output=True)
        if current.returncode != 0 or Path(current.stdout.strip()).resolve() != recorded_root:
            raise HarvestError("preflight repo_root does not match the current brain checkout")
    binding = require_object(run.get("preflight"), "run.json.preflight",
                             {"sha256", "schema_version"})
    if binding["schema_version"] != preflight["schema_version"] \
            or binding["sha256"] != document_digest(preflight):
        raise HarvestError("preflight changed after prepare; rerun prepare before review")
    if run.get("target") != preflight["target"]:
        raise HarvestError("run target does not match canonical preflight target")
    return preflight


def cmd_review(args: argparse.Namespace,
               privacy_linter: Callable[[Path], None] = default_privacy_linter) -> int:
    scratch = Path(args.scratch).resolve()
    violations = verify_scratch(scratch)
    if violations:
        raise HarvestError("coverage ledger invariants failed before review:\n  " + "\n  ".join(violations))
    ledger = load_json(scratch / "ledger.json")
    clusters_doc = load_json(scratch / "clusters.json")
    manifest = load_manifest(scratch)
    run = load_json(scratch / "run.json")
    run = require_object(run, "run.json", {"schema_version", "generated_at", "export_id", "target",
                                            "preflight", "config", "inputs"})
    if run["schema_version"] != 1:
        raise HarvestError("run.json.schema_version must be 1")
    export_id = require_string(run["export_id"], "run.json.export_id")
    if not SAFE_EXPORT_ID_RE.fullmatch(export_id):
        raise HarvestError("run.json.export_id contains unsafe characters")
    validate_config(run["config"], "run.json.config")
    inputs = require_object(run["inputs"], "run.json.inputs", {"corpus_files", "corpus_sha256", "format",
                            "templates_format", "templates_count", "templates_sha256"})
    require_int(inputs["corpus_files"], "run.json.inputs.corpus_files", minimum=1)
    if not re.fullmatch(r"[0-9a-f]{64}", str(inputs["corpus_sha256"])):
        raise HarvestError("run.json.inputs.corpus_sha256 must be a lowercase SHA-256")
    formats = inputs["format"].split("+")
    if not formats or len(formats) != len(set(formats)) or not set(formats) <= {"v1", "v2", "v3"}:
        raise HarvestError("run.json.inputs.format must be a sorted combination of v1, v2, and v3")
    if inputs["templates_format"] not in ("", "v1"):
        raise HarvestError("run.json.inputs.templates_format must be blank or v1")
    require_int(inputs["templates_count"], "run.json.inputs.templates_count", minimum=0)
    if inputs["templates_sha256"] and not re.fullmatch(r"[0-9a-f]{64}", str(inputs["templates_sha256"])):
        raise HarvestError("run.json.inputs.templates_sha256 must be blank or a lowercase SHA-256")
    preflight = validate_review_preflight(scratch, run)
    if ledger.get("risk", {}).get("over_cap") is not False:
        raise HarvestError("risk.over_cap is true; prune marker rules and rerun prepare before fan-out")

    reports: list[dict[str, Any]] = []
    for report_path in args.agent_reports:
        reports.append(validate_agent_report(load_json(Path(report_path)), str(report_path)))
    reconcile_reports(ledger, clusters_doc, manifest, reports)
    holdout_ids = set(ledger["holdout"]["ids"])
    requested_holdouts = run["config"]["holdout_count"]
    if requested_holdouts < 1:
        raise HarvestError("review requires at least one configured holdout")
    if len(holdout_ids) != requested_holdouts:
        raise HarvestError("prepared holdout count does not match requested holdout_count; "
                           f"expected {requested_holdouts}, got {len(holdout_ids)}")
    replay_cases = validate_replay_cases(load_json(scratch / "replay-cases.json"), holdout_ids)
    holdout_doc = load_json(scratch / "holdout.json")
    if holdout_doc.get("replay_cases") != replay_cases["cases"]:
        raise HarvestError("replay-cases.json does not reconcile with prepared holdout.json")
    scan_explicit_synthesis_inputs(
        [*(Path(path) for path in args.agent_reports), Path(args.reduction)], replay_cases)
    scan_synthesis_holdout_leakage(scratch, holdout_ids, replay_cases)
    reduction = validate_reduction(load_json(Path(args.reduction)))
    reject_synthesis_holdout_leakage(reduction, holdout_ids, str(args.reduction))
    reconcile_reduction_evidence(reduction, manifest, ledger)
    reconcile_settings_verification(reduction, scratch, preflight)
    read_access = preflight["access"]["read"]
    write_access = preflight["access"]["write"]
    for index, change in enumerate(reduction["settings_changes"]):
        if change["status"] == "applied":
            surface = change["surface"]
            matrix_key = "hard_rules" if surface == "hard_rule" else surface
            verified_scopes = preflight["scope_matrix"][matrix_key]["available_scopes"]
            if not read_access[surface]:
                raise HarvestError(f"reduction.settings_changes[{index}] applied without verified current "
                                   "setting/target read; mark it pending")
            if change["scope"] not in verified_scopes:
                raise HarvestError(f"reduction.settings_changes[{index}] applied without a verified "
                                   f"{change['scope']} {surface} target/read; mark it pending")
            if not write_access[surface]:
                raise HarvestError(f"reduction.settings_changes[{index}] applied without verified write access; "
                                   "mark it pending")
    discovered_topics = {conflict["topic"] for report in reports for conflict in report["contradictions"]}
    reduced_topics = {conflict["topic"] for conflict in reduction["contradictions"]}
    if discovered_topics - reduced_topics:
        raise HarvestError("reduction does not resolve/surface every agent-reported contradiction: "
                           f"{sorted(discovered_topics - reduced_topics)}")
    evaluation = validate_evaluation(load_json(Path(args.evaluation)), holdout_ids)
    metrics = validate_metrics(load_json(Path(args.metrics)))
    diagnostics = validate_diagnostics_artifact(load_json(scratch / "diagnostics.json"))
    coverage_rows, totals = coverage_summary(ledger, clusters_doc)
    brief = build_review_brief(ledger, run, coverage_rows, totals, reports, reduction, evaluation,
                               metrics, diagnostics)
    source = sanitized_review_source(ledger, run, totals, evaluation, metrics)
    try:
        date.fromisoformat(args.harvest_date)
    except ValueError as exc:
        raise HarvestError("--harvest-date must be YYYY-MM-DD") from exc
    if not SAFE_VERSION_RE.fullmatch(args.kit_version):
        raise HarvestError("--kit-version must be a semantic version such as v0.3.0")
    candidate = build_record_candidate(source, args.harvest_date, args.kit_version)
    validate_record_source(source)
    candidate_encoded = json.dumps(candidate, indent=2, ensure_ascii=False) + "\n"
    if opaque_ids(candidate) or "@" in candidate_encoded or "http://" in candidate_encoded \
            or "https://" in candidate_encoded:
        raise HarvestError("generated record candidate failed privacy boundary (opaque id/contact/link)")
    brief_dir = scratch / "brief"
    linter_stage = Path(tempfile.mkdtemp(prefix=".candidate-lint-", dir=brief_dir))
    try:
        candidate_lint_path = linter_stage / "record-candidate.md"
        candidate_lint_path.write_text(candidate_encoded, encoding="utf-8")
        privacy_linter(candidate_lint_path)
    finally:
        shutil.rmtree(linter_stage, ignore_errors=True)
    publish_text_bundle(brief_dir, {
        "review-brief.md": brief,
        "record-source.json": json.dumps(source, indent=2, ensure_ascii=False) + "\n",
        "record-candidate.json": candidate_encoded,
    })
    validate_review_bundle(brief_dir)
    print(f"generated review brief, exact record candidate, and validated "
          f"{len(holdout_ids)} holdout score(s) in {brief_dir}")
    return 0


def validate_record_source(source: Any) -> dict[str, Any]:
    source = require_object(source, "record-source.json",
                            {"schema_version", "export_id", "threads", "date_span", "coverage",
                             "holdout", "run_metrics"})
    if source["schema_version"] != 1:
        raise HarvestError("record-source.json.schema_version must be 1")
    export_id = require_string(source["export_id"], "record-source.json.export_id")
    if not SAFE_EXPORT_ID_RE.fullmatch(export_id):
        raise HarvestError("record-source.json.export_id contains unsafe characters")
    require_int(source["threads"], "record-source.json.threads")
    span = require_list(source["date_span"], "record-source.json.date_span")
    if len(span) != 2 or any(item is not None and not isinstance(item, str) for item in span):
        raise HarvestError("record-source.json.date_span must contain two dates/nulls")
    for index, item in enumerate(span):
        if item is not None:
            try:
                date.fromisoformat(item)
            except ValueError as exc:
                raise HarvestError(f"record-source.json.date_span[{index}] must be YYYY-MM-DD/null") from exc
    coverage = require_object(source["coverage"], "record-source.json.coverage",
                              {"scanned", "assigned", "deep_read", "sampled", "noise_excluded",
                               "holdout", "rerouted"})
    for key, value in coverage.items():
        require_int(value, f"record-source.json.coverage.{key}")
    if coverage["scanned"] != source["threads"]:
        raise HarvestError("record-source threads and coverage.scanned differ")
    if coverage["scanned"] != coverage["assigned"] + coverage["noise_excluded"] + coverage["holdout"]:
        raise HarvestError("record-source primary coverage statuses do not reconcile")
    holdout = require_object(source["holdout"], "record-source.json.holdout", {"count", "cases"})
    count = require_int(holdout["count"], "record-source.json.holdout.count")
    cases = require_list(holdout["cases"], "record-source.json.holdout.cases")
    if len(cases) != count:
        raise HarvestError("record-source holdout count does not match cases")
    for index, case in enumerate(cases, start=1):
        case = require_object(case, f"record-source.json.holdout.cases[{index - 1}]", {"case", "scores"})
        if case["case"] != index:
            raise HarvestError("record-source holdout cases must be sequential sanitized ordinals")
        scores = require_object(case["scores"], f"record-source.json.holdout.cases[{index - 1}].scores",
                                {"factual_agreement", "routing", "tone"})
        for key, value in scores.items():
            require_int(value, f"record-source holdout case {index}.{key}", maximum=4)
    validate_metrics(source["run_metrics"], "record-source.json.run_metrics")
    if opaque_ids(source):
        raise HarvestError("record-source contains opaque thread ids")
    return source


def validate_record_destination(output: Path, scratch: Path,
                                git_runner: Callable[..., Any] = subprocess.run) -> None:
    top = git_runner(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if top.returncode != 0:
        raise HarvestError("record destination requires a git checkout")
    root = Path(top.stdout.strip()).resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise HarvestError(f"--out must be inside git root {root}") from exc
    ignored = git_runner(["git", "check-ignore", "-q", str(output)], cwd=root).returncode == 0
    if ignored:
        raise HarvestError("--out must not be ignored; harvest records are committed")
    try:
        output.relative_to(scratch)
    except ValueError:
        pass
    else:
        raise HarvestError("--out must be a tracked destination outside the sensitive scratch root")


def cmd_record(args: argparse.Namespace,
               destination_checker: Callable[[Path, Path], None] = validate_record_destination,
               privacy_linter: Callable[[Path], None] = default_privacy_linter) -> int:
    if not args.approved:
        raise HarvestError("refusing to generate a tracked harvest record without --approved")
    scratch = Path(args.scratch).resolve()
    validate_review_bundle(scratch / "brief")
    source = validate_record_source(load_json(scratch / "brief" / "record-source.json"))
    violations = verify_scratch(scratch)
    if violations:
        raise HarvestError("coverage ledger changed after review:\n  " + "\n  ".join(violations))
    ledger = load_json(scratch / "ledger.json")
    _, current_totals = coverage_summary(ledger, load_json(scratch / "clusters.json"))
    run = load_json(scratch / "run.json")
    validate_review_preflight(scratch, run)
    if (source["threads"] != ledger["corpus"]["threads"]
            or source["date_span"] != ledger["corpus"]["date_span"]
            or source["coverage"] != current_totals
            or source["export_id"] != run.get("export_id")):
        raise HarvestError("ledger/run changed after review; regenerate and re-approve the record candidate")
    candidate_path = scratch / "brief" / "record-candidate.json"
    candidate_text = candidate_path.read_text(encoding="utf-8")
    record = json.loads(candidate_text)
    record = require_object(record, "record-candidate.json", {"harvest_record"})
    body = require_object(record["harvest_record"], "record-candidate.json.harvest_record", {
        "schema_version", "harvest_date", "export_id", "threads", "date_span", "coverage",
        "holdout", "run_metrics", "kit_version",
    })
    try:
        date.fromisoformat(body["harvest_date"])
    except (TypeError, ValueError) as exc:
        raise HarvestError("record candidate harvest_date must be YYYY-MM-DD") from exc
    if not isinstance(body["kit_version"], str) or not SAFE_VERSION_RE.fullmatch(body["kit_version"]):
        raise HarvestError("record candidate kit_version must be semantic")
    expected = build_record_candidate(source, body["harvest_date"], body["kit_version"])
    if record != expected:
        raise HarvestError("record candidate no longer matches the machine-validated review source")
    encoded = json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
    if candidate_text != encoded:
        raise HarvestError("record candidate bytes changed after review; regenerate and re-approve")
    if opaque_ids(record) or "@" in encoded or "http://" in encoded or "https://" in encoded:
        raise HarvestError("generated record failed privacy boundary (opaque id/contact/link)")
    output = Path(args.out).resolve()
    destination_checker(output, scratch)
    if output.exists():
        existing = output.read_text(encoding="utf-8")
        if existing != candidate_text:
            raise HarvestError("refusing to replace a different existing harvest record")
        privacy_linter(output)
        print(f"sanitized approved harvest record already identical {output}")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    lint_stage = Path(tempfile.mkdtemp(prefix=".record-lint-", dir=output.parent))
    try:
        lint_path = lint_stage / "harvest-record.md"
        lint_path.write_text(candidate_text, encoding="utf-8")
        privacy_linter(lint_path)
    finally:
        shutil.rmtree(lint_stage, ignore_errors=True)
    atomic_write_text(output, candidate_text)
    privacy_linter(output)
    print(f"generated sanitized approved harvest record {output}")
    return 0


def apply_reports(scratch: Path, report_paths: list[Path]
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger = load_json(scratch / "ledger.json")
    clusters_doc = load_json(scratch / "clusters.json")
    threads = ledger["threads"]
    by_id = {c["id"]: c for c in clusters_doc["clusters"]}

    for path in report_paths:
        report = validate_agent_report(json.loads(path.read_text(encoding="utf-8")), str(path))
        holdout_ids = set(ledger["holdout"]["ids"])
        reject_synthesis_holdout_leakage(report, holdout_ids, str(path))
        referenced = opaque_ids(report)
        unknown = referenced - set(threads)
        if unknown:
            raise HarvestError(f"{path} references unknown ids {sorted(unknown)}")
        nonassigned = {tid for tid in referenced if threads[tid].get("status") != "assigned"}
        if nonassigned:
            raise HarvestError(f"{path} references non-synthesis ids {sorted(nonassigned)}")
        for tid in report.get("read_deep", []):
            threads[tid]["read"] = "deep"
        for tid in report.get("read_sampled", []):
            if threads[tid]["read"] != "deep":
                threads[tid]["read"] = "sampled"
        for move in report.get("route_elsewhere", []):
            tid, dest = move.get("id"), move.get("suggested_cluster")
            source = threads[tid]["cluster"]
            if source == dest:
                if threads[tid].get("routed_to") == dest:
                    continue  # exact report re-application is idempotent
                raise HarvestError(f"{path}: {tid} route destination equals current cluster")
            threads[tid]["cluster"] = dest
            threads[tid]["routed_to"] = dest
            if source in by_id:
                for key in ("thread_ids", "sample_ids", "deep_read_ids"):
                    if tid in by_id[source][key]:
                        by_id[source][key].remove(tid)
            if dest not in by_id:
                by_id[dest] = {"id": dest, "label": dest, "size": 0,
                               "thread_ids": [], "sample_ids": [], "deep_read_ids": []}
                clusters_doc["clusters"].append(by_id[dest])
            if tid not in by_id[dest]["thread_ids"]:
                by_id[dest]["thread_ids"].append(tid)
                by_id[dest]["thread_ids"].sort()

    for cluster in clusters_doc["clusters"]:
        cluster["size"] = len(cluster["thread_ids"])
    ledger["clusters"] = [{"id": c["id"], "size": c["size"]} for c in clusters_doc["clusters"]]
    return ledger, clusters_doc


def cmd_ledger_apply(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    ledger, clusters_doc = apply_reports(scratch, [Path(p) for p in args.reports])
    violations = verify_docs(read_manifest_ids(scratch), ledger, clusters_doc)
    if violations:  # nothing is persisted when the merged result would break invariants
        raise HarvestError("ledger invariants broken after apply:\n  " + "\n  ".join(violations))
    (scratch / "clusters.json").write_text(json.dumps(clusters_doc, indent=2, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    (scratch / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
    print(f"applied {len(args.reports)} report(s); coverage ledger OK")
    return 0


def cmd_ledger_expand(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    if args.count < 1:
        raise HarvestError("--count must be >= 1")
    report_path = scratch / "drafts" / f"{args.cluster}.report.json"
    report = validate_agent_report(load_json(report_path), str(report_path))
    if report["cluster"] != args.cluster or report["saturation"]["still_yielding"] is not True:
        raise HarvestError("plan expansion requires that cluster's still_yielding report")
    # Fold the triggering report into the staged documents first; exact re-entry is idempotent.
    ledger, clusters_doc = apply_reports(scratch, [report_path])
    if any(item.get("cluster") == args.cluster for item in ledger.get("followups", [])):
        raise HarvestError(f"cluster {args.cluster} already used its one follow-up expansion")
    cluster = next((item for item in clusters_doc["clusters"] if item["id"] == args.cluster), None)
    if cluster is None:
        raise HarvestError(f"unknown cluster {args.cluster}")
    planned = set(ledger["reading_plan"]["deep_read_ids"]) | set(ledger["reading_plan"]["sample_ids"])
    remaining = sorted(thread_id for thread_id in cluster["thread_ids"]
                       if ledger["threads"][thread_id]["status"] == "assigned"
                       and thread_id not in planned)
    selected = remaining[:args.count]
    if not selected:
        raise HarvestError(f"cluster {args.cluster} has no remaining assigned ids to expand")
    cluster["sample_ids"] = sorted(set(cluster["sample_ids"]) | set(selected))
    ledger["reading_plan"]["sample_ids"] = sorted(set(ledger["reading_plan"]["sample_ids"])
                                                    | set(selected))
    followups = ledger.setdefault("followups", [])
    followups.append({"cluster": args.cluster, "ordinal": 1, "count": len(selected),
                      "ids": selected, "trigger": "still_yielding"})
    violations = verify_docs(read_manifest_ids(scratch), ledger, clusters_doc)
    if violations:
        raise HarvestError("ledger invariants broken after expansion:\n  " + "\n  ".join(violations))
    stage = Path(tempfile.mkdtemp(prefix=".ledger-expand-", dir=scratch))
    try:
        (stage / "clusters.json").write_text(json.dumps(clusters_doc, indent=2,
                                                        ensure_ascii=False) + "\n", encoding="utf-8")
        (stage / "ledger.json").write_text(json.dumps(ledger, indent=2,
                                                       ensure_ascii=False) + "\n", encoding="utf-8")
        for name in ("clusters.json", "ledger.json"):
            (stage / name).replace(scratch / name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(f"expanded {args.cluster} follow-up plan by {len(selected)} id(s)")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch).resolve()
    if not args.yes:
        raise HarvestError("refusing to delete the scratch root without --yes")
    # Deleting is irreversible: only remove a directory prepare actually populated.
    if scratch.exists() and not any((scratch / marker).exists()
                                    for marker in ("manifest.jsonl", "corpus")):
        raise HarvestError(f"{scratch} does not look like a harvest scratch root; refusing to delete")
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        raise HarvestError(f"cleanup failed; {scratch} still exists")
    print(f"removed {scratch}")
    return 0


def default_rc_runner(argv: list[str]) -> tuple[int, str, str] | None:
    try:
        completed = subprocess.run([_cli(), *argv], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode, completed.stdout, completed.stderr


def find_mailbox_inventory_record(raw: str, mailbox: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def objects(item: Any) -> Iterable[dict[str, Any]]:
        if isinstance(item, dict):
            yield item
            for nested in item.values():
                yield from objects(nested)
        elif isinstance(item, list):
            for nested in item:
                yield from objects(nested)

    wanted = mailbox.casefold()
    for item in objects(value):
        for key in ("id", "mailbox_id", "mailboxId"):
            if isinstance(item.get(key), str) and item[key].casefold() == wanted:
                return item
    return None


def inventory_provider(record: dict[str, Any]) -> str:
    for key in ("provider", "provider_type", "providerType", "kind"):
        value = record.get(key)
        if isinstance(value, str):
            return value.casefold()
        if isinstance(value, dict):
            for nested_key in ("name", "type", "kind"):
                if isinstance(value.get(nested_key), str):
                    return value[nested_key].casefold()
    return ""


def recursive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from recursive_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from recursive_strings(nested)


def normalize_capabilities(raw: str) -> list[str]:
    """Normalize JSON or text `rp auth access` output to stable lowercase capability names."""
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    found: set[str] = set()
    for text_value in recursive_strings(value):
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]*:(?:\*|[A-Za-z0-9_.-]+)", text_value):
            found.add(token.casefold())
    return sorted(found)


def capability_allows(capabilities: Iterable[str], permission: str) -> bool:
    capabilities = set(capabilities)
    namespace = permission.split(":", 1)[0]
    return ("admin:*" in capabilities or "config:write" in capabilities
            or permission in capabilities or f"{namespace}:*" in capabilities)


def find_object_with_identity(raw: str, expected: dict[str, str]) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    aliases = {
        "export_id": ("export_id", "exportId", "id"),
        "project": ("project", "project_id", "projectId"),
        "tenant": ("tenant", "tenant_id", "tenantId", "tenant_slug", "tenantSlug"),
        "mailbox": ("mailbox", "mailbox_id", "mailboxId"),
        "provider": ("provider", "provider_type", "providerType"),
    }
    candidates = [item for item in _walk_objects(value)]
    for item in candidates:
        matches = True
        for field_name, wanted in expected.items():
            if not wanted:
                continue
            actual = next((item.get(key) for key in aliases[field_name]
                           if isinstance(item.get(key), str)), None)
            if actual is None:
                # The server omits some identity keys (e.g. corpus get lacks project/provider);
                # an absent key cannot contradict the target. Only a present, differing value fails.
                if field_name == "export_id":
                    matches = False
                    break
                continue
            if actual.casefold() != wanted.casefold():
                matches = False
                break
        if matches:
            return item
    return None


def _walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_objects(nested)


def validate_preflight(value: Any, *, expected_export_id: str = "") -> dict[str, Any]:
    value = require_object(value, "preflight.json", {
        "schema_version", "repo_root", "target", "access", "verification", "scope_matrix",
        "corpus", "checks", "result",
    })
    if value["schema_version"] != 3 or value["result"] not in {"pass", "pass_with_warnings"}:
        raise HarvestError("preflight must be schema v3 without failed checks")
    require_string(value["repo_root"], "preflight.json.repo_root")
    target = require_object(value["target"], "preflight.json.target",
                            {"project", "tenant", "mailbox", "provider", "export_id"})
    for key in target:
        require_string(target[key], f"preflight.json.target.{key}", allow_empty=key == "tenant")
    if expected_export_id and target["export_id"] != expected_export_id:
        raise HarvestError("preflight export does not match requested export")
    verification = require_object(value["verification"], "preflight.json.verification",
                                  {"auth", "access", "mailbox", "provider", "export"})
    if any(flag is not True for flag in verification.values()):
        raise HarvestError("preflight target/auth/access/export verification is incomplete")
    access = require_object(value["access"], "preflight.json.access",
                            {"verified", "capabilities", "read", "write"})
    if access["verified"] is not True:
        raise HarvestError("preflight access is not verified")
    capabilities = require_list(access["capabilities"], "preflight.json.access.capabilities")
    if capabilities != sorted(set(capabilities)):
        raise HarvestError("preflight capabilities must be normalized and sorted")
    for permission_kind in ("read", "write"):
        permissions = require_object(access[permission_kind],
                                     f"preflight.json.access.{permission_kind}",
                                     {"persona", "triage_policy", "hard_rule"})
        if any(not isinstance(flag, bool) for flag in permissions.values()):
            raise HarvestError(f"preflight {permission_kind} access values must be booleans")
    matrix = require_object(value["scope_matrix"], "preflight.json.scope_matrix",
                            {"persona", "triage_policy", "hard_rules", "brain_facts"})
    surface_map = {"persona": "persona", "triage_policy": "triage_policy",
                   "hard_rules": "hard_rule"}
    allowed_scopes = {"persona": {"mailbox", "tenant", "project"},
                      "triage_policy": {"tenant", "project"},
                      "hard_rules": {"tenant", "project"}}
    for matrix_key, permission_key in surface_map.items():
        required = {"available_scopes", "narrowest_target", "target_available", "write_verified"}
        if matrix_key != "persona":
            required.add("mailbox_scope")
        entry = require_object(matrix[matrix_key], f"preflight.json.scope_matrix.{matrix_key}", required)
        scopes = require_list(entry["available_scopes"],
                              f"preflight.json.scope_matrix.{matrix_key}.available_scopes")
        if len(scopes) != len(set(scopes)) or not set(scopes) <= allowed_scopes[matrix_key]:
            raise HarvestError(f"preflight {matrix_key} available scopes are invalid")
        if entry["target_available"] is not (entry["narrowest_target"] in scopes):
            raise HarvestError(f"preflight {matrix_key} target availability does not match verified scopes")
        if entry["write_verified"] is not access["write"][permission_key]:
            raise HarvestError(f"preflight {matrix_key} write verification is inconsistent")
        if access["read"][permission_key] is not bool(scopes):
            raise HarvestError(f"preflight {matrix_key} read verification is inconsistent")
        if matrix_key != "persona" and entry["mailbox_scope"] is not False:
            raise HarvestError(f"preflight {matrix_key} must not claim mailbox scope")
        require_string(entry["narrowest_target"],
                       f"preflight.json.scope_matrix.{matrix_key}.narrowest_target")
    require_object(matrix["brain_facts"], "preflight.json.scope_matrix.brain_facts",
                   {"available_scopes", "narrowest_target"})
    require_object(value["corpus"], "preflight.json.corpus", {"files", "formats"})
    require_list(value["checks"], "preflight.json.checks")
    return value


def cmd_preflight(args: argparse.Namespace,
                  rc_runner: Callable[[list[str]], tuple[int, str, str] | None] | None = None,
                  git_runner: Callable[..., Any] = subprocess.run) -> int:
    scratch = Path(args.scratch).resolve()
    rc_runner = rc_runner or default_rc_runner
    checks: list[tuple[str, str, str]] = []
    check_records: list[dict[str, Any]] = []
    gitignored = False
    repo_root = Path()

    def add(level: str, label: str, detail: str, *, exit_code: int | None = None) -> None:
        checks.append((level, label, detail))
        record: dict[str, Any] = {"name": label, "status": level.lower(), "detail": detail}
        if exit_code is not None:
            record["exit_code"] = exit_code
        check_records.append(record)

    top = git_runner(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if top.returncode != 0:
        add("FAIL", "git checkout", "not inside a git checkout", exit_code=top.returncode)
    else:
        repo_root = Path(top.stdout.strip()).resolve()
        add("OK", "git checkout", "inside a git checkout", exit_code=0)
        probe = scratch / ".harvest-ignore-check"
        ignored_result = git_runner(["git", "check-ignore", "-q", str(probe)], cwd=repo_root)
        gitignored = ignored_result.returncode == 0
        add("OK" if gitignored else "FAIL", "scratch gitignored",
            "scratch is ignored" if gitignored else "scratch is stageable; add it to .gitignore",
            exit_code=ignored_result.returncode)
        status = git_runner(["git", "status", "--short", "--branch"], cwd=repo_root,
                            text=True, capture_output=True)
        if status.returncode == 0:
            lines = [line for line in getattr(status, "stdout", "").splitlines() if line.strip()]
            changes = sum(not line.startswith("##") for line in lines)
            add("OK", "git state", f"readable; {changes} changed path(s)", exit_code=0)
        else:
            add("WARN", "git state", f"unreadable (exit {status.returncode})",
                exit_code=status.returncode)

    corpus_dir = scratch / "corpus"
    files = [f for f in corpus_dir.glob("*") if f.suffix in (".md", ".txt")] if corpus_dir.is_dir() else []
    if not files:
        formats: set[str] = set()
        add("WARN", "corpus format", "no corpus files acquired yet")
    else:
        formats = set()
        for path in files:
            try:
                meta, _ = parse_front_matter(path.read_bytes().decode("utf-8", "replace"))
                formats.add(meta.get("harvest_format", "?"))
            except HarvestError:
                formats.add("unparseable")
        unsupported = formats - {"v1", "v2", "v3"}
        add("FAIL" if unsupported else "OK", "corpus format",
            f"{len(files)} file(s), formats {sorted(formats)}")

    project = getattr(args, "project", None) or ""
    tenant = getattr(args, "tenant", None) or ""
    mailbox = getattr(args, "mailbox", None) or ""
    provider = (getattr(args, "provider", None) or "").lower()
    export_id = getattr(args, "export_id", None) or ""
    if provider and provider not in {"google", "microsoft", "imap"}:
        add("FAIL", "target provider", "provider must be google, microsoft, or imap")

    inventory: list[tuple[str, list[str]]] = [
        ("rp auth", ["auth", "status"]),
        ("rp access", ["auth", "access"]),
        ("rp mailbox inventory", ["project", "mailbox", "ls", "-o", "json"]),
        ("rp persona settings", ["project", "settings", "behavior", "get", "-o", "json"]),
        ("rp triage policy", ["project", "triage", "policy", "get", "-o", "json"]),
        ("rp hard rules", ["project", "triage", "rules", "ls", "-o", "json"]),
        ("rp grounding databases", ["dev", "console", "database", "list", "-o", "json"]),
        ("rp capabilities", ["dev", "console", "capabilities"]),
        ("rp mirror health", ["fleet", "health"]),
        ("rp corpus history", ["project", "corpus", "ls", "-o", "json"]),
        ("rp doctor", ["self", "doctor"]),
    ]
    if mailbox:
        inventory.append(("rp mailbox persona", ["project", "mailbox", "settings", "get",
                                                  mailbox, "-o", "json"]))
    if tenant:
        inventory.append(("rp tenant settings", ["project", "tenant", "settings", "get",
                                                 tenant, "-o", "json"]))
    if export_id:
        inventory.append(("rp export", ["project", "corpus", "get", export_id]))

    project_prefix = ["--project", project] if project else []
    tenant_prefix = [*project_prefix, "--tenant", tenant] if tenant else project_prefix
    inventory_outputs: dict[str, str] = {}
    inventory_codes: dict[str, int | None] = {}
    explicit_target = bool(project or tenant or mailbox or provider or export_id)
    critical = {"rp auth", "rp access"}
    if explicit_target:
        critical.update({"rp persona settings", "rp triage policy", "rp hard rules"})
    if mailbox or provider:
        critical.add("rp mailbox inventory")
    if mailbox:
        critical.add("rp mailbox persona")
    if tenant:
        critical.add("rp tenant settings")
    if export_id:
        critical.add("rp export")
    for label, argv in inventory:
        scoped_prefix = tenant_prefix if tenant and label in {
            "rp mailbox inventory", "rp triage policy", "rp hard rules", "rp grounding databases",
            "rp capabilities", "rp mirror health", "rp corpus history", "rp export",
        } else project_prefix
        result = rc_runner([*scoped_prefix, *argv])
        if result is None:
            inventory_codes[label] = None
            add("FAIL" if explicit_target and label in critical else "WARN", label,
                "rp not available; cannot verify explicit target" if explicit_target and label in critical
                else "rp not available; complete manually before fan-out")
        else:
            code, out, err = result
            inventory_codes[label] = code
            inventory_outputs[label] = out
            add("OK" if code == 0 else "FAIL" if explicit_target and label in critical else "WARN", label,
                "read succeeded" if code == 0 else f"read failed (exit {code})", exit_code=code)

    mailbox_output = inventory_outputs.get("rp mailbox inventory", "")
    mailbox_record = find_mailbox_inventory_record(mailbox_output, mailbox) if mailbox else None
    if mailbox:
        add("OK" if mailbox_record else "FAIL", "target mailbox",
            "mailbox present in inventory" if mailbox_record else "mailbox absent from inventory")
    if provider:
        provider_matches = bool(mailbox_record and inventory_provider(mailbox_record) == provider)
        add("OK" if provider_matches else "FAIL", "target provider",
            "provider matches target mailbox" if provider_matches else
            "provider not confirmed by mailbox inventory")

    capabilities = normalize_capabilities(inventory_outputs.get("rp access", ""))
    access_verified = inventory_codes.get("rp access") == 0 and bool(capabilities)
    if explicit_target and not access_verified:
        add("FAIL", "verified access", "auth access returned no recognized capabilities")
    write_access = {
        "persona": capability_allows(capabilities, "persona:write"),
        "triage_policy": capability_allows(capabilities, "triage:write"),
        "hard_rule": capability_allows(capabilities, "triage:write"),
    }
    persona_scopes = []
    if inventory_codes.get("rp mailbox persona") == 0:
        persona_scopes.append("mailbox")
    if inventory_codes.get("rp persona settings") == 0:
        persona_scopes.append("project")
    if inventory_codes.get("rp tenant settings") == 0:
        persona_scopes.insert(1 if persona_scopes and persona_scopes[0] == "mailbox" else 0, "tenant")
    resolved_settings_scope = "tenant" if tenant else "project"
    triage_scopes = [resolved_settings_scope] if inventory_codes.get("rp triage policy") == 0 else []
    hard_rule_scopes = [resolved_settings_scope] if inventory_codes.get("rp hard rules") == 0 else []
    read_access = {
        "persona": bool(persona_scopes),
        "triage_policy": bool(triage_scopes),
        "hard_rule": bool(hard_rule_scopes),
    }

    export_verified = False
    if export_id and inventory_codes.get("rp export") == 0:
        expected = {"export_id": export_id, "project": project, "tenant": tenant, "mailbox": mailbox,
                    "provider": provider}
        export_verified = find_object_with_identity(inventory_outputs.get("rp export", ""), expected) is not None
        add("OK" if export_verified else "FAIL", "target export",
            "export metadata matches exact target" if export_verified else
            "export metadata did not prove export/project/mailbox/provider binding")

    scope_matrix = {
        "persona": {"available_scopes": persona_scopes,
                    "narrowest_target": resolved_settings_scope,
                    "target_available": resolved_settings_scope in persona_scopes,
                    "write_verified": write_access["persona"]},
        "triage_policy": {"available_scopes": triage_scopes,
                          "narrowest_target": resolved_settings_scope, "mailbox_scope": False,
                          "target_available": resolved_settings_scope in triage_scopes,
                          "write_verified": write_access["triage_policy"]},
        "hard_rules": {"available_scopes": hard_rule_scopes,
                       "narrowest_target": resolved_settings_scope, "mailbox_scope": False,
                       "target_available": resolved_settings_scope in hard_rule_scopes,
                       "write_verified": write_access["hard_rule"]},
        "brain_facts": {"available_scopes": ["tenant", "project"],
                        "narrowest_target": "business_scope"},
    }
    artifact = {
        "schema_version": 3,
        "repo_root": str(repo_root),
        "target": {"project": project, "tenant": tenant, "mailbox": mailbox, "provider": provider,
                   "export_id": export_id},
        "access": {"verified": access_verified, "capabilities": capabilities,
                   "read": read_access,
                   "write": write_access},
        "verification": {
            "auth": inventory_codes.get("rp auth") == 0,
            "access": access_verified,
            "mailbox": bool(mailbox_record),
            "provider": bool(provider and mailbox_record
                             and inventory_provider(mailbox_record) == provider),
            "export": export_verified,
        },
        "scope_matrix": scope_matrix,
        "corpus": {"files": len(files), "formats": sorted(formats)},
        "checks": check_records,
        "result": "fail" if any(level == "FAIL" for level, _, _ in checks) else "pass_with_warnings"
                  if any(level == "WARN" for level, _, _ in checks) else "pass",
    }
    if gitignored:
        scratch.mkdir(parents=True, exist_ok=True)
        atomic_write_text(scratch / "preflight.json",
                          json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")

    for level, label, detail in checks:
        print(f"[{level:4}] {label}: {detail}")
    if gitignored:
        print(f"wrote private machine-readable preflight artifact {scratch / 'preflight.json'}")
    return 1 if any(level == "FAIL" for level, _, _ in checks) else 0


# --- CLI ------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="local + best-effort rc environment checks")
    pre.add_argument("--scratch", required=True)
    pre.add_argument("--project", help="explicit rp project context")
    pre.add_argument("--tenant", help="explicit tenant slug for tenant-scoped inventory")
    pre.add_argument("--mailbox", help="explicit mailbox id to validate and scope")
    pre.add_argument("--provider", choices=("google", "microsoft", "imap", "intercom"),
                     help="expected provider for --mailbox")
    pre.add_argument("--export-id", help="existing export id to inventory")

    prep = sub.add_parser("prepare", help="parse corpus into the opaque manifest/ledger scratch root")
    prep.add_argument("--corpus", required=True, help="raw corpus file or directory of files")
    prep.add_argument("--templates", help="separate Gmail templates/v1 JSON artifact")
    prep.add_argument("--scratch", required=True)
    prep.add_argument("--config", help="JSON file overriding DEFAULTS knobs")
    prep.add_argument("--holdout", type=int, help="override holdout_count")
    prep.add_argument("--seed", type=int, help="override sampling/holdout tie-break seed")
    prep.add_argument("--export-id", default="",
                      help="export job handle persisted privately for review/record (required by review)")

    ver = sub.add_parser("verify", help="check coverage-ledger invariants")
    ver.add_argument("--scratch", required=True)

    ledger = sub.add_parser("ledger", help="ledger operations")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    apply = ledger_sub.add_parser("apply", help="merge agent coverage reports and re-verify")
    apply.add_argument("--scratch", required=True)
    apply.add_argument("reports", nargs="+", help="drafts/<cluster>.report.json files")
    expand = ledger_sub.add_parser("expand", help="expand one still-yielding cluster plan once")
    expand.add_argument("--scratch", required=True)
    expand.add_argument("--cluster", required=True)
    expand.add_argument("--count", required=True, type=int)

    review = sub.add_parser("review", help="validate step-10 inputs and generate the operator brief")
    review.add_argument("--scratch", required=True)
    review.add_argument("--agent-report", dest="agent_reports", action="extend", nargs="+", required=True,
                        help="strict cluster report JSON file(s); provide every non-empty cluster")
    review.add_argument("--reduction", required=True,
                        help="strict reduction JSON: settings_changes/skip_proposals/durable_rules/contradictions")
    review.add_argument("--evaluation", required=True,
                        help="strict holdout scores plus production_replay metadata JSON")
    review.add_argument("--metrics", required=True,
                        help="strict turns/wall_clock_seconds/preparation_seconds JSON")
    review.add_argument("--harvest-date", required=True,
                        help="YYYY-MM-DD used in the exact tracked-safe record candidate")
    review.add_argument("--kit-version", required=True,
                        help="semantic kit version used in the exact tracked-safe record candidate")

    record = sub.add_parser("record", help="copy the reviewed tracked-safe candidate after approval")
    record.add_argument("--scratch", required=True)
    record.add_argument("--out", required=True, help="tracked destination outside scratch (JSON)")
    record.add_argument("--approved", action="store_true",
                        help="confirm the mandatory operator diff/candidate approval occurred")

    clean = sub.add_parser("cleanup", help="delete the scratch root after operator approval")
    clean.add_argument("--scratch", required=True)
    clean.add_argument("--yes", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "prepare":
            return cmd_prepare(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "ledger" and args.ledger_command == "apply":
            return cmd_ledger_apply(args)
        if args.command == "ledger" and args.ledger_command == "expand":
            return cmd_ledger_expand(args)
        if args.command == "review":
            return cmd_review(args)
        if args.command == "record":
            return cmd_record(args)
        if args.command == "cleanup":
            return cmd_cleanup(args)
    except (HarvestError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare-harvest: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
