from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import random
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_harvest.py"
SPEC = importlib.util.spec_from_file_location("prepare_harvest", SCRIPT)
assert SPEC and SPEC.loader
ph = importlib.util.module_from_spec(SPEC)
sys.modules["prepare_harvest"] = ph
SPEC.loader.exec_module(ph)


V2_CORPUS = """---
harvest_format: v2
harvested_at: 2026-01-02T03:04:05Z
accepted_interactions: 8
unique_content: 8
terminal_condition: accepted_target
---

## Warranty question about widget — #1

**Occurrences:** 3

**external (2025-06-01):**
Hello, I have a question about the warranty on the widget that we bought from you.

**mailbox (2025-06-02):**
Thanks for reaching out. The warranty covers two years and we will gladly assist you with the paperwork.
_[attachment: manual.pdf]_

## Warranty question about widget 12 — #2

**external (2022-03-01):**
Hello, I have a longer question about the warranty on the widget that we bought from your webshop last month. The unit stopped working after two weeks and I would like to know which steps we should take now to have this looked at and what information you need from us to proceed.

**mailbox (2022-03-02):**
Thanks for the details. We will arrange an inspection and you will receive the instructions from us shortly.

## Re: Warranty question about widget 99 — #3

**external (2025-07-01):**
Hello, this is another warranty question about the widget for you.

**mailbox (2025-07-02):**
Thanks, we have received your warranty question and we will answer it for you tomorrow.

## Order — #4

**external (2024-02-01):**
Hello, where is my order? I would like to know when it will arrive at our address.

**mailbox (2024-02-02):**
Ok

## Contact — #5

**external (2026-01-01):**
Submitted through the contact form on the website. Please call me back about this.

## (no subject) — #6

**external (2023-05-05):**
Hello, just checking in with you about this and that.

## Order — #123 problem — #7

**external (2021-04-04):**
Hello, there is a problem with ticket #55 for this order and we would like an update from you.

**mailbox (2021-04-05):**
Thanks for the message. We looked into the problem and we will send you the update this week.

## Refund request for damaged widget — #8

**external (2010-05-01):**
Hello, the widget arrived damaged and I want a refund or a chargeback for this order please.

**mailbox (2010-05-02):**
Sorry to hear that. We will process the refund for you right away and you will see it on your statement.
"""


V1_CORPUS = """---
harvest_format: v1
mailbox: owner@example.com
harvested_at: 2026-01-02T03:04:05Z
threads: 2
cleaned: true
truncated: false
---

## Delivery issue — #1
**Participants:** anna@customer.example, owner@example.com
**Span:** 2019-03-04 → 2019-03-10

**anna@customer.example (2019-03-04):**
Hello, my delivery has not arrived and I would like to know where it is at the moment please.

**Owner@Example.com (2019-03-05):**
We checked with the courier and your parcel will arrive tomorrow, sorry for the delay and thanks for waiting.
_[attachment: label.pdf]_

## (no subject) — #2
**anna@customer.example (2020-01-01):**
Just one line for you.
"""

V3_CORPUS = """---
harvest_format: v3
harvested_at: 2026-01-02T03:04:05Z
accepted_count: 2
rejection_reasons: {"no_contact_message":3,"no_later_admin_reply":5,"unverified_admin":2,"automation_only":7,"malformed_structure":1}
actor_types: {"automation":7,"contact":12,"human_admin":4,"system":2}
initiation_types: {"email":14,"messenger":5,"unknown":1}
candidate_bands: [{"start":"2025-01-01T00:00:00Z","end":"2025-07-01T00:00:00Z","count":11},{"start":"2025-07-01T00:00:00Z","end":"2026-01-01T00:00:00Z","count":10}]
deduplicated: 1
---

## Login question — #1
**outbound/human_admin (2025-06-01):**
An earlier admin note does not establish the reply pair.
**inbound/contact (2025-06-02):**
Could you explain why login is unavailable and how we can restore access to the account?
**outbound/human_admin (2025-06-03):**
We verified the account state and restored access. Please retry with the same login address.

## Billing question — #2
**inbound/contact (2025-08-01):**
Could you clarify this invoice and confirm whether the adjustment is already included?
**outbound/human_admin (2025-08-02):**
The adjustment is included in the current invoice and no additional payment is required.
"""


def write_corpus(root: Path, text: str, name: str = "corpus.md") -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def prepare(tmp: Path, text: str, export_id: str = "exp-test", **cfg_overrides) -> Path:
    corpus = write_corpus(tmp, text)
    scratch = tmp / "scratch"
    cfg = dict(ph.DEFAULTS)
    cfg.update(cfg_overrides)
    ph.prepare_scratch(corpus, scratch, cfg, export_id=export_id,
                       preflight=ph.synthetic_preflight(export_id))
    return scratch


