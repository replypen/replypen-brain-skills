#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic>=2", "psycopg[binary]>=3"]
# ///
"""Pydantic models for `report.json` — the one artefact the LLM writes.

One source of truth for `validate.py`, `publish.py` and `prompt_compose.py`.
Keys and technical content are English; the owner half is Dutch.

Field guide for the LLM: `../report_schema.md`.
Contract with the collector: `kpis.json` / `manifest.json` (see PLAN, slice A).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    model_validator,
)

RUN_URL_PREFIX = "https://app.replypen.com/runs/"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
CANONICAL_RUN_URL = re.compile(rf"^{re.escape(RUN_URL_PREFIX)}{_UUID}$")
TOKENIZED_RUN_URL = re.compile(
    rf"^{re.escape(RUN_URL_PREFIX)}{_UUID}(\?t=[A-Za-z0-9_\-]+)?$"
)

MAX_FINDINGS = 12
PROMPT_WORDS_MIN = 90
PROMPT_WORDS_MAX = 260

Severity = Literal["high", "medium", "low", "good"]
Audience = Literal["technical", "owner", "both"]
Confidence = Literal["low", "medium", "high"]
ScopeLevel = Literal["project", "member", "channel", "tenant"]
RecurrenceState = Literal["new", "recurring", "gone"]
TaskKind = Literal["fix", "investigate", "decide"]
FeedStatus = Literal["complete", "partial", "unavailable"]
MailboxMode = Literal["live", "shadow", "watch", "off"]

FindingKind = Literal[
    "lost_runs",
    "action_failure",
    "script_error",
    "capture_gap",
    "correction",
    "open_feedback",
    "policy_question",
    "regression",
    "watch",
    "pattern",
    "path_miss",
    "usage",
    "other",
    "good",
]

Plane = Literal[
    "host",
    "action_plane",
    "brain_script",
    "brain_content",
    "tenant_brain",
    "persona",
    "settings",
    "mirror",
    "project_code",
    "human_policy",
    "human_context",
    "noise",
    "unknown",
]

NonEmpty = Annotated[str, Field(min_length=1)]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# kpis.json / manifest.json (produced by collect.py, copied verbatim)
# --------------------------------------------------------------------------


class Counters(_Model):
    """The 18 counters of the A↔B contract; every one defaults to 0."""

    runs: int = Field(default=0, ge=0)
    counted: int = Field(default=0, ge=0)
    excluded: int = Field(default=0, ge=0)
    drafts: int = Field(default=0, ge=0)
    sent_as_proposed: int = Field(default=0, ge=0)
    edited: int = Field(default=0, ge=0)
    shadow_compared: int = Field(default=0, ge=0)
    not_sent_yet: int = Field(default=0, ge=0)
    no_draft: int = Field(default=0, ge=0)
    actions_ok: int = Field(default=0, ge=0)
    actions_failed: int = Field(default=0, ge=0)
    actions_proposed: int = Field(default=0, ge=0)
    run_errors: int | None = Field(default=0, ge=0)
    bash_real_errors: int = Field(default=0, ge=0)
    usage_errors: int = Field(default=0, ge=0)
    deltas_live: int = Field(default=0, ge=0)
    deltas_shadow: int = Field(default=0, ge=0)
    feedback: int = Field(default=0, ge=0)


class DayKpis(Counters):
    date: IsoDate


class AxisKpis(Counters):
    axis: Literal["tenant", "channel", "member"]
    key: NonEmpty
    name: NonEmpty
    mode: MailboxMode | None = None


class ActionFunnelRow(_Model):
    action_id: NonEmpty
    proposed_total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    superseded: int = Field(ge=0)
    canceled: int = Field(ge=0)
    executing: int = Field(ge=0)
    pending: int = Field(ge=0)
    stale: int = Field(ge=0)
    human_confirmed: int = Field(ge=0)
    auto: int = Field(ge=0)
    acceptance_rate: float | None = Field(ge=0, le=1)


class ActionFunnelTable(_Model):
    rows: list[ActionFunnelRow]
    total: ActionFunnelRow


class ActionFunnelAxis(ActionFunnelTable):
    axis: Literal["tenant", "channel", "member"]
    key: NonEmpty


class ActionFunnelRule(_Model):
    reviewer_confirmed_after_s: int = Field(ge=0)
    stale_after_h: int = Field(ge=0)


class ActionFunnel(_Model):
    focus: ActionFunnelTable
    context: ActionFunnelTable
    per_axis: list[ActionFunnelAxis]
    rule: ActionFunnelRule


class Kpis(_Model):
    action_funnel: ActionFunnel | None = None
    focus: DayKpis
    context_days: list[DayKpis] = Field(default_factory=list)
    per_axis: list[AxisKpis] = Field(default_factory=list)


class FeedCoverage(_Model):
    feed: NonEmpty
    status: FeedStatus
    reason: str | None = None
    fetched: int = Field(default=0, ge=0)
    observed_at: str | None = None


class Window(_Model):
    focus: IsoDate
    focus_days: list[IsoDate] = Field(default_factory=list)
    context_days: list[IsoDate] = Field(default_factory=list)


class ManifestRaw(_Model):
    """`manifest.raw` — where the rc response cache landed and how big it got."""

    path: str | None = None
    files: int = Field(default=0, ge=0)
    mb: float = Field(default=0.0, ge=0)
    pruned: bool = False


class Manifest(_Model):
    """`manifest.json` verbatim; lands on `report.coverage`.

    Round-trip contract: whatever `collect.py` writes must validate here, so the documented
    "copy manifest.json verbatim" is literally true — including `raw` and `owner_lang`.
    """

    report_id: NonEmpty
    projects: list[NonEmpty] = Field(min_length=1)
    window: Window
    generated_at: NonEmpty
    rc_version: str | None = None
    coverage: list[FeedCoverage] = Field(default_factory=list)
    excluded: dict[str, int] = Field(default_factory=dict)
    owner_lang: str | None = None
    owner_lang_by_tenant: dict[str, str] = Field(default_factory=dict)
    raw: ManifestRaw | None = None

    def lang(self, tenant: str | None = None) -> str:
        """The owner half's language, as the HOST resolved it from `persona.language`.

        Tenant-scoped findings follow their tenant; everything else follows the project. English
        is the floor (the host's own "empty ⇒ English"), never a hardcoded Dutch."""
        for value in ((self.owner_lang_by_tenant or {}).get(tenant or ""), self.owner_lang):
            code = str(value or "").strip().lower()[:2]
            if code:
                return code
        return "en"


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


class Scope(_Model):
    """`level` plus the axis value it names.

    `tenant` stays the tenant-only slug (it is cross-checked against `kpis.per_axis`).
    `key` is the generic axis value — the channel on a tenantless project, the member on a
    multi-member report, the tenant slug on a tenant finding. `level: project` is keyless.
    """

    level: ScopeLevel
    tenant: str | None = None
    key: str | None = None

    @model_validator(mode="after")
    def _tenant_only_on_tenant_level(self) -> Scope:
        if self.level == "tenant" and not self.tenant:
            raise ValueError("level 'tenant' needs a 'tenant' slug")
        if self.level != "tenant" and self.tenant:
            raise ValueError("'tenant' is only allowed when level is 'tenant'")
        if self.level == "project" and self.key:
            raise ValueError(
                "'key' is not allowed on level 'project' (a project finding names no axis value)"
            )
        if self.level == "tenant" and self.key and self.key != self.tenant:
            raise ValueError(
                f"'key' {self.key!r} differs from 'tenant' {self.tenant!r} "
                "(on level 'tenant' they are the same slug — set one or both to it)"
            )
        return self

    def axis_value(self) -> str | None:
        return self.tenant or self.key


class Impact(_Model):
    runs: int = Field(default=0, ge=0)
    threads: int = Field(default=0, ge=0)


class Recurrence(_Model):
    first_seen: IsoDate
    last_seen: IsoDate
    focus_count: int = Field(default=0, ge=0)
    context_count: int = Field(default=0, ge=0)
    state: RecurrenceState
    known_since: IsoDate | None = None


class ManualFeedback(_Model):
    # Manual comments are verbatim evidence, including surrounding whitespace.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    score: int | None = Field(default=None, ge=1, le=5)
    comment: NonEmpty | None = None

    @model_validator(mode="after")
    def _present(self):
        if self.score is None and self.comment is None:
            raise ValueError("feedback needs a manual score or comment")
        return self


class EvidenceEntry(_Model):
    run_id: Annotated[str, Field(pattern=rf"^{_UUID}$")]
    label: NonEmpty | None = None
    question: NonEmpty | None = None
    proposed: NonEmpty | None = None
    sent: NonEmpty | None = None
    feedback: ManualFeedback | None = None


class Evidence(_Model):
    entries: list[EvidenceEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        ids = [e.run_id.lower() for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence entries must have unique run IDs")
        return self

    run_ids: list[NonEmpty] = Field(default_factory=list)
    run_urls: list[NonEmpty] = Field(default_factory=list)


class RootCause(_Model):
    plane: Plane
    confidence: Confidence


class Target(_Model):
    repo: NonEmpty
    paths: list[NonEmpty] = Field(min_length=1)

    @model_validator(mode="after")
    def _paths_exist(self) -> Target:
        repo = Path(self.repo).expanduser()
        if not repo.is_absolute() or not repo.is_dir():
            raise ValueError(f"repo must be an existing absolute checkout: {self.repo}")
        for value in self.paths:
            path = Path(re.sub(r":\d+(?::\d+)?$", "", value)).expanduser()
            path = path if path.is_absolute() else repo / path
            if not path.exists():
                raise ValueError(f"path does not exist: {path}")
            if not path.resolve().is_relative_to(repo.resolve()):
                raise ValueError(
                    f"path is outside target repo: {path}; add its repo as another target"
                )
        return self


class Decision(_Model):
    question: Annotated[str, Field(min_length=1, max_length=300)]
    options: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        min_length=2, max_length=4
    )


class Prompt(_Model):
    task_kind: TaskKind
    targets: list[Target] = Field(min_length=1)
    run_refs: list[NonEmpty] = Field(default_factory=list)
    repro: str | None = Field(default=None, max_length=400)
    conclusion: Annotated[str, Field(min_length=20, max_length=600)]
    change: Annotated[str, Field(min_length=20, max_length=900)]
    done_when: Annotated[str, Field(min_length=10, max_length=400)]
    decision: Decision | None = None

    @model_validator(mode="after")
    def _rules(self) -> Prompt:
        if self.task_kind == "decide" and not self.decision:
            raise ValueError("decision is required for decide")
        if any(not CANONICAL_RUN_URL.match(url) for url in self.run_refs):
            raise ValueError("run_refs must be canonical run URLs without tokens")
        return self


class Option(_Model):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    instruction: Annotated[str, Field(min_length=1, max_length=8192)]

    @model_validator(mode="after")
    def _meaningful_label(self):
        words = re.findall(r"\w+", self.label.casefold())
        if " ".join(words) == "done in dashboard" or (len(words) <= 2 and words and set(words) <= {"ja", "nee", "ok"}):
            raise ValueError("option label must name the owner's task or policy choice")
        return self


class PrototypeOutput(_Model):
    stdout: str = Field(max_length=6000)
    stderr: str = Field(max_length=6000)
    exit_code: int
    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    run_id: NonEmpty
    seq: int = Field(default=0, ge=0)


class Prototype(_Model):
    repo: NonEmpty
    branch: Annotated[str, Field(pattern=r"^review/[a-z0-9]+(?:-[a-z0-9]+)*$")]
    before: PrototypeOutput
    after: PrototypeOutput
    command: Annotated[str, Field(min_length=1, max_length=2000)]
    files: int = Field(ge=1)

    @model_validator(mode="after")
    def _successful(self):
        if self.after.exit_code != 0:
            raise ValueError("prototype AFTER must succeed before offering a merge")
        if self.before.sha == self.after.sha:
            raise ValueError("prototype must compare distinct commits")
        return self


class Finding(_Model):
    id: Annotated[str, Field(pattern=r"^F\d{1,3}$")]
    signature: NonEmpty
    kind: FindingKind
    audience: Audience
    members: list[NonEmpty] = Field(default_factory=list)
    scope: Scope
    severity: Severity
    recurrence: Recurrence
    title: Annotated[str, Field(min_length=1, max_length=90)]
    title_nl: Annotated[str, Field(min_length=1, max_length=90)] | None = None
    status: Literal["new", "changed", "unchanged"]
    evidence: Evidence
    impact: Impact | None = None
    root_cause: RootCause | None = None
    text_en: str | None = Field(default=None, max_length=700)
    text_nl: str | None = Field(default=None, max_length=500)
    ask_nl: str | None = Field(default=None, max_length=300)
    ask_for: str | None = Field(default=None, max_length=40)
    update_en: str | None = Field(default=None, max_length=200)
    update_nl: str | None = Field(default=None, max_length=200)
    prompt: Prompt | None = None
    prototype: Prototype | None = None
    options: list[Option] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _rules(self) -> Finding:
        def require(name):
            if not getattr(self, name):
                raise ValueError(
                    f"findings[{self.id}].{name}: required for {self.status}/{self.audience}"
                )

        if self.audience in ("owner", "both"):
            require("options")
            require("title_nl")
        if any(option.label in ("Other", "Skip") for option in self.options):
            raise ValueError("Other and Skip are reserved host options")
        if len({option.label for option in self.options}) != len(self.options):
            raise ValueError("options labels must be unique")
        if any(not TOKENIZED_RUN_URL.match(url) for url in self.evidence.run_urls):
            raise ValueError("evidence.run_urls must be run URLs")
        if self.status == "unchanged":
            forbidden = {
                "text_en",
                "text_nl",
                "ask_nl",
                "ask_for",
                "prompt",
                "prototype",
                "impact",
                "root_cause",
            }
            if forbidden & self.model_fields_set:
                raise ValueError(
                    "unchanged forbids "
                    + ", ".join(sorted(forbidden & self.model_fields_set))
                )
        else:
            require("impact")
            require("root_cause")
            if self.audience in ("technical", "both"):
                require("text_en")
            if self.audience in ("owner", "both"):
                require("text_nl")
                require("ask_nl")
            if self.severity == "high" and not (
                self.audience == "owner" and self.kind == "policy_question"
            ):
                require("prompt")
        if self.status in ("changed", "unchanged"):
            if self.audience in ("technical", "both"):
                require("update_en")
            if self.audience in ("owner", "both"):
                require("update_nl")
        elif {"update_en", "update_nl"} & self.model_fields_set:
            raise ValueError("new forbids update fields")
        return self


def owner_prompt_allowed(finding: Finding) -> bool:
    return bool(
        finding.audience in ("owner", "both")
        and finding.prompt
        and finding.prompt.task_kind == "fix"
        and finding.root_cause
        and finding.root_cause.plane in ("brain_content", "tenant_brain")
        and all(
            Path(t.repo).expanduser().resolve().name.startswith("rootcause-brain-")
            for t in finding.prompt.targets
        )
    )


class TechnicalView(_Model):
    headline: Annotated[str, Field(min_length=1, max_length=220)]
    noise_note: str | None = Field(default=None, max_length=400)


class OwnerView(_Model):
    headline_nl: Annotated[str, Field(min_length=1, max_length=220)]


class Meta(_Model):
    signal_note: str | None = Field(default=None, max_length=600)


class Report(_Model):
    schema_version: Literal[2]
    report_id: NonEmpty
    date: IsoDate
    generated_at: NonEmpty
    window: Window
    kpis: Kpis
    coverage: Manifest
    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    technical: TechnicalView
    owner: OwnerView
    meta: Meta | None = None
    _prior: dict = PrivateAttr(default_factory=dict)
    _source: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _cross_refs(self) -> Report:
        for field in ("id", "signature"):
            values = [getattr(f, field) for f in self.findings]
            if len(values) != len(set(values)):
                raise ValueError(f"findings: duplicate {field}")
        tenants = {a.key for a in self.kpis.per_axis if a.axis == "tenant"}
        for f in self.findings:
            if f.scope.tenant and f.scope.tenant not in tenants:
                raise ValueError(
                    f"findings[{f.id}].scope.tenant: unknown tenant {f.scope.tenant}"
                )
        if self.date != self.window.focus:
            raise ValueError("window.focus differs from date")
        return self

    def effective(self, finding: Finding) -> Finding:
        if finding.status != "unchanged":
            return finding
        previous = self._prior[finding.signature]["finding"]
        inherited = {
            k: previous.get(k) for k in ("text_en", "text_nl", "ask_nl", "ask_for")
        }
        inherited["prompt"] = (
            Prompt.model_validate(previous["prompt"])
            if previous.get("prompt")
            else None
        )
        inherited["root_cause"] = RootCause.model_validate(previous["root_cause"]) if previous.get("root_cause") else None
        inherited["impact"] = Impact.model_validate(previous["impact"]) if previous.get("impact") else None
        return finding.model_copy(update=inherited)

    def finding_by_id(self, finding_id: str) -> Finding | None:
        return next(
            (self.effective(f) for f in self.findings if f.id == finding_id), None
        )

    def axis_rows(self) -> list[AxisKpis]:
        for axis in ("tenant", "channel", "member"):
            rows = [a for a in self.kpis.per_axis if a.axis == axis]
            if rows:
                return rows
        return []

    def axis_name(self, key: str) -> str:
        return next((a.name for a in self.kpis.per_axis if a.key == key), key)

    def findings_sorted(self) -> list[Finding]:
        # Compatibility for the prompt CLI; author order is rank.
        return [self.effective(f) for f in self.findings]


# --------------------------------------------------------------------------
# soft rules (warnings — never block publication)
# --------------------------------------------------------------------------

_NL_WORDS = {
    "de",
    "het",
    "een",
    "en",
    "van",
    "niet",
    "voor",
    "met",
    "is",
    "dat",
    "op",
    "te",
    "zijn",
    "wordt",
    "naar",
    "maar",
    "die",
    "aan",
    "er",
    "ook",
    "nog",
    "bij",
    "we",
    "wel",
    "geen",
    "hij",
    "ze",
    "over",
    "dan",
    "als",
    "om",
    "wat",
    "heeft",
}
_EN_WORDS = {
    "the",
    "and",
    "is",
    "to",
    "of",
    "in",
    "that",
    "for",
    "with",
    "this",
    "are",
    "not",
    "on",
    "it",
    "we",
    "a",
    "an",
    "as",
    "by",
    "from",
    "was",
    "were",
    "but",
    "so",
    "at",
    "has",
    "have",
    "does",
    "which",
    "when",
    "should",
}


def _lang_score(text: str) -> tuple[int, int]:
    words = re.findall(r"[a-zA-Zà-üÀ-Ü]+", text.lower())
    return (
        sum(1 for w in words if w in _NL_WORDS),
        sum(1 for w in words if w in _EN_WORDS),
    )


# Imperative openers per owner language; a language we cannot judge simply skips the check
# rather than warning in the wrong grammar.
_IMPERATIVE_STARTS = {
    "nl": r"^(vul|bevestig|controleer|kijk|geef|kies|pas|werk|voeg|vermeld|beschrijf|bepaal|noteer|zet|maak|stuur)\b",
    "en": r"^(check|confirm|fill|review|provide|choose|update|add|describe|decide|note|send|set|pick)\b",
}


def _looks_english(text: str) -> bool:
    nl, en = _lang_score(text)
    return en > nl


def _looks_dutch(text: str) -> bool:
    nl, en = _lang_score(text)
    return nl > en


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_ids_in(blob: Any) -> set[str]:
    """Every uuid-ish string anywhere in evidence.json (shape-tolerant)."""
    found: set[str] = set()
    stack = [blob]
    uuidish = re.compile(rf"^{_UUID}$")
    short = re.compile(r"^[0-9a-f]{8}$")
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            if uuidish.match(node):
                found.add(node)
                found.add(node[:8])
            elif short.match(node):
                found.add(node)
    return found


def soft_warnings(report: Report, kpis_path=None, manifest_path=None) -> list[str]:
    out = []
    for f in report.findings:
        # `*_nl` means "owner language", whatever the host resolved for this finding's scope.
        owner_lang = report.coverage.lang(f.scope.tenant)
        if owner_lang in _IMPERATIVE_STARTS and f.ask_nl and "?" not in f.ask_nl and not re.match(
            _IMPERATIVE_STARTS[owner_lang], f.ask_nl, re.IGNORECASE,
        ):
            out.append(f"findings[{f.id}].ask_nl: use a direct question or imperative task naming the fill-in items")
        if owner_lang == "nl" and f.title_nl and _looks_english(f.title_nl):
            out.append(f"findings[{f.id}].title_nl: reads as English")
        if f.text_en and _looks_dutch(f.text_en):
            out.append(f"findings[{f.id}].text_en: reads as Dutch")
        if owner_lang == "nl" and f.text_nl and _looks_english(f.text_nl):
            out.append(f"findings[{f.id}].text_nl: reads as English")
        if f.scope.key and f.scope.key not in {a.key for a in report.kpis.per_axis}:
            out.append(f"findings[{f.id}].scope.key: unknown axis key")
        if f.prompt:
            words = len(compose_prompt(f).split())
            if not PROMPT_WORDS_MIN <= words <= PROMPT_WORDS_MAX:
                longest = max(
                    ("conclusion", "change", "done_when"),
                    key=lambda k: len(getattr(f.prompt, k)),
                )
                out.append(
                    f"findings[{f.id}].prompt: {words} words; aim for 90–260 (adjust {longest})"
                )
            if f.prompt.task_kind == "fix":
                if not f.prompt.repro:
                    out.append(
                        f"findings[{f.id}].prompt.repro: fix needs a reproducible case"
                    )
                for phrase in (
                    "locate",
                    "consider",
                    "investigate whether",
                    "either",
                    "or (b)",
                    "read the full error",
                ):
                    if phrase in f.prompt.change.lower():
                        out.append(
                            f"findings[{f.id}].prompt.change: fix hedge {phrase!r}; use investigate"
                        )
        if f.status == "new":
            words = {w for w in re.findall(r"\w+", f.title.lower()) if len(w) > 3}
            for signature, old in report._prior.items():
                old_words = {
                    w
                    for w in re.findall(r"\w+", old["finding"].get("title", "").lower())
                    if len(w) > 3
                }
                if (
                    signature != f.signature
                    and words
                    and len(words & old_words) / len(words) >= 0.6
                ):
                    out.append(
                        f"findings[{f.id}]: looks like prior signature {signature} — reuse it"
                    )
    for audience, maximum in (("technical", 5), ("owner", 4)):
        count = sum(
            f.status != "unchanged" and f.audience in (audience, "both")
            for f in report.findings
        )
        if count > maximum:
            out.append(
                f"{audience}: {count} expanded findings; aim for at most {maximum}"
            )
    for label, model, value, path in (
        ("kpis", Kpis, report.kpis, kpis_path),
        ("coverage", Manifest, report.coverage, manifest_path),
    ):
        if path:
            try:
                other = model.model_validate(_load_json(path))
            except (OSError, ValueError) as exc:
                out.append(
                    f"{label}: cannot compare against {path} ({_one_line_exc(exc)})"
                )
            else:
                if value.model_dump() != other.model_dump():
                    out.append(
                        f"{label}: differs from {path}; copy collector artefact verbatim"
                    )
    return out


def _one_line_exc(exc: Exception) -> str:
    return _excerpt(f"{type(exc).__name__}: {exc}", 200)


def evidence_entry_errors(entry: EvidenceEntry, collected: dict, out_dir: Path) -> list[str]:
    """Shared verbatim-source contract for publication and targeted backfills."""
    from evidence_excerpts import evidence_sources, excerpt_matches

    out = []
    prefix = entry.run_id
    sources = evidence_sources(entry.run_id, collected, out_dir)
    for field in ("question", "proposed", "sent"):
        value = getattr(entry, field)
        if value is not None and not any(excerpt_matches(value, original) for original in sources[field]):
            out.append(f"{prefix}.{field}: excerpt does not match collected text; drill this run or omit the field")
    if entry.feedback:
        supplied = entry.feedback.model_dump(exclude_none=True)
        if not any(all(row.get(k) == v for k, v in supplied.items()) for row in sources["feedback"]):
            out.append(f"{prefix}.feedback: no matching manual feedback in the feedback feed")
    return out


def evidence_errors(report: Report, evidence_path: str | Path) -> list[str]:
    """Run ids cited by findings must exist in the collector's evidence.json."""
    try:
        collected = _load_json(evidence_path)
        known = _run_ids_in(collected)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"<evidence>: cannot read {evidence_path} ({exc})"]
    out: list[str] = []
    for finding in report.findings:
        for entry in finding.evidence.entries:
            prefix = f"findings[{finding.id}].evidence.entries[{entry.run_id}]"
            out.extend(f"{prefix}: {error}" for error in
                       evidence_entry_errors(entry, collected, Path(evidence_path).parent))
        cited = finding.evidence.run_ids + [e.run_id for e in finding.evidence.entries]
        cited += [url.split('/')[-1].split('?')[0] for url in finding.evidence.run_urls]
        for i, run_id in enumerate(cited):
            if run_id not in known and run_id[:8] not in known:
                out.append(
                    f"findings[{finding.id}].evidence.run_ids[{i}]: {run_id!r} is not in "
                    f"{Path(evidence_path).name} (only cite runs the collector saw)"
                )
    return out


# --------------------------------------------------------------------------
# prompt composition (Python owns the blob; the LLM only fills the fields)
# --------------------------------------------------------------------------


def compose_prompt(finding: Finding) -> str:
    prompt = finding.prompt
    if prompt is None:
        return ""
    first = prompt.targets[0]
    lines = [
        f"{prompt.task_kind.upper()}: {finding.title}",
        "",
        f"Start in: {first.repo} — open this checkout, read its AGENTS.md, run everything from its root.",
    ]
    for target in prompt.targets[1:]:
        lines.append(f"Also touch: {target.repo}: {', '.join(target.paths)}")
    lines.append(f"Files: {', '.join(first.paths)}")
    for url in prompt.run_refs:
        lines.append(f"Runs: {url} (trace: rc run show {url.rsplit('/', 1)[-1]})")
    lines += ["", "What is wrong", prompt.conclusion]
    if prompt.repro:
        lines += ["", "Reproduce", prompt.repro]
    heading = {"fix": "Change", "investigate": "Task / Checks", "decide": "Per option"}[
        prompt.task_kind
    ]
    lines += ["", heading, prompt.change, "", "Done when", prompt.done_when]
    if prompt.decision:
        lines += ["", "Decision first", prompt.decision.question]
        lines += [f"- {option}" for option in prompt.decision.options]
    lines += [
        "",
        "Boundaries",
        "- Execute only after a human review decision. Investigation never runs rc ask or confirms actions.",
        "- Edit only the listed repos. Commit AND ship: publish brain, release kit or promote host as appropriate.",
        "- The implementation job owns production verification against Done when; record the result. If not improved, allow one bounded fix/ship/test retry (two rounds maximum).",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# loading / error formatting
# --------------------------------------------------------------------------

_HINTS = {
    "missing": "required field is absent",
    "extra_forbidden": "unknown key — check the spelling against report_schema.md",
    "literal_error": "value is not in the allowed list",
    "string_too_long": "shorten the text",
    "string_too_short": "text is too short",
    "too_short": "too few items",
    "too_long": "too many items",
    "string_pattern_mismatch": "wrong format",
    "greater_than_equal": "number must not be negative",
    "int_parsing": "must be a whole number",
    "json_invalid": "invalid JSON — check commas and quotes",
}

_PATH_LINE = re.compile(r"^[a-z_]+(\[[^\]]+\])?(\.[a-z_]+(\[[^\]]+\])?)*: ")


def _location(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def _excerpt(value: Any, limit: int = 80) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _format_error(error: dict[str, Any]) -> list[str]:
    loc = _location(tuple(error.get("loc", ())))
    msg = str(error.get("msg", "")).removeprefix("Value error, ")
    kind = str(error.get("type", ""))
    hint = _HINTS.get(kind, "")
    raw = error.get("input")
    excerpt = _excerpt(raw) if not isinstance(raw, (dict, list)) else ""
    lines: list[str] = []
    for part in (p.strip() for p in msg.split("\n")):
        if not part:
            continue
        if _PATH_LINE.match(part):
            lines.append(part)
            continue
        tail = f" ({hint})" if hint else ""
        if excerpt and kind not in {"missing", "too_short", "too_long"}:
            tail += f" (got: {excerpt!r})"
        lines.append(f"{loc}: {part}{tail}")
    return lines or [f"{loc}: {msg}"]


def load_report(path: str | Path, *, dsn=None, prior=None) -> Report:
    from prior import prior_findings

    path = Path(path)
    report = Report.model_validate_json(path.read_text(encoding="utf-8"))
    report._source = path.resolve()
    if any(f.evidence.entries for f in report.findings):
        errors = evidence_errors(report, path.parent / 'evidence.json')
        if errors:
            raise ValueError("\n".join(errors))
    report._prior = prior if prior is not None else prior_findings(
        path, projects=report.coverage.projects, dsn=dsn
    )
    for f in report.findings:
        old = report._prior.get(f.signature)
        projects = set(f.members or report.coverage.projects)
        audiences = {"owner", "technical"} if f.audience == "both" else {f.audience}
        relevant = [row for row in old.get("rows", []) if
                    ("project" not in row or row["project"] in projects) and
                    ("audience" not in row or row["audience"] in audiences) and
                    (row.get("audience") != "owner" or row.get("tenant") ==
                     (f.scope.tenant if f.scope.level == "tenant" else None) or
                     (f.scope.level == "tenant" and row.get("tenant") is None and
                      row.get("scope_label") in (f.scope.tenant, "project"))) and
                    "#archived:" not in row.get("signature", "")] if old else []
        if old and old.get("rows") and not relevant:
            old = None
        archived = bool(relevant and all(
            row["status"] in ("closed", "applied") for row in relevant
        ))
        active = [row for row in relevant if row["status"] not in ("closed", "applied")]
        same_day_retry = bool(active and all(
            str(row.get("last_seen")) == report.date for row in active
        ))
        if f.status == "new" and any(
            row["status"] == "closed" and
            (row.get("applied") or {}).get("disposition") != "later"
            for row in relevant
        ):
            raise ValueError(
                f"findings[{f.id}].status: closed signature requires changed + update explaining the met retest trigger"
            )
        if f.status == "new" and old and not archived and not same_day_retry:
            raise ValueError(
                f"findings[{f.id}].status: signature already active; use changed/unchanged"
            )
        if f.status in ("changed", "unchanged") and not old:
            raise ValueError(
                f"findings[{f.id}].status: {f.status} needs a prior v2 finding"
            )
        if f.status == "unchanged":
            if archived:
                raise ValueError(f"findings[{f.id}]: archived signature requires full fields; use new/changed")
            missing = [(project, audience) for project in projects for audience in audiences
                       if not any(row.get("project") == project and
                                  row.get("audience") == audience for row in active)]
            if missing:
                raise ValueError(
                    f"findings[{f.id}]: unchanged needs an active prior row for every member/audience; "
                    f"missing {missing}; use changed with full fields"
                )
            previous = old["finding"]
            if not previous.get("prompt") and not previous.get("ask_nl"):
                raise ValueError(
                    f"findings[{f.id}]: unchanged needs a prior prompt or owner task"
                )
            try:
                effective = report.effective(f)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(
                    f"findings[{f.id}]: inherited prompt no longer validates; "
                    f"use changed ({_one_line_exc(exc)})"
                ) from exc
            for name in (
                ["text_en"]
                if f.audience == "technical"
                else ["text_nl", "ask_nl"]
                if f.audience == "owner"
                else ["text_en", "text_nl", "ask_nl"]
            ):
                if not getattr(effective, name):
                    raise ValueError(
                        f"findings[{f.id}]: prior lacks {name}; use changed"
                    )
    return report


def validation_errors(path: str | Path, *, dsn=None, prior=None) -> list[str]:
    try:
        load_report(path, dsn=dsn, prior=prior)
    except ValidationError as exc:
        return [line for error in exc.errors() for line in _format_error(error)]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [str(exc)]
    return []
