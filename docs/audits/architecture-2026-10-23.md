# Architecture audit — replypen-brain-skills (kit) — 2026-10-23
Window: 123 commits since 10-16. Previous: [`architecture-2026-10-16.md`](architecture-2026-10-16.md). Kit lint run on all 83 local brains.
Method: Opus auditor + independent Astra → lead re-verified → Opus builder → Sol High review.

## Changes (main; release = `refresh-brains.sh --release patch` + host pin → pending PJ go)
| Fix | Bug it removes |
|---|---|
| Unconstructible YAML dates read as strings / file-scoped WARN | one impossible date crashed the whole lint without naming the file |
| `lib.conversation`: NBSP/U+202F group thousands; a plain space only before decimals (`€ 1 234,50`); suffix currency keeps "75 EUR 14 dagen" = €75; "2026 €50" = €50 | `€1 250` read as €1 → KampAdmin cancel preflight suggested a €250 refund on €1 250 |
| simulate: failed decline lookup = check "unavailable" | regressions passed green |
| One doc-size function (`brain_doc.on_demand_doc_bytes`) for lint + hygiene (rendered bytes) | pre-push passed, publish FAILed |
| `kit_fresh` re-runs via `uv run --script` on dependency change | ModuleNotFoundError instead of fallback |
| `triage_source` uses the shared `include_in` reader | silent duplicate-triage notice for host-read forms |
| Conversation schema ≠ v1 → unavailable | host change read as "staff said nothing" |
| Mermaid cold-start test behind `RP_TEST_MERMAID=1`; `sibling_modules` cache; `brain_test.py <file>` collects that file | DA offline tier 4.3 → 2.2 s; single file 5.1 → 0.3 s |
| Near-limit doc WARN only for named/changed files | 146 fleet-wide WARNs nobody acts on (DA 25→1, KA 22→0, PB 25→6) |

## Release impact (why PJ decides)
Preflights read "1 250 €" correctly (KampAdmin `cancel_subscription` verdicts change); hygiene may now block pushes that grow a rendered doc past 6000 B.

## Not done
`lib.mirror_skip` hides a policy.py syntax error as "no tests ran" (kit bug, follow-up); website-kickstart move out of the kit (open since 10-16).