def read_manifest(scratch: Path) -> list[dict]:
    lines = (scratch / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_clusters(scratch: Path) -> list[dict]:
    return json.loads((scratch / "clusters.json").read_text(encoding="utf-8"))["clusters"]


def read_ledger(scratch: Path) -> dict:
    return json.loads((scratch / "ledger.json").read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def test_v1_roles_dates_attachments_participants_span(self):
        meta, body = ph.parse_front_matter(V1_CORPUS)
        self.assertEqual(meta["harvest_format"], "v1")
        sections = ph.split_sections(body)
        self.assertEqual(len(sections), 2)
        thread = ph.parse_section(sections[0], "v1", meta["mailbox"], 0)
        self.assertEqual(thread.subject, "Delivery issue")
        self.assertEqual(thread.participants, ["anna@customer.example", "owner@example.com"])
        self.assertEqual(thread.span, ["2019-03-04", "2019-03-10"])
        self.assertEqual([(m.role, m.date) for m in thread.messages],
                         [("external", "2019-03-04"), ("mailbox", "2019-03-05")])
        self.assertEqual(thread.messages[1].attachments, ["label.pdf"],
                         "case-insensitive mailbox match must classify the owner reply")
        self.assertNotIn("_[attachment", thread.messages[1].body)

    def test_v2_roles_occurrences_and_attachment(self):
        meta, body = ph.parse_front_matter(V2_CORPUS)
        sections = ph.split_sections(body)
        self.assertEqual(len(sections), 8)
        thread = ph.parse_section(sections[0], "v2", "", 0)
        self.assertEqual(thread.occurrences, 3)
        self.assertEqual([(m.role, m.date) for m in thread.messages],
                         [("external", "2025-06-01"), ("mailbox", "2025-06-02")])
        self.assertEqual(thread.messages[1].attachments, ["manual.pdf"])

    def test_subject_with_em_dash_hash_and_plain_hash(self):
        meta, body = ph.parse_front_matter(V2_CORPUS)
        sections = ph.split_sections(body)
        tricky = ph.parse_section(sections[6], "v2", "", 6)
        self.assertEqual(tricky.subject, "Order — #123 problem")
        self.assertEqual(tricky.occurrence_index, 7)
        self.assertIn("#55", tricky.messages[0].body)

    def test_bom_crlf_front_matter(self):
        raw = "﻿" + V2_CORPUS.replace("\n", "\r\n")
        meta, body = ph.parse_front_matter(raw)
        self.assertEqual(meta["harvest_format"], "v2")
        self.assertEqual(len(ph.split_sections(body)), 8)

    def test_unknown_format_v4_fails_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = write_corpus(Path(tmp), V2_CORPUS.replace("harvest_format: v2",
                                                               "harvest_format: v4"))
            with self.assertRaisesRegex(ph.HarvestError, r"v4.*rp project corpus download") as ctx:
                ph.load_threads([corpus])
            self.assertIn("48h", str(ctx.exception))

    def test_v3_requires_explicit_structural_headings_and_ordered_reply(self):
        _, body = ph.parse_front_matter(V3_CORPUS)
        first = ph.parse_section(ph.split_sections(body)[0], "v3", "", 0)
        self.assertEqual([(m.direction, m.actor_role, m.role) for m in first.messages], [
            ("outbound", "human_admin", "mailbox"),
            ("inbound", "contact", "external"),
            ("outbound", "human_admin", "mailbox"),
        ])
        legacy = ph.split_sections(body)[0].replace("inbound/contact", "external", 1)
        with self.assertRaisesRegex(ph.HarvestError, "body/header inference is forbidden"):
            ph.parse_section(legacy, "v3", "", 0)
        automation = ph.split_sections(body)[0].replace("outbound/human_admin", "outbound/automation")
        with self.assertRaisesRegex(ph.HarvestError, "inbound/contact or outbound/human_admin"):
            ph.parse_section(automation, "v3", "", 0)
        no_later_reply = ph.split_sections(body)[0].rsplit("**outbound/human_admin", 1)[0]
        with self.assertRaisesRegex(ph.HarvestError, "contact inbound followed"):
            ph.parse_section(no_later_reply, "v3", "", 0)

    def test_v3_rejects_non_chronological_messages(self):
        _, body = ph.parse_front_matter(V3_CORPUS)
        section = ph.split_sections(body)[1].replace("2025-08-02", "2025-07-31")
        with self.assertRaisesRegex(ph.HarvestError, "chronological"):
            ph.parse_section(section, "v3", "", 1)

    def test_v3_diagnostics_are_strict_half_open_count_distributions(self):
        meta, _ = ph.parse_front_matter(V3_CORPUS)
        diagnostics = ph.parse_diagnostics(meta, "fixture")
        self.assertEqual(diagnostics["accepted_count"], 2)
        self.assertEqual(diagnostics["rejection_reasons"]["automation_only"], 7)
        self.assertEqual(diagnostics["candidate_bands"][0]["end"],
                         diagnostics["candidate_bands"][1]["start"])
        partial = dict(meta)
        del partial["actor_types"]
        with self.assertRaisesRegex(ph.HarvestError, "partial harvest diagnostics"):
            ph.parse_diagnostics(partial, "fixture")
        overlap = dict(meta)
        overlap["candidate_bands"] = overlap["candidate_bands"].replace(
            '"2025-07-01T00:00:00Z","end":"2026',
            '"2025-06-30T00:00:00Z","end":"2026')
        with self.assertRaisesRegex(ph.HarvestError, "non-overlapping"):
            ph.parse_diagnostics(overlap, "fixture")

    def test_missing_front_matter_fails_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = write_corpus(Path(tmp), "## Hello — #1\n\n**external (2024-01-01):**\nhi\n")
            with self.assertRaisesRegex(ph.HarvestError, "front-matter.*rp project corpus download"):
                ph.load_threads([corpus])

    def test_empty_corpus_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            front_only = "---\nharvest_format: v2\nharvested_at: 2026-01-02T03:04:05Z\n---\n\n"
            corpus = write_corpus(Path(tmp), front_only)
            with self.assertRaisesRegex(ph.HarvestError, "no '## "):
                ph.load_threads([corpus])


class PrepareTests(unittest.TestCase):
    def test_deterministic_byte_identical_reruns(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = prepare(Path(a), V2_CORPUS, holdout_count=1)
            second = prepare(Path(b), V2_CORPUS, holdout_count=1)
            for name in ("manifest.jsonl", "clusters.json", "ledger.json", "holdout.json",
                         "replay-cases.json", "run.json", "templates.json", "diagnostics.json", "diagnostics.md"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)
            self.assertEqual(sorted(p.name for p in (first / "threads").iterdir()),
                             sorted(p.name for p in (second / "threads").iterdir()))

    def test_templates_are_validated_normalized_and_source_ids_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "templates-export.json"
            raw = {"templates_format": "v1", "template_count": 2, "templates": [
                {"id": "gmail-draft-1", "subject": " Refund ", "body": " Use this phrasing. "},
                {"id": "gmail-draft-2", "body": "Second response"},
            ]}
            path.write_text(json.dumps(raw), encoding="utf-8")
            normalized, metadata = ph.load_templates(path)
            self.assertEqual(metadata["format"], "v1")
            self.assertEqual(metadata["count"], 2)
            self.assertRegex(metadata["sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("id", normalized["templates"][0])
            self.assertEqual(normalized["templates"][0],
                             {"subject": "Refund", "body": "Use this phrasing."})

            raw["template_count"] = 1
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ph.HarvestError, "template_count"):
                ph.load_templates(path)

    def test_prepare_keeps_templates_out_of_thread_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = write_corpus(root, V2_CORPUS)
            template_export = write_json(root / "templates-export.json", {
                "templates_format": "v1", "template_count": 1,
                "templates": [{"id": "gmail-draft-1", "subject": "Refund", "body": "Stock response"}],
            })
            scratch = root / "scratch"
            ph.prepare_scratch(corpus, scratch, dict(ph.DEFAULTS, holdout_count=1),
                               export_id="exp-test", preflight=ph.synthetic_preflight("exp-test"),
                               templates=template_export)
            prepared = json.loads((scratch / "templates.json").read_text(encoding="utf-8"))
            run = json.loads((scratch / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(prepared["template_count"], 1)
            self.assertEqual(run["inputs"]["templates_count"], 1)
            self.assertEqual(len(read_manifest(scratch)), 8, "templates must not enter the thread ledger")

    def test_idempotent_rerun_replaces_stale_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            original = (scratch / "manifest.jsonl").read_bytes()
            (scratch / "threads" / "H999999.md").write_text("stale", encoding="utf-8")
            (scratch / "manifest.jsonl").write_text("corrupted", encoding="utf-8")
            cfg = dict(ph.DEFAULTS, holdout_count=1)
            ph.prepare_scratch(Path(tmp) / "corpus.md", scratch, cfg, export_id="exp-test",
                               preflight=ph.synthetic_preflight("exp-test"))
            self.assertFalse((scratch / "threads" / "H999999.md").exists())
            self.assertEqual((scratch / "manifest.jsonl").read_bytes(), original)

    def test_ids_are_opaque_content_derived_and_manifest_first_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            rows = read_manifest(scratch)
            self.assertEqual(len({r["id"] for r in rows}), 8)
            self.assertTrue(all(ph.OPAQUE_ID_RE.fullmatch(r["id"]) for r in rows))
            self.assertEqual([list(r)[0] for r in rows], ["id"] * 8, "first key must be id")
            self.assertEqual([r["id"] for r in rows], sorted(r["id"] for r in rows))
            for path in (scratch / "threads").iterdir():
                self.assertRegex(path.name, r"^H[0-9a-f]{32}\.md$")

    def test_ids_stay_stable_across_full_delta_and_reordered_overlap(self):
        meta, body = ph.parse_front_matter(V2_CORPUS)
        sections = ph.split_sections(body)
        front = ("---\nharvest_format: v2\nharvested_at: " + meta["harvested_at"] + "\n---\n\n")
        delta = front + "\n\n".join([sections[1], sections[6]]) + "\n"
        reordered = front + "\n\n".join(reversed(sections)) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("full", "delta", "reordered"):
                (root / name).mkdir()
            full_rows = read_manifest(prepare(root / "full", V2_CORPUS, holdout_count=0))
            delta_rows = read_manifest(prepare(root / "delta", delta, holdout_count=0))
            reordered_rows = read_manifest(prepare(root / "reordered", reordered, holdout_count=0))
            full = {row["date_first"]: row["id"] for row in full_rows}
            self.assertEqual({row["date_first"]: row["id"] for row in delta_rows},
                             {row["date_first"]: full[row["date_first"]] for row in delta_rows})
            self.assertEqual({row["date_first"]: row["id"] for row in reordered_rows}, full)

    def test_duplicate_indistinguishable_threads_fail_before_outputs(self):
        meta, body = ph.parse_front_matter(V2_CORPUS)
        section = ph.split_sections(body)[0]
        duplicate = section.replace("— #1", "— #2", 1)
        corpus = ("---\nharvest_format: v2\nharvested_at: " + meta["harvested_at"] +
                  "\n---\n\n" + section + "\n\n" + duplicate + "\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ph.HarvestError, "duplicate indistinguishable"):
                prepare(root, corpus, holdout_count=0)
            self.assertFalse((root / "scratch" / "manifest.jsonl").exists())

    def test_mixed_bucket_and_generic_subjects_never_form_topic_clusters(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            clusters = {c["id"]: c for c in read_clusters(scratch)}
            self.assertIn("mixed", clusters)
            labels = {c["label"] for c in clusters.values()}
            self.assertIn("warranty-question-about-widget", labels)
            for generic in ("order", "contact", ""):
                self.assertNotIn(generic, labels)
            rows = {r["subject_family"]: r for r in read_manifest(scratch)}
            self.assertEqual(rows["order"]["cluster"], "mixed")
            self.assertEqual(rows["contact"]["cluster"], "mixed")
            self.assertEqual(rows[""]["cluster"], "mixed", "(no subject) must land in mixed")
            self.assertEqual(rows["order-problem"]["cluster"], "mixed",
                             "below-min-size families fall back to mixed")

    def test_prose_reply_flag_counts_and_form_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_manifest(prepare(Path(tmp), V2_CORPUS, holdout_count=0))
            by_family = {(r["subject_family"], r["date_first"]): r for r in rows}
            warranty = by_family[("warranty-question-about-widget", "2025-06-01")]
            self.assertTrue(warranty["prose_reply"])
            self.assertEqual(warranty["prose_reply_count"], 1)
            self.assertEqual(warranty["occurrences"], 3)
            self.assertTrue(warranty["attachments"])
            self.assertEqual(warranty["direction"], "external_first")
            order = by_family[("order", "2024-02-01")]
            self.assertFalse(order["prose_reply"], "a 2-char mailbox reply is not prose")
            self.assertEqual(order["prose_reply_count"], 0)
            self.assertEqual(order["mailbox_message_count"], 1)
            contact = by_family[("contact", "2026-01-01")]
            self.assertTrue(contact["form_source"])
            self.assertFalse(contact["prose_reply"])

    def test_v3_prepare_emits_structural_manifest_and_counts_only_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V3_CORPUS, holdout_count=0)
            rows = read_manifest(scratch)
            self.assertTrue(all(row["source_format"] == "v3" for row in rows))
            self.assertTrue(all(row["structural_messages"] for row in rows))
            billing = next(row for row in rows if row["subject_family"] == "billing-question")
            self.assertEqual(billing["message_directions"], {"inbound": 1, "outbound": 1})
            self.assertEqual(billing["actor_roles"], {"contact": 1, "human_admin": 1})
            diagnostics = json.loads((scratch / "diagnostics.json").read_text())
            self.assertTrue(diagnostics["available"])
            self.assertEqual(diagnostics["accepted_count"], 2)
            self.assertEqual(diagnostics["deduplicated"], 1)
            human = (scratch / "diagnostics.md").read_text()
            self.assertIn("automation_only: 7", human)
            self.assertIn("[2025-01-01T00:00:00Z, 2025-07-01T00:00:00Z): 11", human)
            for private in ("Login question", "Billing question", "account", "invoice", "@"):
                self.assertNotIn(private, human)

    def test_legacy_diagnostics_use_accepted_interactions_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = V2_CORPUS.replace("accepted_interactions: 8", "accepted_interactions: 13")
            scratch = prepare(Path(tmp), corpus, holdout_count=0)
            diagnostics = json.loads((scratch / "diagnostics.json").read_text())
            self.assertFalse(diagnostics["available"])
            self.assertEqual(diagnostics["accepted_count"], 13)

    def test_v1_end_to_end_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V1_CORPUS, holdout_count=0)
            rows = read_manifest(scratch)
            self.assertEqual([r["source_format"] for r in rows], ["v1", "v1"])
            delivery = next(r for r in rows if r["subject_family"] == "delivery-issue")
            self.assertEqual(delivery["date_first"], "2019-03-04")
            self.assertEqual(delivery["date_last"], "2019-03-05")
            self.assertTrue(delivery["prose_reply"])
            self.assertTrue(delivery["attachments"])
            self.assertEqual(delivery["mailbox_message_count"], 1)
            self.assertEqual(read_ledger(scratch)["corpus"]["format"], "v1")
            self.assertEqual(ph.verify_scratch(scratch), [])

    def test_era_band_boundaries(self):
        cfg = dict(ph.DEFAULTS)
        harvested = date(2026, 1, 2)
        self.assertEqual(ph.era_band("2024-01-02", harvested, cfg), "recent")
        self.assertEqual(ph.era_band("2023-12-02", harvested, cfg), "mid")
        self.assertEqual(ph.era_band("2020-01-02", harvested, cfg), "mid")
        self.assertEqual(ph.era_band("2019-12-02", harvested, cfg), "old")
        self.assertEqual(ph.era_band(None, harvested, cfg), "old")
        self.assertEqual(ph.era_band("2024-01-02", None, cfg), "old")

    def test_risk_markers_deep_read_and_over_cap_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0, risk_cap=0.9)
            rows = {r["date_first"]: r for r in read_manifest(scratch)}
            self.assertIn("payment_dispute", rows["2010-05-01"]["risk_markers"])
            self.assertIn("refund", rows["2010-05-01"]["risk_markers"])
            ledger = read_ledger(scratch)
            self.assertFalse(ledger["risk"]["over_cap"])
            self.assertGreater(ledger["risk"]["flagged"], 1,
                               "ambiguous forced-deep threads share the bounded risk gate")
            self.assertGreater(ledger["risk"]["share"], 0.125)
            self.assertGreaterEqual(ledger["risk"]["by_marker"]["payment_dispute"], 1)
            risky_id = rows["2010-05-01"]["id"]
            cluster = next(c for c in read_clusters(scratch) if risky_id in c["thread_ids"])
            self.assertIn(risky_id, cluster["deep_read_ids"])
            self.assertNotIn(risky_id, cluster["sample_ids"])

    def test_risk_over_cap_is_reported_but_prepare_still_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0, risk_cap=0.1)
            ledger = read_ledger(scratch)
            self.assertTrue(ledger["risk"]["over_cap"])
            self.assertEqual(ledger["risk"]["by_marker"]["payment_dispute"], 1)
            self.assertEqual(ledger["risk"]["by_marker"]["refund"], 1)
            self.assertGreaterEqual(ledger["risk"]["by_marker"]["ambiguous_generic_subject"], 1)
            # report-only: the ledger is intact and reading plans stayed capped
            self.assertEqual(ph.verify_scratch(scratch), [])
            for cluster in read_clusters(scratch):
                self.assertLessEqual(len(cluster["sample_ids"]), ph.DEFAULTS["sample_cap"])

    def test_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            corpus = write_corpus(tmp_path, V2_CORPUS)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"risk_cap": 0.9, "unknown_knob": 1}), encoding="utf-8")
            args = SimpleNamespace(corpus=str(corpus), scratch=str(tmp_path / "scratch"),
                                   config=str(config), holdout=1, seed=7, export_id="exp-test")
            cfg = ph.load_config(args)
            self.assertEqual(cfg["risk_cap"], 0.9)
            self.assertEqual(cfg["holdout_count"], 1)
            self.assertEqual(cfg["seed"], 7)
            self.assertNotIn("unknown_knob", cfg)
            with mock.patch.object(ph, "check_safe_output", return_value=tmp_path):
                Path(args.scratch).mkdir()
                write_json(Path(args.scratch) / "preflight.json",
                           ph.synthetic_preflight("exp-test", repo_root=str(tmp_path)))
                self.assertEqual(ph.cmd_prepare(args), 0)
            self.assertFalse(read_ledger(Path(args.scratch))["risk"]["over_cap"])

    def test_prepare_rejects_preflight_from_another_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = write_corpus(root, V2_CORPUS)
            scratch = root / "scratch"
            scratch.mkdir()
            write_json(scratch / "preflight.json",
                       ph.synthetic_preflight("exp-test", repo_root=str(root / "other")))
            args = SimpleNamespace(corpus=str(corpus), scratch=str(scratch), config=None,
                                   holdout=1, seed=0, export_id="exp-test")
            with mock.patch.object(ph, "check_safe_output", return_value=root):
                with self.assertRaisesRegex(ph.HarvestError, "different brain checkout"):
                    ph.cmd_prepare(args)


class HoldoutAndLedgerTests(unittest.TestCase):
    def test_ledger_expand_makes_still_yielding_followup_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0, sample_cap=1, risk_cap=1)
            ledger = read_ledger(scratch)
            cluster = next(item for item in read_clusters(scratch)
                           if len(item["thread_ids"]) > len(item["sample_ids"]) + len(item["deep_read_ids"]))
            deep, sampled = cluster["deep_read_ids"], cluster["sample_ids"]
            report = {"cluster": cluster["id"], "read_deep": deep, "read_sampled": sampled,
                      "route_elsewhere": [], "contradictions": [],
                      "saturation": {"still_yielding": True, "note": "new rule at cap"},
                      "counts": {"assigned": len(cluster["thread_ids"]),
                                 "read": len(deep) + len(sampled)}}
            report_path = write_json(scratch / "drafts" / f"{cluster['id']}.report.json", report)
            merged_ledger, merged_clusters = ph.apply_reports(scratch, [report_path])
            write_json(scratch / "ledger.json", merged_ledger)
            write_json(scratch / "clusters.json", merged_clusters)
            before = set(ledger["reading_plan"]["sample_ids"])
            self.assertEqual(ph.main(["ledger", "expand", "--scratch", str(scratch),
                                      "--cluster", cluster["id"], "--count", "1"]), 0)
            after = read_ledger(scratch)
            self.assertEqual(len(set(after["reading_plan"]["sample_ids"]) - before), 1)
            self.assertEqual(after["followups"][0]["trigger"], "still_yielding")
            self.assertEqual(ph.verify_scratch(scratch), [])

    def test_holdout_reserved_eligible_and_absent_from_all_reading_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            holdout = json.loads((scratch / "holdout.json").read_text(encoding="utf-8"))
            self.assertEqual(holdout["count"], 1)
            rows = {r["id"]: r for r in read_manifest(scratch)}
            for hid in holdout["ids"]:
                self.assertTrue(rows[hid]["holdout"])
                self.assertIsNone(rows[hid]["cluster"])
                self.assertTrue(rows[hid]["prose_reply"], "holdout needs a real human answer")
                for cluster in read_clusters(scratch):
                    for key in ("thread_ids", "sample_ids", "deep_read_ids"):
                        self.assertNotIn(hid, cluster[key], f"{key} of {cluster['id']}")
            ledger = read_ledger(scratch)
            self.assertEqual(ledger["holdout"]["ids"], holdout["ids"])
            self.assertEqual(ledger["threads"][holdout["ids"][0]]["status"], "holdout")
            # only the long-external prose-answered thread is eligible in this corpus
            self.assertEqual(rows[holdout["ids"][0]]["date_first"], "2022-03-01")
            replay = json.loads((scratch / "replay-cases.json").read_text(encoding="utf-8"))
            self.assertEqual(replay["count"], 1)
            self.assertEqual(replay["cases"], holdout["replay_cases"])
            self.assertIn("would like to know", replay["cases"][0]["question"])
            self.assertIn("arrange an inspection", replay["cases"][0]["historical_answer"])

    def test_holdout_rejects_outbound_first_and_automated_and_redacts_replay(self):
        corpus = """---
harvest_format: v2
harvested_at: 2026-01-02T03:04:05Z
---

## Detailed billing question — #1
**external (2025-01-01):**
Dear Alice, could you explain invoice INV-ABCD1234 for alice@example.test? See https://private.test/a or call +32 470 12 34 56 because we would like to know what happened.
**mailbox (2025-01-02):**
We checked the invoice carefully and can confirm the correction. Write to owner@example.test if anything remains unclear.

## Outbound campaign — #2
**mailbox (2025-02-01):**
We wanted to send you this campaign introduction with enough prose to pass the simple reply threshold.
**external (2025-02-02):**
Could you explain this campaign and what it means for us because we would like to know all details?
**mailbox (2025-02-03):**
We can explain the campaign with this long human-looking answer, but the thread started outbound.

## Automated question — #3
**external (2025-03-01):**
This is an automated notification. Could you explain why this delivery status notification was sent to us?
**mailbox (2025-03-02):**
We can explain this automated notification with a sufficiently long prose-looking mailbox message.
"""
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), corpus, holdout_count=1, holdout_min_external_chars=20)
            replay = json.loads((scratch / "replay-cases.json").read_text(encoding="utf-8"))
            self.assertEqual(replay["count"], 1)
            encoded = json.dumps(replay)
            for private in ("Alice", "alice@example.test", "owner@example.test", "private.test",
                            "+32 470 12 34 56", "INV-ABCD1234"):
                self.assertNotIn(private, encoded)
            for marker in ("[name]", "[email]", "[link]", "[phone]", "[identifier]"):
                self.assertIn(marker, encoded)

    def test_verify_catches_double_assignment_and_missing_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            self.assertEqual(ph.main(["verify", "--scratch", str(scratch)]), 0)
            clusters_doc = json.loads((scratch / "clusters.json").read_text(encoding="utf-8"))
            moved = clusters_doc["clusters"][0]["thread_ids"][0]
            mixed = next(c for c in clusters_doc["clusters"] if c["id"] == "mixed")
            mixed["thread_ids"].append(moved)
            mixed["size"] = len(mixed["thread_ids"])
            (scratch / "clusters.json").write_text(json.dumps(clusters_doc), encoding="utf-8")
            violations = "\n".join(ph.verify_scratch(scratch))
            self.assertIn("appears in 2 cluster lists", violations)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["verify", "--scratch", str(scratch)]), 1)
            self.assertIn("FAILED", stderr.getvalue())

            ledger = read_ledger(scratch)
            dropped = sorted(ledger["threads"])[-1]
            del ledger["threads"][dropped]
            (scratch / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            violations = "\n".join(ph.verify_scratch(scratch))
            self.assertIn(f"{dropped}: in manifest but absent from ledger", violations)

    def test_ledger_apply_moves_route_elsewhere_and_records_read_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            cluster = next(c for c in read_clusters(scratch) if c["id"] == "C01")
            moved, sampled = cluster["thread_ids"][0], cluster["thread_ids"][1]
            report = {"cluster": "C01", "read_deep": [moved], "read_sampled": [sampled],
                      "route_elsewhere": [{"id": moved, "suggested_cluster": "mixed",
                                           "reason": "actually a generic inquiry"}],
                      "contradictions": [], "saturation": {"still_yielding": False, "note": ""},
                      "counts": {"assigned": len(cluster["thread_ids"]), "read": 2}}
            report_path = scratch / "drafts" / "C01.report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(ph.main(["ledger", "apply", "--scratch", str(scratch),
                                      str(report_path)]), 0)
            ledger = read_ledger(scratch)
            self.assertEqual(ledger["threads"][moved]["cluster"], "mixed")
            self.assertEqual(ledger["threads"][moved]["routed_to"], "mixed")
            self.assertEqual(ledger["threads"][moved]["read"], "deep")
            self.assertEqual(ledger["threads"][sampled]["read"], "sampled")
            self.assertIsNone(ledger["threads"][sampled]["routed_to"])
            clusters = {c["id"]: c for c in read_clusters(scratch)}
            self.assertNotIn(moved, clusters["C01"]["thread_ids"])
            self.assertIn(moved, clusters["mixed"]["thread_ids"])
            self.assertEqual(clusters["C01"]["size"], len(clusters["C01"]["thread_ids"]))
            self.assertEqual(ph.verify_scratch(scratch), [])
            # Re-entering ledger apply with the same complete cluster report is a no-op.
            self.assertEqual(ph.main(["ledger", "apply", "--scratch", str(scratch),
                                      str(report_path)]), 0)
            self.assertEqual(ph.verify_scratch(scratch), [])

    def test_ledger_apply_hard_fails_on_holdout_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=1)
            holdout_id = read_ledger(scratch)["holdout"]["ids"][0]
            self.assertFalse((scratch / "threads" / f"{holdout_id}.md").exists(),
                             "raw holdout must not enter the synthesis-readable thread tree")
            report = {"cluster": "C01", "read_deep": [holdout_id], "read_sampled": [],
                      "route_elsewhere": [{"id": holdout_id, "suggested_cluster": "mixed",
                                           "reason": "must fail"}],
                      "contradictions": [], "saturation": {"still_yielding": False, "note": ""},
                      "counts": {"assigned": 1, "read": 1}}
            report_path = scratch / "drafts" / "C01.report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["ledger", "apply", "--scratch", str(scratch),
                                          str(report_path)]), 2)
            self.assertIn("holdout leakage", stderr.getvalue())
            record = read_ledger(scratch)["threads"][holdout_id]
            self.assertEqual(record["status"], "holdout")
            self.assertEqual(record["read"], "none", "holdouts are reserved for the evaluation")
            self.assertIsNone(record["routed_to"])
            self.assertEqual(ph.verify_scratch(scratch), [])


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def build_step10_fixture(tmp: Path, holdout_count: int = 1) -> dict:
    scratch = prepare(tmp, V2_CORPUS, holdout_count=holdout_count, sample_cap=1, risk_cap=1,
                      export_id="exp-2026-01-02-safe")
    manifest = read_manifest(scratch)
    ledger = read_ledger(scratch)
    origins: dict[str, set[str]] = {}
    for row in manifest:
        if row["cluster"]:
            origins.setdefault(row["cluster"], set()).add(row["id"])
    planned_deep = set(ledger["reading_plan"]["deep_read_ids"])
    planned_sampled = set(ledger["reading_plan"]["sample_ids"])
    reports = []
    for cluster, members in sorted(origins.items()):
        deep = sorted(members & planned_deep)
        sampled = sorted(members & planned_sampled)
        report = {
            "cluster": cluster,
            "read_deep": deep,
            "read_sampled": sampled,
            "route_elsewhere": [],
            "contradictions": [],
            "saturation": {"still_yielding": False, "note": "sample saturated"},
            "counts": {"assigned": len(members), "read": len(deep) + len(sampled)},
        }
        reports.append(write_json(scratch / "drafts" / f"{cluster}.report.json", report))
    merged_ledger, merged_clusters = ph.apply_reports(scratch, reports)
    write_json(scratch / "ledger.json", merged_ledger)
    write_json(scratch / "clusters.json", merged_clusters)
    ledger = merged_ledger

    verification_dir = scratch / "settings-verification"
    verification_dir.mkdir()
    before_settings = write_json(verification_dir / "persona-before.json",
                                 {"persona": {"guidance": "verbose"}})
    after_settings = write_json(verification_dir / "persona-after.json",
                                {"persona": {"guidance": "concise"}})
    reduction = write_json(scratch / "critic" / "reduced.json", {
        "settings_changes": [{
            "surface": "persona", "scope": "tenant", "status": "applied",
            "summary": "Prefer concise answers", "scope_authority": True,
            "verification": {
                "pre_read_at": "2026-01-02T03:00:00Z",
                "post_read_at": "2026-01-02T03:01:00Z",
                "before_file": "settings-verification/persona-before.json",
                "after_file": "settings-verification/persona-after.json",
                "before_sha256": ph.hashlib.sha256(before_settings.read_bytes()).hexdigest(),
                "after_sha256": ph.hashlib.sha256(after_settings.read_bytes()).hexdigest(),
                "resolved_scope": "tenant", "resolved_target": "tenant-test",
            },
        }],
        "skip_proposals": [{
            "summary": "Skip repeated automated receipts",
            "evidence_class": "presence_without_prose_reply", "evidence_count": 1,
            "evidence_ids": [next(row["id"] for row in manifest
                                  if not row["prose_reply"] and row["occurrences"] == 1
                                  and row["id"] not in ledger["holdout"]["ids"])],
        }],
        "durable_rules": [{
            "summary": "Warranty handling is stable", "evidence_strength": 1,
            "evidence_ids": [next(thread_id for thread_id, row in ledger["threads"].items()
                                  if row["read"] != "none")],
            "era": "recent", "stale_era": False,
        }],
        "contradictions": [{
            "topic": "warranty timing", "status": "resolved",
            "resolution": "recent evidence supersedes old handling", "supersession": "old -> recent",
        }],
    })
    holdout_ids = ledger["holdout"]["ids"]
    holdout_id = holdout_ids[0]
    evaluation_value = {
        "holdouts": [{
            "id": thread_id, "replay_id": f"ask-replay-{index}",
            "status": "succeeded", "trace_url": f"https://app.example.test/runs/holdout-{index}",
            "brain_sha": "a" * 40,
            "scores": {"factual_agreement": 4, "routing": 3, "tone": 4},
            "notes": "Checked against private answer for alice@example.test at https://private.invalid/raw",
        } for index, thread_id in enumerate(holdout_ids, start=1)],
        "production_replay": {
            "run_id": "run-representative-1", "status": "succeeded", "turns": 12,
            "trace_url": "https://app.example.test/runs/representative",
            "brain_sha": "a" * 40, "brain_diff": "Added the new warranty route",
        },
    }
    evaluation = write_json(scratch / "brief" / "evaluation.json", evaluation_value)
    metrics = write_json(scratch / "brief" / "metrics.json", {
        "turns": 42, "wall_clock_seconds": 90.25, "preparation_seconds": 0.25,
    })
    return {"scratch": scratch, "reports": reports, "reduction": reduction,
            "evaluation": evaluation, "metrics": metrics, "holdout_id": holdout_id}


