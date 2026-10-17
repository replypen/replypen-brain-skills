# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic privacy + brain-contract linter for `brain-harvest`. Scans brain Markdown for leaked
secrets, raw thread text, payment links/addresses, contact details (email/phone), order/invoice/
tracking/account identifiers, harvest-scratch leakage (opaque IDs, raw filenames, scratch paths),
counterparty names, and soft contract smells; all supported brain text files are scanned for
local-only rp CLI commands.

Stdlib only: run it with `uv run --no-project python brain_lint.py` or plain `python3 brain_lint.py`.
It is a pre-commit gate, not a formatter — it never edits files.

    python3 brain_lint.py                 # scan STAGED *.md (git diff --cached), the pre-commit gate
    python3 brain_lint.py --all           # scan every tracked/untracked *.md under the tree
    python3 brain_lint.py notes/ x.md     # scan explicit files/dirs
    python3 brain_lint.py --strict        # soft (contract) findings also fail the run
    python3 brain_lint.py --scratch p/     # scratch mode: suppress harvest opaque-ID/filename classes
    python3 brain_lint.py --selftest      # run built-in regex self-checks (no repo needed)

`--scratch` is for linting the harvest scratch root itself (ignored paths passed explicitly): that
tree is *expected* to contain opaque IDs (`H` + 32 lowercase hex; legacy `H000001`) and raw
`YYYY-MM--slug--n.md` filenames, so those
two classes are suppressed there while raw-thread/secret/payment/identifier/name classes still apply
(name findings downgrade from HARD to SOFT in scratch mode, per spec §7). Default mode
(staged/tracked/--all) enables every class — nothing opaque may reach a tracked brain file.

Run-hidden trees are never scanned, in any mode: paths excluded by `.replypenignore` / `.rcignore`
(plus `_internal/`) are physically absent from every production run, so the maintainer-only rp CLI
guidance and operator material living there is exactly where it belongs.

Exit status: 1 if any HARD finding is present, or if `--strict` and any SOFT finding is present; else
0. Findings print grep-style: `path:line: <category>: <snippet>`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ── categories ────────────────────────────────────────────────────────────────────────────────────
# HARD = data, secrets, or unavailable runtime instructions that must never land in a brain; blocks.
# SOFT = brain-contract smell (response mechanics / persona / channel wording) that belongs in persona
#        settings, not brain files; a warning unless --strict. See docs/brain-model.md prompt boundary.
HARD = "HARD"
SOFT = "SOFT"

# ── HARD: credentials / secrets ─────────────────────────────────────────────────────────────────
# High-precision provider token shapes plus a couple of generic ones. Deliberately narrow so a green
# lint is meaningful: we want near-zero false positives on prose, not a maximal secret scanner.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Interior hyphens for modern project/service keys (sk-proj-…, sk-svcacct-…, sk-ant-…).
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("github-token", re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{20,}\b")),
    # Inline credentials in a connection URL: postgres://user:pass@host/db, mongodb://…, redis://…
    # A masked password (`user:***@`, `user:xxx@`, `user:<pass>@`) is documentation, not a credential.
    ("db-url-credential", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:(?!\*+@|x{3,}@|<)[^\s/@]+@")),
    # Value assignment to a secret-ish key. Skip obvious placeholders so example/instructional prose
    # ("password: <your-password>", "token=xxx", "secret=***") does not hard-block a legit commit,
    # and skip env-var lookups (`api_key = os.environ[…]`, `token=$TOKEN`, `ENV["…"]`) — those are
    # the *sanctioned* way to reference a secret, not a leaked value.
    ("password-assign", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*"
        r"(?!<|x{3,}\b|\*{3,}|\.{3}|your[_-]|placeholder\b|redacted\b|example\b"
        r"|`?(?:os\.environ|process\.env|ENV\[|\$))\S{6,}")),
    # Long high-entropy base64-ish blob (>=40 chars) — likely a raw key/token pasted from a thread.
    # Require a base64 signal char (+ or =) OR a mixed-case+digit shape, so plain-hex git SHAs/digests
    # (all lowercase, no +/=) and slash-separated route/slug paths (cases/billing/refunds/…) do NOT
    # hard-fail a legit commit; specific provider patterns above still catch real hex-ish keys by prefix.
    # `/` is deliberately excluded from the char class — a path breaks into short non-matching tokens.
    ("high-entropy-blob", re.compile(
        r"\b(?=[A-Za-z0-9+]{40,}={0,2}\b)"
        r"(?:[A-Za-z0-9+]*[+=]|(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*\d))"
        r"[A-Za-z0-9+]{40,}={0,2}\b")),
]

