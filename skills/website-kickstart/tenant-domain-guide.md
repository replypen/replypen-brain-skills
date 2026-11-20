# Je eigen domein koppelen aan je nieuwe website

Te sturen naar de klant (of wie zijn DNS beheert) zodra de site klaar is. Vervang `<domein>` en `<doel>`
(KampAdmin: `sites.kampsites.be`; de juiste waarde staat in de uitvoer van `rp … project site domain add`).

**Je domein blijft waar het is.** Geen verhuis, geen nieuwe beheerder, je e-mail blijft werken.

Bij je huidige domeinbeheerder (GoDaddy, Combell, one.com, Cloudflare, …), DNS-instellingen:

1. **`www`**: zet één CNAME-record `www` → `<doel>`. Staat er al een `www`-record (bv. naar Webflow of
   Wix), vervang dat. Zit je DNS bij Cloudflare: zet de oranje wolk uit (*DNS only*).
2. **Zonder www** (`<domein>`): stel *doorsturen / forwarding* in naar `https://www.<domein>` (301,
   permanent). Kan je beheerder dat niet, laat het dan staan en zeg het ons.
3. **Niet aanraken**: MX-, SPF-, DKIM- en andere TXT-records (je e-mail).

Laat ons weten wanneer het gedaan is. Binnen enkele minuten tot een uur is `https://www.<domein>` live met
een geldig certificaat; je oude website staat dan niet meer online.

<details><summary>Voor wie de site bouwt</summary>

- Eerst `rp --project <p> --tenant <t> project site domain add www.<domein>` (maakt de hostname klaar),
  dan deze gids sturen, dan `add` herhalen tot `live: true`; daarna `url:` in `_data/site.yml`.
- De kale naam rechtstreeks op ons zetten kan niet: dat vraagt Cloudflare Enterprise (apex proxying).
- Tot het certificaat er is (HTTP-validatie, start pas na de CNAME) kan `www` kort een certificaatfout
  geven: wissel buiten piekuren.
</details>