def review_argv(fixture: dict) -> list[str]:
    argv = ["review", "--scratch", str(fixture["scratch"])]
    for report in fixture["reports"]:
        argv.extend(["--agent-report", str(report)])
    argv.extend(["--reduction", str(fixture["reduction"]),
                 "--evaluation", str(fixture["evaluation"]),
                 "--metrics", str(fixture["metrics"]),
                 "--harvest-date", "2026-01-03", "--kit-version", "v0.3.0"])
    return argv


class Step10ReviewAndRecordTests(unittest.TestCase):
    def test_review_embeds_counts_only_source_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            write_json(fixture["scratch"] / "diagnostics.json", {
                "schema_version": 1, "available": True, "accepted_count": 8,
                "rejection_reasons": {"automation_only": 12, "no_later_admin_reply": 4},
                "actor_types": {"automation": 12, "contact": 9, "human_admin": 8},
                "initiation_types": {"email": 19},
                "candidate_bands": [{"start": "2025-01-01T00:00:00Z",
                                     "end": "2026-01-01T00:00:00Z", "count": 26}],
                "deduplicated": 2,
            })
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            brief = (fixture["scratch"] / "brief" / "review-brief.md").read_text()
            self.assertIn("Accepted: 8 · Deduplicated: 2", brief)
            self.assertIn("automation_only=12", brief)
            self.assertIn("Candidate bands: 1; candidates: 26", brief)

    def test_review_rejects_holdout_content_without_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            replay = json.loads((fixture["scratch"] / "replay-cases.json").read_text())
            leaked = replay["cases"][0]["historical_answer"]
            (fixture["scratch"] / "drafts" / "copied-holdout.md").write_text(leaked)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("holdout content leakage", stderr.getvalue())

    def test_review_scans_explicit_synthesis_inputs_outside_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = build_step10_fixture(root)
            replay = json.loads((fixture["scratch"] / "replay-cases.json").read_text())
            report = json.loads(fixture["reports"][0].read_text())
            report["saturation"]["note"] = replay["cases"][0]["question"]
            external_report = write_json(root / "external-report.json", report)
            fixture["reports"][0] = external_report
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("holdout content leakage in synthesis input", stderr.getvalue())

    def test_evaluation_requires_distinct_holdout_and_production_runs(self):
        ids = {"H" + "0" * 32, "H" + "1" * 32}
        evaluation = {
            "holdouts": [{
                "id": thread_id, "replay_id": "duplicate-run", "status": "succeeded",
                "trace_url": "https://app.example.test/runs/duplicate", "brain_sha": "a" * 40,
                "scores": {"factual_agreement": 4, "routing": 3, "tone": 4}, "notes": "",
            } for thread_id in sorted(ids)],
            "production_replay": {
                "run_id": "representative", "status": "succeeded", "turns": 12,
                "trace_url": "https://app.example.test/runs/representative",
                "brain_sha": "a" * 40, "brain_diff": "changed",
            },
        }
        with self.assertRaisesRegex(ph.HarvestError, "distinct replay id and trace URL"):
            ph.validate_evaluation(evaluation, ids)

        evaluation["holdouts"][1]["replay_id"] = "holdout-2"
        evaluation["holdouts"][1]["trace_url"] = "https://app.example.test/runs/holdout-2"
        evaluation["production_replay"]["run_id"] = "duplicate-run"
        with self.assertRaisesRegex(ph.HarvestError, "distinct from every holdout replay"):
            ph.validate_evaluation(evaluation, ids)

    def test_review_requires_bound_before_after_settings_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            snapshot = fixture["scratch"] / "settings-verification" / "persona-after.json"
            snapshot.write_text('{"persona":{"guidance":"tampered"}}')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("after settings snapshot digest changed", stderr.getvalue())

    def test_review_rejects_mailbox_scoped_harvested_persona(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            reduction = json.loads(fixture["reduction"].read_text())
            reduction["settings_changes"][0]["scope"] = "mailbox"
            reduction["settings_changes"][0]["verification"].update(
                {"resolved_scope": "mailbox", "resolved_target": "mailbox-test"})
            write_json(fixture["reduction"], reduction)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("harvested persona must use tenant or project scope", stderr.getvalue())

    def test_review_accepts_project_persona_for_flat_harvest(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            reduction = json.loads(fixture["reduction"].read_text())
            reduction["settings_changes"][0]["scope"] = "project"
            reduction["settings_changes"][0]["verification"].update(
                {"resolved_scope": "project", "resolved_target": "project-test"})
            write_json(fixture["reduction"], reduction)

            preflight_path = fixture["scratch"] / "preflight.json"
            preflight = json.loads(preflight_path.read_text())
            preflight["target"]["tenant"] = ""
            preflight["scope_matrix"]["persona"].update({
                "available_scopes": ["mailbox", "project"],
                "narrowest_target": "project",
                "target_available": True,
            })
            for key in ("triage_policy", "hard_rules"):
                preflight["scope_matrix"][key].update({
                    "available_scopes": ["project"],
                    "narrowest_target": "project",
                    "target_available": True,
                })
            write_json(preflight_path, preflight)
            run_path = fixture["scratch"] / "run.json"
            run = json.loads(run_path.read_text())
            run["target"]["tenant"] = ""
            run["preflight"]["sha256"] = ph.document_digest(preflight)
            write_json(run_path, run)

            self.assertEqual(ph.main(review_argv(fixture)), 0)

    def test_review_preserves_previous_bundle_on_late_validation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            brief_dir = fixture["scratch"] / "brief"
            before = {name: (brief_dir / name).read_bytes()
                      for name in ("review-brief.md", "record-source.json", "record-candidate.json")}
            metrics = json.loads(fixture["metrics"].read_text())
            metrics["turns"] = 0
            write_json(fixture["metrics"], metrics)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertEqual(before, {name: (brief_dir / name).read_bytes() for name in before})

    def test_review_rejects_changed_preflight_and_risk_over_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            preflight_path = fixture["scratch"] / "preflight.json"
            preflight = json.loads(preflight_path.read_text())
            preflight["target"]["mailbox"] = "different-mailbox"
            write_json(preflight_path, preflight)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("preflight changed", stderr.getvalue())

            (Path(tmp) / "risk").mkdir()
            fixture = build_step10_fixture(Path(tmp) / "risk")
            ledger = read_ledger(fixture["scratch"])
            ledger["risk"]["over_cap"] = True
            write_json(fixture["scratch"] / "ledger.json", ledger)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("risk.over_cap", stderr.getvalue())

            (Path(tmp) / "access").mkdir()
            fixture = build_step10_fixture(Path(tmp) / "access")
            preflight_path = fixture["scratch"] / "preflight.json"
            preflight = json.loads(preflight_path.read_text())
            preflight["access"]["read"]["persona"] = False
            preflight["scope_matrix"]["persona"]["target_available"] = False
            preflight["scope_matrix"]["persona"]["available_scopes"] = []
            write_json(preflight_path, preflight)
            run_path = fixture["scratch"] / "run.json"
            run = json.loads(run_path.read_text())
            run["preflight"]["sha256"] = ph.document_digest(preflight)
            write_json(run_path, run)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("setting/target read", stderr.getvalue())

            preflight["access"]["read"]["persona"] = True
            preflight["scope_matrix"]["persona"]["target_available"] = True
            preflight["scope_matrix"]["persona"]["available_scopes"] = ["tenant"]
            preflight["access"]["write"]["persona"] = False
            preflight["scope_matrix"]["persona"]["write_verified"] = False
            write_json(preflight_path, preflight)
            run["preflight"]["sha256"] = ph.document_digest(preflight)
            write_json(run_path, run)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("without verified write access", stderr.getvalue())

    def test_review_rejects_unverified_setting_scope_and_torn_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = build_step10_fixture(root)
            reduction = json.loads(fixture["reduction"].read_text())
            reduction["settings_changes"][0].update({
                "surface": "triage_policy", "scope": "tenant", "scope_authority": True,
            })
            reduction["settings_changes"][0]["verification"].update(
                {"resolved_scope": "tenant", "resolved_target": "tenant-test"})
            write_json(fixture["reduction"], reduction)
            preflight_path = fixture["scratch"] / "preflight.json"
            preflight = json.loads(preflight_path.read_text())
            preflight["scope_matrix"]["triage_policy"]["available_scopes"] = ["project"]
            preflight["scope_matrix"]["triage_policy"]["target_available"] = False
            run_path = fixture["scratch"] / "run.json"
            run = json.loads(run_path.read_text())
            run["preflight"]["sha256"] = ph.document_digest(preflight)
            write_json(preflight_path, preflight)
            write_json(run_path, run)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("verified tenant triage_policy target/read", stderr.getvalue())

            reduction["settings_changes"] = []
            write_json(fixture["reduction"], reduction)
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            brief = fixture["scratch"] / "brief" / "review-brief.md"
            brief.write_text(brief.read_text() + "torn\n")
            output = root / "notes" / "harvest-record.json"
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(["record", "--scratch", str(fixture["scratch"]),
                                          "--out", str(output), "--approved"]), 2)
            self.assertIn("bundle is incomplete or mixed", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_reduction_evidence_is_machine_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            reduction = json.loads(fixture["reduction"].read_text())
            reduction["skip_proposals"][0]["evidence_count"] += 1
            write_json(fixture["reduction"], reduction)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("occurrence sum", stderr.getvalue())

            (Path(tmp) / "durable").mkdir()
            fixture = build_step10_fixture(Path(tmp) / "durable")
            reduction = json.loads(fixture["reduction"].read_text())
            unread = next(thread_id for thread_id, row in read_ledger(fixture["scratch"])["threads"].items()
                          if row["status"] == "assigned" and row["read"] == "none")
            reduction["durable_rules"][0]["evidence_ids"] = [unread]
            write_json(fixture["reduction"], reduction)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("not semantically read", stderr.getvalue())

    def test_review_parser_accepts_single_or_repeated_agent_report_flags(self):
        common = ["--scratch", "scratch", "--reduction", "reduced.json",
                  "--evaluation", "evaluation.json", "--metrics", "metrics.json",
                  "--harvest-date", "2026-01-03", "--kit-version", "v0.3.0"]
        single = ph.parser().parse_args(["review", "--agent-report", "a.json", "b.json", *common])
        repeated = ph.parser().parse_args(["review", "--agent-report", "a.json",
                                           "--agent-report", "b.json", *common])
        self.assertEqual(single.agent_reports, ["a.json", "b.json"])
        self.assertEqual(repeated.agent_reports, single.agent_reports)

    def test_review_and_approved_record_are_deterministic_and_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = build_step10_fixture(root)
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            brief_dir = fixture["scratch"] / "brief"
            first = {name: (brief_dir / name).read_bytes()
                     for name in ("review-brief.md", "record-source.json", "record-candidate.json")}
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            for name, expected in first.items():
                self.assertEqual((brief_dir / name).read_bytes(), expected, name)

            brief = first["review-brief.md"].decode()
            self.assertIn("Turns: 42", brief)
            self.assertIn("Wall clock: 90.250s (preparation 0.250s)", brief)
            self.assertIn("at tenant-test", brief)
            self.assertIn("Resolved brain SHA: `" + "a" * 40 + "`", brief)
            candidate = first["record-candidate.json"]
            self.assertNotIn(fixture["holdout_id"].encode(), candidate)
            self.assertNotIn(b"alice@example.test", candidate)
            self.assertNotIn(b"private.invalid", candidate)
            self.assertNotIn(b"run-representative-1", candidate)

            output = root / "notes" / "harvest-record.json"
            self.assertEqual(ph.main(["record", "--scratch", str(fixture["scratch"]),
                                      "--out", str(output)]), 2)
            self.assertFalse(output.exists())
            record_args = ph.parser().parse_args(["record", "--scratch", str(fixture["scratch"]),
                                                  "--out", str(output), "--approved"])
            self.assertEqual(ph.cmd_record(record_args, destination_checker=lambda out, scratch: None), 0)
            self.assertEqual(output.read_bytes(), candidate,
                             "approved record must be the exact operator-reviewed candidate")
            self.assertEqual(ph.cmd_record(record_args, destination_checker=lambda out, scratch: None), 0,
                             "identical existing record is an idempotent no-op")
            output.write_text("different\n", encoding="utf-8")
            with self.assertRaisesRegex(ph.HarvestError, "different existing"):
                ph.cmd_record(record_args, destination_checker=lambda out, scratch: None)

    def test_review_reconciles_replay_cases_and_requested_holdout_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            replay_path = fixture["scratch"] / "replay-cases.json"
            replay = json.loads(replay_path.read_text())
            replay["cases"][0]["question"] = "tampered private evaluation question"
            write_json(replay_path, replay)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("does not reconcile", stderr.getvalue())

            # Regenerate, then prove a short reservation cannot masquerade as the requested set.
            short_root = Path(tmp) / "short"
            short_root.mkdir()
            fixture = build_step10_fixture(short_root)
            run_path = fixture["scratch"] / "run.json"
            run = json.loads(run_path.read_text())
            run["config"]["holdout_count"] = 2
            write_json(run_path, run)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("does not match requested", stderr.getvalue())

    def test_prepare_fails_when_requested_holdout_cannot_be_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ph.HarvestError, "requested 2, found 1"):
                prepare(Path(tmp), V2_CORPUS, holdout_count=2)

    def test_holdout_leakage_in_reduction_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            reduction = json.loads(fixture["reduction"].read_text())
            reduction["durable_rules"][0]["summary"] += " from " + fixture["holdout_id"]
            write_json(fixture["reduction"], reduction)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("holdout leakage", stderr.getvalue())
            self.assertFalse((fixture["scratch"] / "brief" / "record-candidate.json").exists())

    def test_review_scans_unlisted_draft_and_critic_artifacts_for_holdout_leakage(self):
        for directory, filename in (("drafts", "notes.md"), ("critic", "early-critic.txt")):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp:
                fixture = build_step10_fixture(Path(tmp))
                (fixture["scratch"] / directory / filename).write_text(
                    "Private evidence from " + fixture["holdout_id"], encoding="utf-8")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(ph.main(review_argv(fixture)), 2)
                self.assertIn(f"{directory}/{filename}", stderr.getvalue())
                self.assertFalse((fixture["scratch"] / "brief" / "record-candidate.json").exists())

    def test_evaluation_score_and_production_replay_schema_failures(self):
        stable_id = "H" + "0" * 32
        holdout = {stable_id}
        valid = {
            "holdouts": [{"id": stable_id, "replay_id": "replay-1",
                          "status": "succeeded", "trace_url": "https://example.test/holdout/1",
                          "brain_sha": "b" * 40,
                          "scores": {"factual_agreement": 4, "routing": 4, "tone": 4},
                          "notes": ""}],
            "production_replay": {"run_id": "run-1", "status": "succeeded", "turns": 12,
                                  "trace_url": "https://example.test/run/1", "brain_sha": "b" * 40,
                                  "brain_diff": "one route changed"},
        }
        ph.validate_evaluation(valid, holdout)
        cases = []
        duplicate = json.loads(json.dumps(valid))
        duplicate["holdouts"].append(duplicate["holdouts"][0])
        cases.append((duplicate, "duplicate score"))
        bad_score = json.loads(json.dumps(valid))
        bad_score["holdouts"][0]["scores"]["tone"] = 5
        cases.append((bad_score, "<= 4"))
        bad_sha = json.loads(json.dumps(valid))
        bad_sha["production_replay"]["brain_sha"] = "main"
        cases.append((bad_sha, "40-char SHA"))
        no_diff = json.loads(json.dumps(valid))
        del no_diff["production_replay"]["brain_diff"]
        cases.append((no_diff, "missing"))
        bad_trace = json.loads(json.dumps(valid))
        bad_trace["production_replay"]["trace_url"] = "local-run"
        cases.append((bad_trace, "HTTP"))
        failed_holdout = json.loads(json.dumps(valid))
        failed_holdout["holdouts"][0]["status"] = "failed"
        cases.append((failed_holdout, "successful"))
        mismatched_sha = json.loads(json.dumps(valid))
        mismatched_sha["holdouts"][0]["brain_sha"] = "c" * 40
        cases.append((mismatched_sha, "same dev brain SHA"))
        for value, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ph.HarvestError, error):
                ph.validate_evaluation(value, holdout)

    def test_report_coverage_completeness_and_out_of_plan_reads_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            incomplete = dict(fixture)
            incomplete["reports"] = fixture["reports"][:-1]
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(incomplete)), 2)
            self.assertIn("cover every", stderr.getvalue())

            ledger = read_ledger(fixture["scratch"])
            planned = set(ledger["reading_plan"]["deep_read_ids"] + ledger["reading_plan"]["sample_ids"])
            manifest = read_manifest(fixture["scratch"])
            unplanned = next(row for row in manifest if row["cluster"] and row["id"] not in planned)
            report_path = next(path for path in fixture["reports"]
                               if json.loads(path.read_text())["cluster"] == unplanned["cluster"])
            report = json.loads(report_path.read_text())
            report["read_sampled"].append(unplanned["id"])
            report["counts"]["read"] += 1
            write_json(report_path, report)
            # Reflecting the extra read in the ledger must not make an out-of-plan read acceptable.
            ledger["threads"][unplanned["id"]]["read"] = "sampled"
            write_json(fixture["scratch"] / "ledger.json", ledger)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("out-of-plan", stderr.getvalue())

    def test_still_yielding_requires_completed_follow_up_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_step10_fixture(Path(tmp))
            report = json.loads(fixture["reports"][0].read_text())
            report["saturation"] = {"still_yielding": True, "note": "new rules at the cap"}
            write_json(fixture["reports"][0], report)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(review_argv(fixture)), 2)
            self.assertIn("follow-up assignment", stderr.getvalue())

    def test_record_candidate_tampering_and_private_fields_are_rejected_or_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = build_step10_fixture(root)
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            candidate_path = fixture["scratch"] / "brief" / "record-candidate.json"
            candidate = json.loads(candidate_path.read_text())
            serialized = json.dumps(candidate)
            for forbidden in ("replay_id", "run_id", "trace_url", "brain_sha", "brain_diff",
                              fixture["holdout_id"], "alice@example.test"):
                self.assertNotIn(forbidden, serialized)
            candidate["harvest_record"]["holdout"]["cases"][0]["scores"]["tone"] = 0
            write_json(candidate_path, candidate)
            output = root / "record.json"
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(["record", "--scratch", str(fixture["scratch"]),
                                          "--out", str(output), "--approved"]), 2)
            self.assertIn("incomplete or mixed", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_record_rejects_format_only_candidate_byte_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = build_step10_fixture(root)
            self.assertEqual(ph.main(review_argv(fixture)), 0)
            candidate_path = fixture["scratch"] / "brief" / "record-candidate.json"
            candidate_path.write_text(json.dumps(json.loads(candidate_path.read_text())), encoding="utf-8")
            output = root / "record.json"
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(ph.main(["record", "--scratch", str(fixture["scratch"]),
                                          "--out", str(output), "--approved"]), 2)
            self.assertIn("incomplete or mixed", stderr.getvalue())
            self.assertFalse(output.exists())


class SafetyAndCLITests(unittest.TestCase):
    def test_export_identity_allows_omitted_context_but_rejects_conflicts(self):
        expected = {"export_id": "exp-safe", "project": "project-safe",
                    "tenant": "tenant-safe", "mailbox": "mb-safe", "provider": "google"}
        self.assertIsNotNone(ph.find_object_with_identity('{"id":"EXP-SAFE"}', expected))
        self.assertIsNone(ph.find_object_with_identity('{"mailbox":"mb-safe"}', expected))
        for field, value in (("project", "other-project"), ("tenant", "other-tenant"),
                             ("mailbox", "other-mailbox"), ("provider", "microsoft")):
            raw = json.dumps({"id": "exp-safe", field: value})
            self.assertIsNone(ph.find_object_with_identity(raw, expected), field)

    def test_capability_normalization_and_record_destination_safety(self):
        self.assertEqual(ph.normalize_capabilities('{"grants":["CONFIG:WRITE","admin:*"]}'),
                         ["admin:*", "config:write"])
        self.assertTrue(ph.capability_allows(["admin:*"], "triage:write"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            scratch = root / ".replypen" / "harvest" / "x"
            output = root / "notes" / "record.json"
            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=str(root) + "\n")
                return SimpleNamespace(returncode=0 if "ignored" in str(cmd[-1]) else 1)
            ph.validate_record_destination(output, scratch, git_runner=git)
            with self.assertRaisesRegex(ph.HarvestError, "must not be ignored"):
                ph.validate_record_destination(root / "ignored" / "record.json", scratch,
                                               git_runner=git)
            with self.assertRaisesRegex(ph.HarvestError, "inside git root"):
                ph.validate_record_destination(root.parent / "elsewhere.json", scratch,
                                               git_runner=git)

    def test_explicit_preflight_fails_closed_on_auth_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            (scratch / "corpus").mkdir(parents=True)
            write_corpus(scratch / "corpus", V2_CORPUS)
            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=tmp + "\n")
                return SimpleNamespace(returncode=0, stdout="## main\n")
            args = SimpleNamespace(scratch=str(scratch), project="p", mailbox="m",
                                   provider="google", export_id="e")
            with contextlib.redirect_stdout(io.StringIO()):
                code = ph.cmd_preflight(args, rc_runner=lambda argv: (1, "", "denied"),
                                        git_runner=git)
            self.assertEqual(code, 1)
            artifact = json.loads((scratch / "preflight.json").read_text())
            self.assertFalse(artifact["verification"]["auth"])
            self.assertFalse(artifact["verification"]["access"])
    def test_cleanup_requires_yes_removes_root_and_verifies_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = prepare(Path(tmp), V2_CORPUS, holdout_count=0)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["cleanup", "--scratch", str(scratch)]), 2)
            self.assertIn("--yes", stderr.getvalue())
            self.assertTrue(scratch.exists())
            self.assertEqual(ph.main(["cleanup", "--scratch", str(scratch), "--yes"]), 0)
            self.assertFalse(scratch.exists())

    def test_cleanup_refuses_directories_that_are_not_scratch_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            precious = Path(tmp) / "precious"
            precious.mkdir()
            (precious / "notes.txt").write_text("keep me", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(ph.main(["cleanup", "--scratch", str(precious), "--yes"]), 2)
            self.assertTrue((precious / "notes.txt").exists())

    def test_check_safe_output_refuses_stageable_scratch_root(self):
        def fake_git(cmd, **kwargs):
            if cmd[1] == "rev-parse":
                return SimpleNamespace(returncode=0, stdout="/repo\n")
            return SimpleNamespace(returncode=1)  # check-ignore: NOT ignored
        with self.assertRaisesRegex(ph.HarvestError, "refusing stageable scratch root"):
            ph.check_safe_output(Path("/repo/.replypen/harvest/x"), git_runner=fake_git)

        def fake_git_ok(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="/repo\n")
        ph.check_safe_output(Path("/repo/.replypen/harvest/x"), git_runner=fake_git_ok)
        with self.assertRaisesRegex(ph.HarvestError, "inside the current git checkout"):
            ph.check_safe_output(Path("/elsewhere/scratch"), git_runner=fake_git_ok)
        def fake_git_norepo(cmd, **kwargs):
            return SimpleNamespace(returncode=128, stdout="")
        with self.assertRaisesRegex(ph.HarvestError, "git checkout"):
            ph.check_safe_output(Path("/repo/x"), git_runner=fake_git_norepo)

    def test_preflight_warns_without_rc_and_fails_on_bad_corpus_format(self):
        def fake_git(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=str(kwargs.get("cwd", "/repo")) + "\n") \
                if cmd[1] == "rev-parse" else SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            (scratch / "corpus").mkdir(parents=True)
            write_corpus(scratch / "corpus", V2_CORPUS)
            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=tmp + "\n")
                return SimpleNamespace(returncode=0)  # check-ignore: ignored
            args = SimpleNamespace(scratch=str(scratch))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = ph.cmd_preflight(args, rc_runner=lambda argv: None, git_runner=git)
            output = stdout.getvalue()
            self.assertEqual(code, 0, "missing rc degrades to WARN, not failure")
            self.assertEqual(output.count("rp not available"), 11)
            self.assertIn("formats ['v2']", output)
            artifact = json.loads((scratch / "preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["corpus"], {"files": 1, "formats": ["v2"]})
            self.assertFalse(artifact["scope_matrix"]["triage_policy"]["mailbox_scope"])

            write_corpus(scratch / "corpus", V2_CORPUS.replace("harvest_format: v2",
                                                               "harvest_format: v9"), "bad.md")
            calls = []
            def rc(argv):
                calls.append(argv)
                return (0, "ok\n", "")
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = ph.cmd_preflight(args, rc_runner=rc, git_runner=git)
            self.assertEqual(code, 1, "unsupported corpus format must FAIL preflight")
            self.assertEqual(calls, [
                ["auth", "status"],
                ["auth", "access"],
                ["project", "mailbox", "ls", "-o", "json"],
                ["project", "settings", "behavior", "get", "-o", "json"],
                ["project", "triage", "policy", "get", "-o", "json"],
                ["project", "triage", "rules", "ls", "-o", "json"],
                ["dev", "console", "database", "list", "-o", "json"],
                ["dev", "console", "capabilities"],
                ["fleet", "health"],
                ["project", "corpus", "ls", "-o", "json"],
                ["self", "doctor"],
            ])

    def test_preflight_explicit_context_runs_targeted_inventory_and_keeps_raw_rc_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            (scratch / "corpus").mkdir(parents=True)
            write_corpus(scratch / "corpus", V2_CORPUS)

            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=tmp + "\n")
                if cmd[1] == "status":
                    return SimpleNamespace(returncode=0, stdout="## main\n M local.md\n")
                return SimpleNamespace(returncode=0)

            calls = []
            raw_secret = "mailbox-secret-output"

            def rc(argv):
                calls.append(argv)
                if argv[-5:] == ["project", "mailbox", "ls", "-o", "json"]:
                    return (0, json.dumps({"mailboxes": [{"id": "mb-safe", "provider": "google"}],
                                           "private": raw_secret}), "")
                if argv[-2:] == ["auth", "access"]:
                    return (0, json.dumps({"capabilities": ["config:write"]}), "")
                if argv[-4:] == ["project", "corpus", "get", "exp-safe"]:
                    return (0, json.dumps({"id": "exp-safe", "project": "project-safe",
                                           "tenant": "tenant-safe", "mailbox": "mb-safe",
                                           "provider": "google"}), "")
                return (0, raw_secret, "")

            args = SimpleNamespace(scratch=str(scratch), project="project-safe", tenant="tenant-safe",
                                   mailbox="mb-safe", provider="google", export_id="exp-safe")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(ph.cmd_preflight(args, rc_runner=rc, git_runner=git), 0)
            self.assertNotIn(raw_secret, stdout.getvalue())
            artifact = json.loads((scratch / "preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["target"], {"project": "project-safe", "tenant": "tenant-safe",
                                                   "mailbox": "mb-safe", "provider": "google",
                                                   "export_id": "exp-safe"})
            self.assertTrue(artifact["scope_matrix"]["persona"]["target_available"])
            self.assertTrue(all(call[:2] == ["--project", "project-safe"] for call in calls))
            self.assertIn(["--project", "project-safe", "project", "mailbox", "settings", "get",
                           "mb-safe", "-o", "json"], calls)
            self.assertIn(["--project", "project-safe", "project", "tenant", "settings", "get",
                           "tenant-safe", "-o", "json"], calls)
            self.assertIn(["--project", "project-safe", "--tenant", "tenant-safe", "project",
                           "triage", "policy", "get", "-o", "json"], calls)
            self.assertIn(["--project", "project-safe", "--tenant", "tenant-safe", "project",
                           "corpus", "get", "exp-safe"], calls)

    def test_preflight_explicit_context_fails_closed_on_settings_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            (scratch / "corpus").mkdir(parents=True)
            write_corpus(scratch / "corpus", V2_CORPUS)

            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=tmp + "\n")
                if cmd[1] == "status":
                    return SimpleNamespace(returncode=0, stdout="## main\n")
                return SimpleNamespace(returncode=0)

            def rc(argv):
                if argv[-5:] == ["project", "mailbox", "ls", "-o", "json"]:
                    return 0, json.dumps({"mailboxes": [{"id": "mb-safe", "provider": "google"}]}), ""
                if argv[-2:] == ["auth", "access"]:
                    return 0, json.dumps({"capabilities": ["config:write"]}), ""
                if argv[-4:] == ["project", "corpus", "get", "exp-safe"]:
                    return 0, json.dumps({"id": "exp-safe", "project": "project-safe",
                                          "mailbox": "mb-safe", "provider": "google"}), ""
                if argv[-6:] == ["project", "triage", "policy", "get", "-o", "json"]:
                    return 7, "", "denied"
                return 0, "{}", ""

            args = SimpleNamespace(scratch=str(scratch), project="project-safe", mailbox="mb-safe",
                                   provider="google", export_id="exp-safe")
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(ph.cmd_preflight(args, rc_runner=rc, git_runner=git), 1)
            self.assertIn("[FAIL] rp triage policy", stdout.getvalue())
            artifact = json.loads((scratch / "preflight.json").read_text())
            self.assertEqual(artifact["result"], "fail")
            self.assertEqual(artifact["scope_matrix"]["triage_policy"]["available_scopes"], [])
            with self.assertRaisesRegex(ph.HarvestError, "without failed checks"):
                ph.validate_preflight(artifact, expected_export_id="exp-safe")

    def test_preflight_provider_must_belong_to_the_exact_target_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            (scratch / "corpus").mkdir(parents=True)
            write_corpus(scratch / "corpus", V2_CORPUS)

            def git(cmd, **kwargs):
                if cmd[1] == "rev-parse":
                    return SimpleNamespace(returncode=0, stdout=tmp + "\n")
                if cmd[1] == "status":
                    return SimpleNamespace(returncode=0, stdout="## main\n")
                return SimpleNamespace(returncode=0)

            inventory = {"mailboxes": [{"id": "mb-target", "provider": "google"},
                                        {"id": "mb-other", "provider": "microsoft"}]}

            def rc(argv):
                if argv[-5:] == ["project", "mailbox", "ls", "-o", "json"]:
                    return 0, json.dumps(inventory), ""
                return 0, "{}", ""

            args = SimpleNamespace(scratch=str(scratch), project="", mailbox="mb-target",
                                   provider="microsoft", export_id="")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ph.cmd_preflight(args, rc_runner=rc, git_runner=git), 1)
            artifact = json.loads((scratch / "preflight.json").read_text())
            provider_check = next(check for check in artifact["checks"]
                                  if check["name"] == "target provider")
            self.assertEqual(provider_check["status"], "fail")

    def test_preflight_git_failure_does_not_write_private_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"

            def git(cmd, **kwargs):
                return SimpleNamespace(returncode=128, stdout="")

            args = SimpleNamespace(scratch=str(scratch))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ph.cmd_preflight(args, rc_runner=lambda argv: None,
                                                  git_runner=git), 1)
            self.assertFalse((scratch / "preflight.json").exists())