# ── raw-thread shape: HARD plumbing + SOFT blockquote ───────────────────────────────────────────
# Verbatim email plumbing that means someone pasted a raw thread instead of distilling it.
RAWTHREAD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("on-x-wrote", re.compile(r"(?i)^\s*On .+ wrote:\s*$")),
    ("mail-header", re.compile(r"(?i)^\s*(?:From|To|Cc|Bcc|Sent|Reply-To|Date|Subject)\s*:\s*\S")),
    ("forwarded-block", re.compile(r"(?i)-{3,}\s*(?:Forwarded message|Original Message)\s*-{3,}")),
]
# A lone `>` line is legitimate Markdown (callouts, symptom quotes in cases/skills docs) far more
# often than it is a pasted reply — full-tree scans of mature brains showed hundreds of blockquote
# callouts and zero raw threads. It stays a signal (SOFT: warns; fatal under --strict, which the
# harvest gate runs), but per this file's near-zero-FP rule it can no longer be HARD on its own —
# a real pasted thread still hard-fails via mail headers, "On … wrote:", names, and identifiers.
RAWTHREAD_SOFT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("quoted-reply", re.compile(r"^\s*>")),
]

# ── HARD: payment links ─────────────────────────────────────────────────────────────────────────
# Precise, low-false-positive shapes: a pay link or an IBAN is real PII/financial data, never a
# distilled pattern. (Street addresses are handled as SOFT below — that heuristic is coarse.)
PAYMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("stripe-pay-link", re.compile(r"(?i)\b(?:checkout\.)?stripe\.com/pay\b")),
    ("paypal-me", re.compile(r"(?i)\bpaypal\.me/\S+")),
    # Real IBANs are all-caps + digits. Restricting the body/tail groups to [A-Z0-9] stops the pattern
    # greedily eating a trailing lowercase English word (e.g. "US12 ABCD 1234 for" / "AB12 CDEF 3456 GHIJ").
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,3}\b")),
]

# ── HARD: high-precision identifiers ─────────────────────────────────────────────────────────────
# Order/invoice/tracking/account references in a *high-precision* shape are real customer data, not a
# distilled rule. Kept HARD only where the shape is near-unmistakable: a carrier tracking format, or an
# explicit prefix keyword followed (across a mandatory separator) by a long mixed letter+digit token —
# the signature of a real order/invoice/account code. Coarse "order #12345"-style mentions are far more
# ambiguous and live in the SOFT group below, per this file's near-zero-false-positive rule for HARD.
IDENTIFIER_HARD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # UPS: literal 1Z + 16 uppercase alnum. Case-sensitive, so it will not eat lowercase prose.
    ("carrier-tracking-id", re.compile(r"\b1Z[0-9A-Z]{16}\b")),
    # <prefix><sep(s)><token>, token >=8 chars mixing letters and digits. The mandatory separator kills
    # English false positives: "accountability2024…" cannot match — no separator follows the keyword.
    ("order-invoice-id", re.compile(
        r"(?i)\b(?:order|invoice|inv|ord|po|ref|account|acct)[ #:\-]+"
        r"(?=[A-Za-z0-9\-]*[A-Za-z])(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]{7,}\b")),
]

# ── HARD: harvest scratch leakage ────────────────────────────────────────────────────────────────
# Opaque harvest thread IDs (`H` + 32 lowercase hex; legacy H000001), the splitter's
# `YYYY-MM--slug--n.md` filename shape, and the
# `.replypen/harvest`|`.replypen/exports` scratch/export path fragments must never reach a tracked
# brain file. All three shapes are highly specific (near-zero FP), so they are HARD in the default
# (tracked/staged) mode. In `--scratch` mode they are SUPPRESSED: the harvest scratch root is *expected*
# to be full of these, and flagging them there would only train `--no-verify` bypass on legit content.
HARVEST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("harvest-opaque-id", re.compile(r"\bH(?:\d{6}|[0-9a-f]{32})\b")),
    ("harvest-filename", re.compile(r"(?i)\b\d{4}-\d{2}--[a-z0-9][a-z0-9\-]*--\d+\.md\b")),
    ("harvest-scratch-path", re.compile(r"(?i)\.(?:replypen|rootcause)/(?:harvest|exports)/")),
]

