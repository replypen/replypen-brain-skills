# Image generation — cost model and the ladder decisions

`lib.image` (runtime/lib/image.py) owns the preview → final ladder; the ReplyPen host owns the
model choice (`internal/imagegen`, `ModelFor`). Both calibrated against OpenAI GPT Image 2.5,
vetted live 2026-09-08. Re-vet with a one-off curl on `/v1/images/generations`; images are not on
OpenRouter, so the model-picker cannot see them.

## The models (only two in the 2.5 family, no mini)

| | Flare | Sunburst |
|---|---|---|
| price | $30/M output tokens, $8/M image input | identical |
| tokens per tier/size | identical | identical |
| speed | ~2× faster than gpt-image-2 | ~1.5–2× slower than Flare |
| strength | fresh generation | edit fidelity: reproduces the base image, Flare re-imagines it |

Same $/token as gpt-image-2 too (OpenAI's own calculator uses one rate). Articles claiming "2.5 is
2× the price" compared the standard tab with the batch tab.

## What sets the price — the token formula (from OpenAI's calculator)

```
tokens  = ceil( patches × (2,000,000 + w×h) / 4,000,000 )
patches = long-edge grid per tier × round(long / aspect)
tiers   : low 16 · medium 24 · high 48 · xhigh 64 · max 96   (gpt-image-2: 16 / 48 / 96)
```

- **Tier is the lever.** low → medium → high → xhigh → max ≈ 1× · 2.2× · 9× · 16× · 36×.
- **Pixels barely matter.** From the 655,360 px floor to 1024² the factor moves 0.66 → 0.76; 8 MP
  is 2.6×. Below the floor the API returns 400 "minimum pixel budget" — you cannot buy a 512² image.
- **Wide is cheaper.** Fewer short-edge patches: 16:9 low ≈ 99 tokens, 3:1 low ≈ 60, square 196.
- Constraints: edges multiples of 16, aspect ≤ 3:1, ≤ 3840 per edge, ≤ 8.3 MP.

Measured at 1024² (both models): low 196 tok $0.006 · medium 439 $0.013 · high 1756 $0.053 ·
xhigh 3122 $0.094 · max 7024 $0.21. An edit adds ~1024 input tokens for the base image ($0.008).

## Preview side — how we make it cheap

1. **quality low** — the only knob that moves the needle (9× cheaper than high).
2. **smallest pixel budget the aspect allows** — `size_for` picks the smallest %16 short edge
   whose size clears the 655k floor +1% (816², 736×928, 1104×624 …); worth ~13%, free because the
   preview only has to confirm composition, wording and style.
3. **Flare** — same price, fastest; the preview is a decision point, not the deliverable.
4. **Words before pixels** — the skill ideates in text and shows free style examples first; one
   preview, never a stack of guesses (`skills/image/SKILL.md` in replypen).

Result: $0.003–0.006 per preview. There is nothing left to squeeze here.

## Final side — decisions

- **Final = edit of the approved preview**, never a fresh prompt: a new seed drifts from what the
  human approved.
- **Sunburst for every edit** (refine and later tweaks). Same price; A/B on two previews showed it
  keeps composition where Flare adds/moves elements. +10–20 s, and we optimise quality over latency.
- **Tier medium** ($0.013 at 1024², 24×24 patches). Our audience posts social visuals and site
  banners; medium is visibly sharper than the preview and half the cost of the old gpt-image-2
  ladder. High ($0.05) and xhigh/max ($0.09/$0.21) exist for print-grade detail — flip
  `_QUALITY["final"]` if a project demonstrably needs it; no knob until then.
- **Full-size pixels** for the final (short edge 1024, long edge from the ratio): pixels are nearly
  free, so the deliverable gets its real resolution.

## Aspect — any W:H, derived not tabled

- `--aspect` takes any `W:H` from 1:3 to 3:1 (`1:1` default for `generate`); outside ⇒ `ImageError`.
  No preset menu: `size_for(ratio, step)` derives both sizes, edges rounded to 16. Within 1:3..3:1
  the upper provider bounds (3840 edge, 8.3 MP) cannot bind.
- `edit`/`refine` keep the **base image's own ratio** (a 3:4 phone photo stays 3:4; >3:1 bases clamp
  to 3:1). `--aspect` overrides ("make it square" ⇒ `--aspect 1:1`).
- `edit` defaults to `final` (medium): its base is usually an uploaded photo or a delivered final.
  `--step preview` = cheap low-quality trial.
- A size the provider still rejects is retried once by the host at the nearest of
  1024², 1024×1536, 1536×1024.

Whole ladder ≈ $0.02 per delivered image, ~35 s wall time.
