# Old vs new: comparing a probabilistic change without fooling yourself

Every brain, prompt, persona or mirror change is judged by replaying real runs on both versions. Two
replays of the *same* version differ, and an LLM judge has its own noise and biases, so a verdict needs a
design, not a tally. Tool: [`brain-simulate/scripts/ab.py`](../skills/brain-simulate/scripts/ab.py)
(replay N reps per arm → collect → gates → blind swapped packs → verdict). Judge prompt:
[`ab-judge-prompt.md`](../skills/brain-simulate/templates/ab-judge-prompt.md).

> [!IMPORTANT]
> Measured on kampadmin-support chat, 9 Nov 2026: 10 turns × 2 identical arms × 2 reps (40 replays), judged
> twice. Last week's one-order brief **picked a winner in 35-50% of identical pairs**, favouring position Y
> 4-6:1. The swapped two-order template with two model families: **0 clear wins in 160 judgments, 0/10 cases
> won**, swap agreement 95-100%, and it still **caught 15/16 planted defects per judge** (leaks 100%).
> Deterministic gates (outcome, actions, SQL errors) were identical across arms. The earlier "regression"
> (old 4 · new 1 clear) was inside that noise.

## Principles

1. **Validate the measurement first.** The judge must see everything the user saw: full answer (never
   truncated), action cards with their preflight preview, the conversation context. A missing piece reads as
   a bad answer. Report every case that dropped out and why; never let incomplete pairs vanish.
2. **Gates before judges.** Deterministic signals (outcome, proposed actions, real SQL errors, links,
   bootstrap size, bash calls) are cheap and stable. A gate difference counts only when it exceeds what the
   same arm does twice (`ab.py metrics`: within-arm vs across-arm). Deterministic code changes get golden
   outputs / exact diffs, not a judge.
3. **The case is the unit.** Three noises, three remedies: which cases you picked (more cases), run-to-run
   variance (reps), judge error (both orders, second family). Reps and votes are not new customers: average per
   case, then test across cases (paired bootstrap CI, sign test). Pair reps at random; old#1 vs new#1 means nothing.
4. **One arm = one configuration.** Fingerprint each run (resolved brain/tenant commit); interleave arms in
   time. `metrics` warns when an arm spans two commits (main moved mid-experiment).
5. **Blind, both orders, two families.** Every pair is judged as X/Y and again as Y/X in a separate fresh
   session; disagreement cancels out instead of becoming a verdict. A second judge from another model family
   catches self-preference ([Zheng 2023](https://arxiv.org/abs/2306.05685), [Shi 2024](https://arxiv.org/abs/2406.07791),
   [Verga 2024](https://arxiv.org/abs/2404.18796)).
6. **"Same" is a valid answer and the default.** Judge by criterion in priority order (correct → complete →
   right action → no leaks → terms/tone → concise); wording, order and length are not a difference; a fault
   both arms share is not a preference.
7. **Calibrate the judge both ways.** An A-vs-A null (same config twice) must tie; planted known-worse copies
   (`pack --plants`, truncated answer / internals leak) must be caught (≥ 80%). Without both, "same" may just
   mean "lenient" and a "win" may just mean "position".
8. **Decide the rule before looking; extend uniformly.** Margin and N are fixed upfront. Inconclusive → one
   more rep per arm on *every* case. Re-running only the cases that looked bad is diagnosis, never evidence
   (it shrinks real effects toward zero; error bars per [Miller 2024](https://arxiv.org/abs/2411.00640)).
9. **Don't tune on the test set.** The cases you debugged a fix on prove the fix, not the change: verdict
   on a fresh stratified draw (or rotate half the set each round). Replay restores the input, not history:
   live data moved since, and a human's sent email is evidence, not an oracle.
10. **Humans spot-check the extremes.** Read every planted miss, every clear loss and 2-3 random "same"
   pairs before you trust a verdict. Criteria drift as you read outputs; update the judge prompt, not the
   verdict ([Shankar 2024](https://arxiv.org/abs/2404.12272)).

## Default protocol (brain / prompt trims)

| Step | Default |
|---|---|
| Cases | 20 real turns, stratified (e.g. SQL / how-to / records / no-tool), recent enough to replay |
| Reps | 1 per arm; extend with `--reps 2` (all cases) when inconclusive |
| Gates | `metrics`: no new SQL errors, no signature flips beyond within-arm noise, size/cost delta as intended |
| Judge | `pack --plants 4`: pack 1 and pack 2 each to a fresh session, 2 families (4 sessions) |
| Rule | `verdict --margin 0.15`: CI lower > 0 → better · CI ≥ −0.15 → not worse (trims may ship) · CI below −0.15 → worse · else inconclusive → extend once, then decide on what you have |
| Null | once per case set / judge prompt change: `--arm a1= --arm a2=`, store its `cmp-a1-a2` and pass `--null` |

Mean preference is per case in [−1, +1] (+1 = every judgment prefers the candidate). A margin of 0.15 means
"the old version may win at most ~1 in 7 judgments net". `verdict` warns when the margin sits inside the
null's CI, when a judge's swap agreement is < 70%, or when it caught < 80% of plants.

<details><summary>Why these numbers, what each knob costs</summary>

- Chat replay ≈ 2-3 min wall clock; 4 in flight (`--parallel`). 20 cases × 2 arms = 40 replays ≈ 25 min, one laptop process.
- Judges are separate fresh sessions, one pack each: ~20-30 pairs per session works.
- Null (kampadmin-support, 9 Nov 2026): within-arm action/outcome signatures stable 20/20; judge decisive
  2-18%, clear 0-1%, swap agreement 95-100%; null CI ≈ ±0.1 at 10 cases. Plants: a truncation that kept
  the key number is the one judges call "same"; that is fair.
- Last week's replays re-judged within one arm (old vs old, new vs new, 10 pairs): 2 clear in 80 judgments,
  3/10 cases leaning one way. A single case "win" is noise; decide on the CI over cases. So run variance at the judged level is
  small for this brain: spend on **more cases** before more reps or a third judge.
- More reps per case help when single cases are unstable (within-arm signature flips); more cases help when
  the question is "does this generalise"; a third judge only helps if two disagree often.
</details>

## Where things live

| Piece | Home |
|---|---|
| Replay, collect, gates, packs, plants, verdict | kit `ab.py`: batch `rp ask --replay-run a,b --no-wait`, `rp run wait --once` (≤ `--parallel` in flight, so live mail keeps workers), `rp run trace` |
| Cost + tokens per replay | `ab.py collect` → `rp admin project run-usage` (human global admin only: unit economics never reach a customer surface); `metrics` prints median + total per arm |
| Judge criteria, product context, strata | the brain's own notes (`_internal/`), pasted into the judge template |
| Fixture cases with deterministic `expect` checks | brain `_internal/simulate/` ([brain-simulate](../skills/brain-simulate/SKILL.md#fixture-cases)) |
| Cases that outlive retention | **none, by decision (PJ, Nov 2026)**: pinning would keep patient-name dictionaries past the 14-day vault ceiling we promise. Draw fresh cases each round (masked email replays ≤ 14 d, others ≤ 28 d); a case worth keeping forever becomes a PII-free fixture case in the brain |