# ── SOFT: contact details (email / phone) ────────────────────────────────────────────────────────
# A brain legitimately references its OWN routing addresses ("route billing to billing@…") and support
# lines, so a blanket HARD here would fire on correct content and break the near-zero-FP rule for HARD
# (which trains bypass). These are SOFT: surfaced for review, blocking only under --strict (the harvest
# gate). The phone shape is deliberately narrow — an intl `+` prefix, a parenthesised area code, or a
# 3-3-4 dashed group — so it does NOT fire on prices ($1,299.00), dates (2026-07-19), IBAN-adjacent
# 4-4-2 numerics, or dotted version numbers (3.11.6).
CONTACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email-address", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("phone-number", re.compile(
        r"(?<![\w+])(?:"
        r"\+\d{1,3}[ .\-]?(?:\(?\d{1,4}\)?[ .\-]?){2,4}\d{2,4}"
        r"|\(\d{3}\)[ .\-]?\d{3}[ .\-]?\d{4}"
        r"|\d{3}[ .\-]\d{3}[ .\-]\d{4}"
        r")(?!\d)")),
]

# ── SOFT: coarse identifier mentions ─────────────────────────────────────────────────────────────
# The ambiguous half of the identifier axis: "order #12345", "invoice number 7788". Requiring an
# explicit number word (#/no/nr/number/id) keeps it off SKU-ish prose like "Order US12 ABCD 1234".
IDENTIFIER_SOFT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("order-ref-coarse", re.compile(
        r"(?i)\b(?:order|invoice|ticket|case|ref(?:erence)?|account|acct|po|tracking)\s*"
        r"(?:#|no\.?|nr\.?|number|id)\s*[:#]?\s*\w{3,}\b")),
]

# ── counterparty names: HARD in tracked scans, SOFT in --scratch ─────────────────────────────────
# Severity is mode-split per spec §7 (brain-harvest-long-horizon-v2): names are a HARD failure in the
# tracked diff, but a soft warning only when scanning scratch proposals — name detection is an NER
# problem in a regex linter, and a hard gate on scratch (where greetings legitimately appear mid-
# distillation) would train `--no-verify` bypass. What makes HARD defensible in tracked mode is the
# deliberately narrow shape: an honorific + capitalised name, or a "Dear <Name>" salutation with
# generic-word exclusions. Do not broaden these — precision is the HARD licence.
NAME_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("counterparty-name", re.compile(
        r"\b(?:Mr|Mrs|Ms|Mx|Dr|Prof|Sir|Madam|Mme|Mlle|Herr|Frau|Dhr|Mevr)\.?\s+[A-Z][a-z]+")),
    ("greeting-name", re.compile(
        r"\b(?:Dear|Beste|Geachte)\s+"
        r"(?!Customer\b|Team\b|Support\b|Sir\b|Madam\b|All\b|There\b|Folks\b|Everyone\b|Valued\b"
        r"|Suppliers?\b|Vendors?\b|Partners?\b|Colleagues?\b|Guests?\b|Sirs\b)"
        r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?")),
]

# ── SOFT: response-mechanics / persona wording + coarse address heuristic ────────────────────────
# These are warnings, not commit blockers. Persona wording belongs in persona settings, not brain
# files (see docs/brain-model.md prompt boundary). The address heuristic is deliberately coarse —
# house number + name word(s) + street suffix — so it surfaces likely addresses for operator review
# without hard-blocking a legit commit on a false match (which would just train `--no-verify`).
# response-mechanics / persona-voice match INSTRUCTIONAL wording only ("draft a reply", "sign off
# with…", "use a warm greeting") — never a bare topic mention. Field-note P2: "greeting cards" and
# "the sign-off field in the contract PDF" (and the approval sense "sign off on a quote") must pass
# clean, so `greeting`/`sign-off`/`salutation` only count when an imperative verb or directive frames
# them.
CONTRACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("street-address", re.compile(
        r"(?i)\b\d{1,5}\s+(?:[A-Z][A-Za-z.]+\s+){0,2}[A-Z][A-Za-z.]+\s*"
        r"(?:street|avenue|ave|road|boulevard|blvd|lane|drive|straat|laan)\b")),
    ("response-mechanics", re.compile(
        r"(?i)\b(?:"
        r"(?:draft|write|compose)\s+(?:a\s+|the\s+)?(?:repl\w+|response|message|email)"
        r"|sign[\s-]?off\s+(?:with|warmly|using|as\b|by\b)"
        r"|(?:always|please|do|should|must)\s+sign[\s-]?off"
        r"|(?:use|open|start|begin|end|close)\s+(?:\w+\s+){0,3}(?:greeting|salutation|sign[\s-]?off)"
        r"|(?:warm|friendly|formal|polite|casual)\s+(?:greeting|salutation)"
        r"|tone of voice|customer-facing tone"
        r")\b")),
    ("persona-voice", re.compile(
        r"(?i)\b(?:sound more like us|brand voice|use a (?:friendly|formal|warm) tone|"
        r"always sign|email signature)\b")),
]

