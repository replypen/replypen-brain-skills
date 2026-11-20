# Connecting your own domain to your new website

Send to the tenant (or whoever manages its DNS) once the site is ready, translated to their language.
Replace `<domain>` and `<target>` (KampAdmin: `sites.kampsites.be`; `rp … project site domain add` prints it).

**Your domain stays where it is.** No transfer, no new provider, your email keeps working.

At your current DNS provider (GoDaddy, Combell, one.com, Cloudflare, …), DNS settings:

1. **`www`**: one CNAME record `www` → `<target>`. If a `www` record already exists (e.g. to Webflow or
   Wix), replace it. DNS on Cloudflare? Turn the orange cloud off (*DNS only*).
2. **Without www** (`<domain>`): set up *forwarding* to `https://www.<domain>` (301, permanent). If your
   provider can't, leave it and tell us.
3. **Don't touch**: MX, SPF, DKIM and other TXT records (your email).

Tell us when it's done. Within minutes to an hour `https://www.<domain>` is live with a valid
certificate; your old website is then no longer online.

<details><summary>For whoever builds the site</summary>

- First `rp --project <p> --tenant <t> project site domain add www.<domain>` (prepares the hostname),
  then send this guide, then rerun `add` until `live: true`; then `url:` in `_data/site.yml`.
- The bare domain can't point at us directly: that needs Cloudflare Enterprise (apex proxying).
- Until the certificate is issued (HTTP validation, starts only after the CNAME) `www` can briefly show a
  certificate error: switch outside peak hours.
</details>