TOPICS = (
    ("Warranty claim for widget", "en"),
    ("Delivery delay for shipment", "en"),
    ("Retourzending van bestelling", "nl"),
    ("Vraag over garantie", "nl"),
    ("Question sur la livraison", "fr"),
    ("Frage zur Lieferung", "de"),
    ("Product manual request", "en"),
    ("Wholesale pricing request", "en"),
)
QUESTION = {
    "en": "Hello, we have a question about this and we would like to hear from you what the next steps are for our situation.",
    "nl": "Beste, wij hebben hierover een vraag en wij zouden graag van u horen wat de volgende stappen zijn voor onze situatie.",
    "fr": "Bonjour, nous avons une question et nous voudrions savoir de vous quelles sont les étapes suivantes pour notre situation.",
    "de": "Hallo, wir haben eine Frage dazu und wir möchten von Ihnen wissen, welche die nächsten Schritte für unsere Situation sind.",
}
DETAIL = {
    "en": " We already looked at the page you sent us and we still cannot find the answer, so please explain it once more for us in detail.",
    "nl": " Wij hebben de pagina al bekeken die u ons stuurde en wij vinden het antwoord nog niet, dus graag nog een keer uitleg voor ons.",
    "fr": " Nous avons déjà regardé la page que vous nous avez envoyée et nous ne trouvons pas la réponse, merci de nous expliquer encore.",
    "de": " Wir haben die Seite schon angesehen, die Sie uns geschickt haben, und wir finden die Antwort nicht, bitte erklären Sie es uns noch einmal.",
}
REPLY = {
    "en": "Thanks for reaching out. We looked at this for you and we will send the details shortly, please let us know if anything is unclear.",
    "nl": "Dank voor uw bericht. Wij hebben dit voor u bekeken en wij sturen de details zo snel mogelijk, laat het ons weten met vragen.",
    "fr": "Merci pour votre message. Nous avons regardé cela pour vous et nous allons envoyer les détails, dites-nous si ce n'est pas clair.",
    "de": "Danke für Ihre Nachricht. Wir haben das für Sie geprüft und wir senden die Details in Kürze, melden Sie sich gerne bei Fragen.",
}