# The public rp CLI (legacy alias `rc`) is available to the local brain-development agent, never to
# the production main loop. Match known command roots rather than the bare token so RC_* env vars,
# `rc:branch` projection markers, `/tmp/rc-*` paths, and prose such as "rp CLI" remain valid.
RC_CLI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rc-cli-command", re.compile(
        r"\br[cp][ \t]+(?:access|action|admin|ask|auth|bash|brain|branding|capabilities|completion|"
        r"commands|config|connection|database|db|dev|dream|env|explain|export|fleet|github|health|help|"
        r"id|integration|integrations|kb|login|logout|mailbox|mcp|member|openapi|patterns|project|"
        r"projects|prompt|provider|repo|routes|run|runs|schema|self|skills|spam|status|tenant|thread|"
        r"token|triage|upgrade|version|whoami)\b"
    )),
    ("rc-cli-command", re.compile(
        r"\br[cp][ \t]+(?:-[hov]\b|--(?:help|no-preview|out-dir|output|profile|project|raw-output|tenant|version)\b)"
    )),
]

SCAN_SUFFIXES = {".md", ".py", ".rb", ".sh", ".toml", ".yaml", ".yml", ".json"}


# Run-hidden trees: content the production run never sees (`/brain` is built without them), so the
# maintainer-only rp CLI guidance, harness config and operator overlays living there are exactly
# where they belong. Same semantics as brain_structure.py's ignored-refs resolver.
IGNORE_CONTROLS = (".replypenignore", ".rcignore")
ALWAYS_HIDDEN_PREFIXES = ("_internal/",)


def run_hidden_paths(root: Path | None = None) -> tuple[set[str], tuple[str, ...]]:
    """(exact paths, directory prefixes) excluded from runs by `.replypenignore` / `.rcignore`
    / the conventional `_internal/` tree, all repo-relative posix."""
    root = (root or Path.cwd()).resolve()
    exact: set[str] = set()
    prefixes: set[str] = set(ALWAYS_HIDDEN_PREFIXES)
    for control in IGNORE_CONTROLS:
        if not (root / control).is_file():
            continue
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "-c", "-o", "-i",
             f"--exclude-from={root / control}"], capture_output=True, text=True)
        if proc.returncode != 0:
            continue  # not a git checkout / unusable git: judge everything, as before
        for entry in (e for e in proc.stdout.split("\0") if e):
            exact.add(entry)
            if (root / entry).is_dir():
                prefixes.add(entry.rstrip("/") + "/")
    return exact, tuple(sorted(prefixes))


def _is_run_hidden(path: Path, hidden: tuple[set[str], tuple[str, ...]], root: Path) -> bool:
    exact, prefixes = hidden
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix().removeprefix("./")
    return rel in exact or rel.startswith(prefixes)


def _kit_checkout_root() -> Path | None:
    """Return the kit cwd only when lint was launched from the kit checkout itself."""
    if (
        Path("docs/rp-cli.md").is_file()
        and Path("runtime/lib").is_dir()
        and Path("skills/local-brain-work/SKILL.md").is_file()
    ):
        return Path.cwd().resolve()
    return None


def _is_kit_target(path: Path, kit_root: Path | None) -> bool:
    """Exempt intentional kit docs, never an external target passed while cwd is the kit."""
    return kit_root is not None and path.resolve().is_relative_to(kit_root)


