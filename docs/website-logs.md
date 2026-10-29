# Website logs (`lib.site_logs`) — a run debugs its own site's broken links

**Intent:** "which old links break on our site?" is answerable inside a ReplyPen run (chat or power embed that may
edit the website), without a Cloudflare credential in the container and without any way to read another
customer's site. Typical loop: `python -m lib.site_logs` → confirm a path existed (old sitemap, git history) →
`website_change` adding a `_redirects` line → the user publishes.

> [!IMPORTANT]
> Cloudflare log permissions are account-wide and one account hosts many customers' sites, so the token lives
> only on the host (`CF_SITE_LOGS_TOKENS`, JSON `{"<account id>": "<token>"}`, permission Workers Observability).
> The run cannot pass a site, account, Worker or query: the host picks the binding of the run's own scope.

| Piece | Where |
|---|---|
| Host endpoint `rc-broker.internal/site_logs/misses?days&limit&include_noise` (fixed query, aggregates, noise filter, untrusted-path sanitizing, 10 calls/run, 5-min cache, audited) | replypen `internal/sitelogs/` |
| Which site a scope may read: operator-owned `Bindings` keyed by the immutable project/tenant **ids** (a renamed project frees its name, never its site), plus Cloudflare account + logging Worker. A tenant run never inherits the project's site | replypen `internal/sitelogs/sitelogs.go` |
| Mounted only when `Caps.SiteLogs` = website editing AND a trusted caller (member or power embed); a consumer embed never gets it, whatever its knobs say | replypen `internal/run/capabilities.go`, `assembler.go` |
| Client + CLI | `runtime/lib/site_logs.py` |
| Model pointer | replypen `skills/website/SKILL.md` router |

**Logs must exist first.** A static-assets Worker runs no code and logs nothing. Today only sites with a Worker in
front that writes `console.log(JSON.stringify({miss: pathname}))` on a 404 (Workers Logs on) have data:
momentumtools.io (`momentumtools/site-momentumtools` `ops/b4b-proxy/`). Fleet-wide logging (a tiny Worker on the
site that logs misses, likely into Analytics Engine for 3-month retention) is the next step, not built.

**Add a site:** make its Worker log misses, add a `Binding` (reviewed commit), add a token for its account to
`CF_SITE_LOGS_TOKENS` on the host, promote, then ask in that project's chat "which links on our site are broken?".