def generate_v2_corpus(n: int = 1000, seed: int = 20260719) -> str:
    """Seeded synthetic v2 corpus shaped like the real harvests: 2007-2026 span, ~18% automated
    notification threads without a prose reply, en/nl/fr/de, deep multi-reply threads, duplicate
    subject families, a few risk-marked threads."""
    rng = random.Random(seed)
    sections = []

    def message(role: str, when: date, body: str) -> str:
        return f"**{role} ({when.isoformat()}):**\n{body}\n"

    for i in range(1, n + 1):
        if i == 1:
            start = date(2007, 1, 15)
        elif i == 2:
            start = date(2026, 5, 1)
        else:
            year = rng.randint(2007, 2026)
            month = rng.randint(1, 5) if year == 2026 else rng.randint(1, 12)
            start = date(year, month, rng.randint(1, 28))
        kind = rng.random()
        parts = []
        if kind < 0.18:  # automated notification, never prose-answered
            subject = f"Your order confirmation #{rng.randint(1000, 99999)}"
            parts.append(message("external", start,
                                 "This is an automated notification about your order. Do not reply to this message."))
        elif kind < 0.21:  # risk-marked dispute with a prose reply
            subject = f"Chargeback dispute for order {rng.randint(100, 999)}"
            parts.append(message("external", start,
                                 "Hello, my bank opened a chargeback for this order and I want a refund from you now." + DETAIL["en"]))
            parts.append(message("mailbox", start + timedelta(days=1), REPLY["en"]))
        elif kind < 0.25:  # generic subject -> mixed bucket
            subject = rng.choice(("Contact", "Order", "Invoice", "Question", "Info"))
            lang = rng.choice(("en", "nl", "fr", "de"))
            parts.append(message("external", start, QUESTION[lang]))
            parts.append(message("mailbox", start + timedelta(days=1), REPLY[lang]))
        else:  # duplicate subject families, occasionally deep threads
            topic, lang = rng.choice(TOPICS)
            subject = f"{topic} {rng.randint(1, 999)}"
            depth = rng.choice([2] * 8 + [3] * 3 + [8])
            body = QUESTION[lang] + (DETAIL[lang] if rng.random() < 0.4 else "")
            parts.append(message("external", start, body))
            for j in range(1, depth):
                role = "mailbox" if j % 2 else "external"
                text = REPLY[lang] if role == "mailbox" else QUESTION[lang]
                parts.append(message(role, start + timedelta(days=j), text))
        occurrences = rng.choice([1] * 8 + [2, 3])
        sections.append(f"## {subject} — #{i}\n\n**Occurrences:** {occurrences}\n\n" + "\n".join(parts))

    front = ("---\nharvest_format: v2\nharvested_at: 2026-06-01T00:00:00Z\n"
             f"accepted_interactions: {n}\nunique_content: {n}\nterminal_condition: accepted_target\n---\n\n")
    return front + "\n".join(sections)


