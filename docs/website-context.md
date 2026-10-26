# Website context: where "how this site is built" lives

A project's public website is a read-only mirror (`role=website`) the chat agent edits through a preview
card. Three kinds of knowledge, three owners:

| Knowledge | Lives in | Why |
|---|---|---|
| Repo structure: which file holds what, what is generated, recipes ("new help article = `help-center/article/<slug>.html` + these front-matter keys") | the **site repo's root `AGENTS.md`** | changes with the code, same owner, also read by Claude Code/Codex when a developer works on the site |
| Product facts, support reasoning, when to propose a site change | the **project brain** (`skills/<topic>/SKILL.md`), pointing at the site, never copying its map | durable knowledge, one copy |
| Diff hand-over, content-only allowlist, preview/publish, plain language | the platform skill `/skills/website` + host enforcement | same for every site |
| Laptop coding-agent loop (build, `wrangler dev`, look at 390/1280 px, `check_page.py`, branch preview, `main` = live) | `website-dev` in public `replypen/replypen-skills`, vendored into each site's `.agents/skills/` (template carries it) | same for every site; never pasted into chat |

> [!IMPORTANT]
> The host **pastes the site's root `AGENTS.md`** into the run's bootstrap whenever website editing is
> on (dashboard/setup chat, power embed; never email/MCP). No frontmatter tag needed. Before
> 2026-10-22 it wasn't, and prod runs never opened it. Want it in email runs too? Tag it
> `include_in: [agent]` (every run then pays for it; rarely worth it: email answers from `/kb`).

## Writing the site's AGENTS.md

- Lead with a "where things live" table and the recipe for the recurring edit (help article, blog post,
  opening hours). Say what is **generated** and must never be hand-edited.
- State the front-matter keys the generator actually reads; mark legacy keys (e.g. a WordPress `wp_id`)
  as "leave out on new files". Don't invent attributes "for the AI": the agent copies an existing file.
- Say the ReplyPen agent cannot build (no Node in its workspace); the branch preview is its check.
- Open with the `main` = live callout (push deploys; branch → preview → owner OK → merge): a coding agent's global
  "push to main" habit otherwise wins (trial 2026-10-26: Codex pushed a content edit to `main`).
- Keep it under ~16 KB (the paste cap); it is repo documentation, not instructions that override the host.
- Give the mirror a one-line `description` (`rp project repo set site description=…`): email runs see only
  that line in the mirror roster.

Reference: `momentumtools/site-momentumtools` `AGENTS.md`; template `replypen/site-template`.
Host side: replypen `.agents/skills/features/website-editing.md`.
