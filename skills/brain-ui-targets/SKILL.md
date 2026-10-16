---
name: brain-ui-targets
description: "Inventory the DOM elements of a project's own web app that the embedded chat widget should be able to point users at ('click here' highlight + scroll, never click): mine the help center's annotated screenshots, map the app's render layer, crawl the real DOM, pick stable selectors, add minimal data-rc-guide hooks. Use when preparing a project for in-app UI guidance or re-verifying targets after a UI/framework upgrade."
---

# brain-ui-targets - which DOM elements matter, and how to find them stably

Intent: non-technical users ask the widget "where do I click?". The widget can only help if the brain knows
the app's **targets** (stable id → element) and the **flows** that chain them. This playbook produces that
inventory. It needs the host app's source checkout and a local dev instance; it never needs production.

Output: a provisional inventory in the brain's operator-only `_internal/ui-guidance/` (excluded from runs)
until the project's guidance format is live; the brain then documents **ids and workflows**, never CSS.
Worked example (KampAdmin, Avo/Rails): `rootcause-brain-kampadmin-support/_internal/ui-guidance/`.

## 1. Help center first: what users already needed pointing to

- Pull the `/kb` snapshot to the laptop: `rc dev console download /kb/<provider> ./kb` (dirs are tarred;
  see `rc dev console --help`). Never page through `bash run` output: it truncates and says so.
- Screenshot alt texts are AI descriptions ([knowledge-base.md](../../docs/knowledge-base.md)). Grep them for
  annotations, the strongest signal: `rg -i "arrow|pijl|red box|omkaderd|highlight|circled|markering" kb/`.
  Rank controls by annotation count; extract recurring multi-step flows with exact labels.
- Fan out one cheap sub-agent per KB folder; merge into: control · intents in the user's words · path ·
  prerequisites · article url · annotation quote (≤20 words). No customer names/emails.
- KB prose drifts (renamed menus, removed actions, old screenshots). Trust code and the live DOM for labels;
  keep the drift list: it is help-center feedback (`brain-helpcenter-suggestions`).

## 2. Source map: where controls render, which hooks already exist

Find the app's render layer (admin framework components, its overrides, the menu config, action/registry
lists). Per surface (global chrome, list/index, record/detail, tabs, edit form, modals, dashboards, custom
screens) record: existing stable attribute, generating file:line, uniqueness, role predicate. Most
frameworks already emit machine keys (field id, column id, action class, resource name): reuse them.

## 3. Crawl the real DOM (local dev only)

Log in to the **local** app with dev seed credentials (never on production). Write a small Playwright
crawler, committed in the host repo, that visits **states**, not just pages: index, filter panel open,
each dropdown open, each modal, record per tab, edit form. Per state dump every visible control with
candidate selectors and their **visible** match counts. It doubles as the regression check after
upgrades (exit non-zero on HTTP errors). Pick one row/record via the DOM, not hard-coded ids.

## 4. Selector rules

| Prefer | Avoid |
|---|---|
| `[data-rc-guide="<surface>.<key>"]` | nth-child paths, utility classes |
| framework machine attrs (`data-field-id`, column/filter ids, action class in href) | auto-generated ids (`autocomplete-0`, numbered card ids), ids repeated per widget |
| href params matched exactly: suffix `$=` or `…=<key>&` | bare `href*=` prefixes (`WaitingList` also hits `WaitingListToBeConfirmed`) |
| keys from code/config | visible labels: translated, tenant-editable |

- **Duplicates:** responsive copies (hidden mobile menu), top+bottom form buttons → resolve to the first
  *visible* match; per-row controls → scope by the row's record id.
- **State:** collapsed menu sections, lazy tabs (other tabs' fields absent from the DOM), hidden dropdown
  items, modals loaded into a frame, actions disabled until rows are selected. Record the prerequisite
  click per target; the host must report present / visible / disabled.
- **Variance:** role-gated and record-state-gated controls; tenant custom fields and renamed tabs (ground
  the key → label via the brain's column lookup). Disabled placeholders with a reason tooltip are guidance
  gold ("why can't I delete?").
- **SPA/Turbo:** re-resolve after client navigation; offset scrolling under fixed headers. The widget is a
  cross-origin iframe: a host-side resolver of whitelisted ids does the highlighting, no arbitrary JS.
- Mark **regions** (rows, badges, errors, toasts) separately from click targets.

## 5. Stabilise minimally in the host app

Only where step 3 found no stable hook: emit `data-rc-guide="<surface>.<key>"` (`nav.<resource>`,
`index.<control>`, `record.<control>`, `form.save`, `action.<class key>`, `header.<button>`). Machine keys,
never labels. One systematic override of the framework's control/menu renderer beats per-template edits;
it must fail safe (a missing hook never breaks a page), carry a spec, and get an opposite-model review.
Re-crawl to prove uniqueness, then ship through the host's normal deploy.

## 6. Inventory checklist

Per target type: id · selector · visible count (1 / 2* / per row) · present on load or after which click ·
who sees it · KB articles (+ annotated?) · source file · region or click. Plus: top flows mapped to ids,
coverage (states crawled, roles not verified), gaps, and how to re-run the crawler.