def _iter_targets(paths: list[str]) -> list[Path]:
    """Expand dirs into supported brain text files; explicit files are always honored."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(child for child in p.rglob("*") if child.suffix.lower() in SCAN_SUFFIXES))
        elif p.exists():
            out.append(p)
    return out


def _staged_targets() -> list[Path]:
    """The pre-commit target set: supported staged text paths that still exist on disk."""
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"error: git diff --cached failed: {proc.stderr.strip()}", file=sys.stderr)
        return []
    return [
        Path(ln) for ln in proc.stdout.splitlines()
        if Path(ln).suffix.lower() in SCAN_SUFFIXES and Path(ln).exists()
    ]


def _all_targets() -> list[Path]:
    """`--all` target set: supported brain text under the tree, skipping `.replypen/` export
    dumps and VCS/build noise so we never lint the raw corpus we are trying to keep OUT of the brain."""
    skip = {".git", ".replypen", ".rootcause", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}
    return [
        p for p in sorted(Path(".").rglob("*"))
        if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES and not (skip & set(p.parts))
    ]


def _scan_line(
    line: str, *, allow_rc_cli: bool = False, scratch: bool = False,
) -> list[tuple[str, str, str]]:
    """Return (severity, category, snippet) for every pattern hit on one line.

    `scratch` mode suppresses the harvest opaque-ID / raw-filename / scratch-path classes: that
    content is expected inside the harvest scratch root. Every other class still applies there."""
    hits: list[tuple[str, str, str]] = []
    groups: list[tuple[list[tuple[str, re.Pattern[str]]], str]] = [
        (SECRET_PATTERNS, HARD),
        (RAWTHREAD_PATTERNS, HARD),
        (RAWTHREAD_SOFT_PATTERNS, SOFT),
        (PAYMENT_PATTERNS, HARD),
        (IDENTIFIER_HARD_PATTERNS, HARD),
        (CONTACT_PATTERNS, SOFT),
        (IDENTIFIER_SOFT_PATTERNS, SOFT),
        # Names: HARD in the tracked diff, soft warning only in scratch scanning (spec §7).
        (NAME_PATTERNS, SOFT if scratch else HARD),
        (CONTRACT_PATTERNS, SOFT),
    ]
    if not scratch:
        groups.append((HARVEST_PATTERNS, HARD))
    for groups_, severity in groups:
        for name, pat in groups_:
            m = pat.search(line)
            if m:
                snippet = m.group(0).strip()
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                hits.append((severity, name, snippet))
    if not allow_rc_cli:
        for name, pat in RC_CLI_PATTERNS:
            if match := pat.search(line):
                hits.append((HARD, name, match.group(0)))
    return hits


def lint_file(
    path: Path, *, allow_rc_cli: bool = False, rc_only: bool = False, scratch: bool = False,
) -> list[tuple[int, str, str, str]]:
    """Scan one file. Returns (lineno, severity, category, snippet) findings."""
    findings: list[tuple[int, str, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return findings
    for n, line in enumerate(text.splitlines(), start=1):
        hits = [] if rc_only else _scan_line(line, allow_rc_cli=True, scratch=scratch)
        if not allow_rc_cli:
            for name, pat in RC_CLI_PATTERNS:
                if match := pat.search(line):
                    hits.append((HARD, name, match.group(0)))
        for severity, category, snippet in hits:
            findings.append((n, severity, category, snippet))
    return findings


def _selftest() -> int:
    """Cheap built-in check that each category fires and clean prose stays clean. No repo needed."""
    must_flag = {
        "AKIA1234567890ABCDEF": HARD,
        "token: hunter2secret": HARD,
        "> quoted line from a thread": SOFT,   # blockquotes are legit Markdown; fatal only under --strict
        "On Tue, Jan 2 2024, Alice wrote:": HARD,
        "From: alice@example.com": HARD,
        "pay here https://stripe.com/pay/abc": HARD,
        "IBAN NL91 ABNA 0417 1643 00": HARD,
        "connect via postgres://user:pass@db.host:5432/app": HARD,   # db-url-credential
        "the key is sk-proj-abc123DEF456ghi789JKL012mno345": HARD,    # modern openai project key
        "Please draft a reply to the customer": SOFT,
        "sign off warmly with our name": SOFT,
        "Always sign off with the agent's first name.": SOFT,          # instructional sign-off (FP fix keeps it)
        "The customer at 123 Main Street reported a duplicate charge.": SOFT,
        "Call the support line at +1 (555) 123-4567 anytime.": SOFT,   # phone number
        "See order #10482 in the billing portal.": SOFT,               # coarse order ref
        "Escalated by Dr. Smith on the account.": HARD,                # honorific + name (tracked mode)
        "Dear Jane Doe, thanks for reaching out.": HARD,               # greeting + name (tracked mode)
        "Tracking number 1Z999AA10123456784 shipped Monday.": HARD,    # carrier tracking id
        "Order ABC12345XYZ was refunded in full.": HARD,               # explicit-prefix mixed order id
        "See thread H000123 for the disputed charge.": HARD,           # legacy opaque harvest id
        "See thread H0123456789abcdef0123456789abcdef for context.": HARD,  # stable opaque id
        "Evidence: 2026-07--refund-duplicate-charge--3.md backs this.": HARD,  # harvest filename shape
        "Raw dump lives in .replypen/harvest/tmp/ locally.": HARD,    # harvest scratch path
        "Use `rp run debug <id>` to inspect the run.": HARD,
        "Run `rp env pull` before the live check.": HARD,
        "Inspect it with `rp action list`.": HARD,
        "Query through `rp db schema prod`.": HARD,
        "Use `rp bash run 'true'` for the smoke test.": HARD,
        "Use `rp --project pro-backup capabilities`.": HARD,
        "Inspect the connector with `rp integration list`.": HARD,
        "Use `rp run debug <id>` to inspect the run.": HARD,          # legacy alias still flagged
    }
    must_pass = [
        "Customers on the Pro plan can export up to 10k rows.",
        "Route billing questions to the refund runbook.",
        "See the route cases/billing/refunds/proration/upgrades/x for the playbook.",  # slug path, not a blob
        "Order US12 ABCD 1234 for the batch shipped Monday.",         # not an IBAN (trailing word)
        "SKU AB12 CDEF 3456 GHIJ in the catalog is discontinued.",    # SKU-ish, not an IBAN
        "Set password: <your-password> in the local .env before running.",  # placeholder, not a secret
        "Use token=xxx as an example when documenting the API.",      # placeholder, not a secret
        'Read it via `sdk.api_key = os.environ["STRIPE_RESTRICTED_KEY"]` at startup.',  # env lookup
        "Export token=$GITHUB_TOKEN before running the sync.",        # env-var reference, not a value
        "Connect with postgresql://provisioner:***@db.host:5432/app locally.",  # masked password
        "Dear Suppliers, please update your invoicing details.",      # generic role, not a person
        "The invoice total reflects proration for mid-cycle upgrades.",
        "Customers ask about greeting cards and thank-you notes.",     # topic mention, not response-mechanics
        "The sign-off field on the invoice form is optional.",         # noun mention, not a directive
        "Approve the quote before you sign off on the refund.",        # approval sense of "sign off"
        "The Pro plan renews at $1,299.00 per year.",                  # price, not a phone number
        "The incident opened on 2026-07-19 around noon.",              # date, not a phone number
        "Pin the runtime to version 3.11.6 exactly.",                  # version, not a phone number
        "Refer to commit 3f2a1b9c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f90 for the fix.",  # git SHA, not a secret
        "The sha256 digest is e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.",
        "Use the rp CLI only from the local development checkout.",
        "Set RC_PROJECT_ID before running the script.",
        "Keep the <!-- rc:branch --> marker in this template.",
        "Write scratch output under /tmp/rc-debug/.",
    ]
    ok = True
    for text, want in must_flag.items():
        sev = {s for s, _, _ in _scan_line(text)}
        if want not in sev:
            print(f"selftest FAIL: expected {want} finding for {text!r}, got {sev}", file=sys.stderr)
            ok = False
    for text in must_pass:
        hits = _scan_line(text)
        if hits:
            print(f"selftest FAIL: clean prose flagged {text!r}: {hits}", file=sys.stderr)
            ok = False

    # A lone blockquote is legit Markdown: SOFT (fatal only under --strict, the harvest gate), never HARD.
    quoted = "> The customer wrote something here."
    sev = {s for s, _, _ in _scan_line(quoted)}
    if SOFT not in sev or HARD in sev:
        print(f"selftest FAIL: blockquote should be SOFT-only, got {sev}", file=sys.stderr)
        ok = False

    # An own-domain routing address is a legitimate brain reference: SOFT (reviewable), never HARD.
    routing = "Route escalations to billing@pro-backup.io for triage."
    sev = {s for s, _, _ in _scan_line(routing)}
    if SOFT not in sev or HARD in sev:
        print(f"selftest FAIL: routing address should be SOFT-only, got {sev}", file=sys.stderr)
        ok = False

    # Default mode flags harvest opaque IDs and raw filenames (HARD); --scratch suppresses just those.
    leak = "See thread H0123456789abcdef0123456789abcdef in 2026-07--dispute--3.md for context."
    default_cats = {c for _, c, _ in _scan_line(leak)}
    scratch_cats = {c for _, c, _ in _scan_line(leak, scratch=True)}
    if not {"harvest-opaque-id", "harvest-filename"} <= default_cats:
        print(f"selftest FAIL: default mode missed harvest leak, got {default_cats}", file=sys.stderr)
        ok = False
    if {"harvest-opaque-id", "harvest-filename"} & scratch_cats:
        print(f"selftest FAIL: --scratch should suppress harvest classes, got {scratch_cats}",
              file=sys.stderr)
        ok = False

    # Names are HARD in the tracked diff, downgraded to SOFT in scratch scanning (spec §7).
    name_line = "Dear Jane Doe, thanks for reaching out."
    name_default = {s for s, c, _ in _scan_line(name_line) if c == "greeting-name"}
    name_scratch = {s for s, c, _ in _scan_line(name_line, scratch=True) if c == "greeting-name"}
    if name_default != {HARD} or name_scratch != {SOFT}:
        print(f"selftest FAIL: name severity split wrong: default={name_default} scratch={name_scratch}",
              file=sys.stderr)
        ok = False

    kit_root = Path("/tmp/brain-skills-kit").resolve()
    if not _is_kit_target(kit_root / "docs/rp-cli.md", kit_root):
        print("selftest FAIL: kit target was not exempt", file=sys.stderr)
        ok = False
    if _is_kit_target(Path("/tmp/project-brain/AGENTS.md"), kit_root):
        print("selftest FAIL: external brain target inherited kit exemption", file=sys.stderr)
        ok = False
    print("selftest ok" if ok else "selftest FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_lint.py", description=__doc__.split("\n")[0])
    p.add_argument("paths", nargs="*", help="files/dirs to scan (default: staged brain text).")
    p.add_argument("--all", action="store_true", help="scan supported brain text under the tree.")
    p.add_argument("--strict", action="store_true", help="soft (contract) findings also fail.")
    p.add_argument("--scratch", action="store_true",
                   help="scratch mode: suppress harvest opaque-ID/filename classes (expected there).")
    p.add_argument("--selftest", action="store_true", help="run built-in regex self-checks and exit.")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return _selftest()

    if args.paths:
        targets = _iter_targets(args.paths)
    elif args.all:
        targets = _all_targets()
    else:
        targets = _staged_targets()

    # Maintainer-only trees (.agents/, .claude/, _internal/, anything the ignore controls hide) are
    # physically absent from every run, so local-only rp CLI guidance there is by design: never judge
    # them, in any mode — explicit hidden paths are silently dropped too.
    root = Path.cwd().resolve()
    hidden = run_hidden_paths(root)
    targets = [t for t in targets if not _is_run_hidden(t, hidden, root)]

    if not targets:
        print("no brain text to scan (staged set empty — use --all or pass paths).")
        return 0

    hard = soft = 0
    kit_root = _kit_checkout_root()
    for path in targets:
        allow_rc_cli = _is_kit_target(path, kit_root)
        rc_only = path.suffix.lower() != ".md"
        for lineno, severity, category, snippet in lint_file(
            path, allow_rc_cli=allow_rc_cli, rc_only=rc_only, scratch=args.scratch,
        ):
            print(f"{path}:{lineno}: {severity} {category}: {snippet}")
            if severity == HARD:
                hard += 1
            else:
                soft += 1

    scanned = len(targets)
    if hard:
        print(f"\nFAIL: {hard} hard finding(s), {soft} soft, across {scanned} file(s). "
              "Remove raw data/secrets and local-only rp CLI guidance.", file=sys.stderr)
        return 1
    if soft and args.strict:
        print(f"\nFAIL (--strict): {soft} soft contract finding(s) across {scanned} file(s). "
              "Move response-mechanics/persona wording to persona settings.", file=sys.stderr)
        return 1
    if soft:
        print(f"\nok with warnings: {soft} soft finding(s) across {scanned} file(s) "
              "(run --strict to enforce). No hard findings.")
    else:
        print(f"ok: no findings across {scanned} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