class LargeCorpusTests(unittest.TestCase):
    scratch: Path

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        corpus = write_corpus(tmp, generate_v2_corpus())
        cls.scratch = tmp / "scratch"
        started = time.monotonic()
        ph.prepare_scratch(corpus, cls.scratch, dict(ph.DEFAULTS), export_id="exp-large",
                           preflight=ph.synthetic_preflight("exp-large"))
        cls.elapsed = time.monotonic() - started

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_runs_in_seconds_with_full_coverage_ledger(self):
        self.assertLess(self.elapsed, 30, "1,000-thread prepare must run in seconds")
        rows = read_manifest(self.scratch)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(ph.verify_scratch(self.scratch), [])
        ledger = read_ledger(self.scratch)
        self.assertEqual(len(ledger["threads"]), 1000)
        statuses = {r["status"] for r in ledger["threads"].values()}
        self.assertLessEqual(statuses, set(ph.PRIMARY_STATUSES))
        self.assertEqual(ledger["corpus"]["date_span"][0][:4], "2007")
        self.assertEqual(ledger["corpus"]["date_span"][1][:4], "2026")
        self.assertEqual(len(list((self.scratch / "threads").iterdir())),
                         1000 - len(ledger["holdout"]["ids"]))

    def test_automated_share_languages_and_families_look_like_real_harvests(self):
        rows = read_manifest(self.scratch)
        automated = [r for r in rows if r["form_source"]]
        self.assertAlmostEqual(len(automated) / 1000, 0.18, delta=0.04)
        self.assertTrue(all(not r["prose_reply"] for r in automated))
        self.assertGreaterEqual({r["language"] for r in rows} & {"en", "nl", "fr", "de"},
                                {"en", "nl", "fr", "de"})
        self.assertGreater(sum(1 for r in rows if r["message_count"] >= 8), 10,
                           "several deep multi-reply threads expected")
        families = [r["subject_family"] for r in rows]
        self.assertGreater(max(map(families.count, set(families))), 50,
                           "duplicate subject families expected")
        self.assertEqual({r["era"] for r in rows}, {"recent", "mid", "old"})

    def test_sampling_capped_risk_deep_read_and_mixed_present(self):
        clusters = {c["id"]: c for c in read_clusters(self.scratch)}
        self.assertIn("mixed", clusters)
        self.assertGreater(clusters["mixed"]["size"], 30)
        rows = {r["id"]: r for r in read_manifest(self.scratch)}
        ledger = read_ledger(self.scratch)
        self.assertFalse(ledger["risk"]["over_cap"])
        self.assertGreater(ledger["risk"]["flagged"], 20)
        self.assertIn("payment_dispute", ledger["risk"]["by_marker"])
        for cluster in clusters.values():
            self.assertLessEqual(len(cluster["sample_ids"]), ph.DEFAULTS["sample_cap"])
            for tid in cluster["deep_read_ids"]:
                self.assertTrue(rows[tid]["risk_markers"] or rows[tid]["ambiguous"])
            if cluster["size"] > ph.DEFAULTS["sample_cap"] * 2:
                eras = {rows[tid]["era"] for tid in cluster["sample_ids"]}
                self.assertGreater(len(eras), 1, f"{cluster['id']} sample must span era bands")

    def test_holdout_default_eight_stratified_and_excluded(self):
        ledger = read_ledger(self.scratch)
        holdout_ids = ledger["holdout"]["ids"]
        self.assertEqual(len(holdout_ids), 8)
        rows = {r["id"]: r for r in read_manifest(self.scratch)}
        eras = {rows[hid]["era"] for hid in holdout_ids}
        self.assertGreater(len(eras), 1, "holdout must be stratified across era bands")
        for hid in holdout_ids:
            self.assertTrue(rows[hid]["prose_reply"])
            self.assertEqual(ledger["threads"][hid]["status"], "holdout")
        plan_ids = set()
        for cluster in read_clusters(self.scratch):
            plan_ids.update(cluster["thread_ids"])
            plan_ids.update(cluster["sample_ids"])
            plan_ids.update(cluster["deep_read_ids"])
        self.assertFalse(plan_ids & set(holdout_ids))


if __name__ == "__main__":
    unittest.main()
