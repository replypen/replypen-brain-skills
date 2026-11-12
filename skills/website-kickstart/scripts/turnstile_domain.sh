#!/usr/bin/env bash
# Add a site's custom domain to ReplyPen's fleet Turnstile widget (the contact-form bot check), so the
# widget renders on it. Needed once per custom domain at go-live; every *.rootcause-sites.workers.dev
# host is already covered. Uses wrangler's OAuth login (scope challenge-widgets.write; `wrangler login`).
#   turnstile_domain.sh                 # list the widget's hostnames
#   turnstile_domain.sh dekies.be       # add an apex (covers www. and every other subdomain)
# A customer's own widget (self-service, its own Cloudflare account; your wrangler login must be a member):
#   TURNSTILE_ACCOUNT=<account id> TURNSTILE_SITEKEY=<org widget sitekey> turnstile_domain.sh kamp-x.be
set -euo pipefail
ACCOUNT=${TURNSTILE_ACCOUNT:-60c5feed78b351b48868d3a691184c4b} # default: Cloudflare "Rootcause Sites"
SITEKEY=${TURNSTILE_SITEKEY:-0x4AAAAAAFlC2YxmVg2JbUFF}         # default: fleet "ReplyPen website forms" (site-template _data/site.yml)
WR=(env CLOUDFLARE_ACCOUNT_ID="$ACCOUNT" pnpm dlx wrangler@4 turnstile widget)

# The widget JSON carries the secret: keep it in this pipe, print only hostnames.
current=$("${WR[@]}" get "$SITEKEY" --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print("\n".join(d.get("result",d)["domains"]))')
if [ $# -eq 0 ]; then echo "$current"; exit 0; fi

domain=$(echo "$1" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##; s#^www\.##')
if echo "$current" | grep -qxF "$domain"; then echo "already listed: $domain"; exit 0; fi
n=$(echo "$current" | grep -c .)
if [ "$n" -ge 10 ]; then echo "widget is full (10 hostnames on the free plan): see website-forms.md in the replypen repo" >&2; exit 1; fi
args=(); while read -r d; do args+=(--domain "$d"); done <<<"$current"
"${WR[@]}" update "$SITEKEY" "${args[@]}" --domain "$domain" --json >/dev/null
"${WR[@]}" get "$SITEKEY" --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print("\n".join(d.get("result",d)["domains"]))'
