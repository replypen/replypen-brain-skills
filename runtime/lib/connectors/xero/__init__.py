"""Xero Accounting connector — read-only grounding for billing/contact support.

Script, not a declarative row, for two reasons: every accounting call needs a ``Xero-tenant-id``
header (raw ``lib.api get`` cannot send headers), and each list endpoint wraps its rows under its own
key (``Invoices``, ``Contacts``, …). The tenant is resolved from ``GET /connections`` when the
connection covers exactly one organisation; with several, the caller picks one by ``--tenant-id`` or
``--org``.

Read-only: only ever issues GETs. Writes to customer systems are out of scope (action plane).

CLI:
    python -m lib.connectors.xero tenants
    python -m lib.connectors.xero contact --email billing@acme.example
    python -m lib.connectors.xero contact "Acme Corp"
    python -m lib.connectors.xero invoice INV-0042 [--online-url]
    python -m lib.connectors.xero invoices [--status AUTHORISED] [--contact-id <uuid>]
    python -m lib.connectors.xero get CreditNotes --query 'where=Status=="AUTHORISED"'
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lib import api

_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")


def _load_manifest() -> api.Manifest:
    raw = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return api._manifest_from_dict(raw)


MANIFEST = api.register(_load_manifest())

BASE_URL = "https://api.xero.com/api.xro/2.0"
CONNECTIONS_URL = "https://api.xero.com/connections"
PAGE_SIZE = 100

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_XERO_DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def _client() -> api.Client:
    return api.client(MANIFEST, token_key="xero")


def _hinted(e: api.ApiError) -> RuntimeError:
    """Turn a Xero/broker failure into an error the agent can act on, keeping the original detail."""
    if e.status == 401:
        # Xero names a missing scope only in WWW-Authenticate, which ApiError does not keep: one hint
        # covers both causes, and reconnecting fixes both.
        return RuntimeError(
            "Xero rejected the connection (expired, ReplyPen removed in Xero → Settings → Connected apps, "
            "or a read permission missing): a project admin must reconnect Xero in ReplyPen (Data sources). "
            f"({e})"
        )
    if e.status == 403:
        return RuntimeError(
            "Xero refused this organisation for the connection: run `python -m lib.connectors.xero tenants` "
            f"and pass one of the listed ids with --tenant-id. ({e})"
        )
    return RuntimeError(str(e))


def _get(path: str, *, tenant_id: str, query: dict[str, Any] | None = None) -> Any:
    """One GET with the tenant header. A 404 stays an ``ApiError`` (callers decide what "absent"
    means); every other failure becomes a hinted ``RuntimeError``."""
    try:
        return _client().get(path, query=query, headers={"Xero-tenant-id": tenant_id})
    except api.ApiError as e:
        if e.status == 404:
            raise
        raise _hinted(e) from e


def _xero_pages(
    path: str,
    *,
    tenant_id: str,
    query: dict[str, Any] | None = None,
    items_key: str,
    max_pages: int = 1000,
    page_size: int = PAGE_SIZE,
) -> tuple[list[dict], bool]:
    """Collect a Xero list endpoint with 1-based ``page`` numbers → ``(rows, truncated)``.

    Invoices and Contacts honour ``pageSize`` (default 100, max 1000); endpoints that ignore it still
    return 100-row pages, so short-page termination holds as long as ``page_size`` stays 100 there.
    ``items_key`` is per endpoint, so each call carries it as ``items_field``. Unlike ``collect``, any
    page failure raises: a partial invoice list must never read as a complete balance. ``truncated``
    means the page cap stopped the loop with more rows likely on the server.
    """
    manifest = replace(
        MANIFEST,
        pagination=api.Pagination(
            style="page",
            page_param="page",
            page_start=1,
            limit_param="pageSize",
            page_size=page_size,
            items_field=items_key,
        ),
    )
    rows: list[dict] = []
    first = True
    try:
        for page in api.client(manifest, token_key="xero").paginate(
            path, query=dict(query or {}), headers={"Xero-tenant-id": tenant_id}, max_pages=max_pages
        ):
            if first and not (isinstance(page.body, dict) and isinstance(page.body.get(items_key), list)):
                raise RuntimeError(f"`{path}` is not a pageable list (no `{items_key}` array); drop --max-pages")
            first = False
            rows.extend(page.items)
    except api.ApiError as e:
        if e.status == 0 and "max_pages" in str(e):
            return rows, True
        raise _hinted(e) from e
    return rows, False


# ---------------------------------------------------------------------------
# Tenants (organisations)
# ---------------------------------------------------------------------------


def list_tenants() -> list[dict]:
    """Every tenant this connection was granted (``GET /connections``, no tenant header)."""
    try:
        body = _client().get(CONNECTIONS_URL)
    except api.ApiError as e:
        raise _hinted(e) from e
    return body if isinstance(body, list) else []


def _org_line(t: dict) -> str:
    return f"{t.get('tenantName', '?')} (--tenant-id {t.get('tenantId', '?')})"


def resolve_tenant(tenant_id: str | None = None, org: str | None = None) -> str:
    """Return the tenant id to call with.

    An explicit ``tenant_id`` wins without a lookup. Otherwise the connection's ORGANISATION tenants
    are listed: ``org`` picks one by name (exact, then unique substring, case-insensitive); with no
    ``org`` exactly one organisation is required. Every miss raises with the names + ids to pick from.
    """
    if tenant_id:
        return tenant_id.strip()
    orgs = [t for t in list_tenants() if (t.get("tenantType") or "ORGANISATION") == "ORGANISATION"]
    if not orgs:
        raise RuntimeError(
            "This Xero connection covers no organisation. A project admin must reconnect Xero in "
            "ReplyPen (Data sources) and select the organisation on Xero's consent screen."
        )
    listing = "; ".join(_org_line(t) for t in orgs)
    if org:
        needle = org.strip().lower()
        exact = [t for t in orgs if (t.get("tenantName") or "").strip().lower() == needle]
        hits = exact or [t for t in orgs if needle in (t.get("tenantName") or "").lower()]
        if len(hits) == 1:
            return hits[0]["tenantId"]
        what = "matches several" if hits else "matches none"
        raise RuntimeError(f"--org {org!r} {what} of the connected organisations: {listing}")
    if len(orgs) == 1:
        return orgs[0]["tenantId"]
    raise RuntimeError(
        f"This Xero connection covers {len(orgs)} organisations; pass --org <name> or --tenant-id <id>: {listing}"
    )


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def _xero_date(value: Any) -> Any:
    """``/Date(1735689600000+0000)/`` → ``2025-01-01``; anything else unchanged."""
    if not isinstance(value, str):
        return value
    m = _XERO_DATE_RE.match(value)
    if not m:
        return value
    dt = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)
    if dt.hour == dt.minute == dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(obj: dict, fields: tuple[str, ...]) -> dict:
    out = {}
    for f in fields:
        v = obj.get(f)
        if v is None or v == "" or v == []:
            continue
        out[f] = _xero_date(v)
    return out


_INVOICE_FIELDS = (
    "InvoiceID", "InvoiceNumber", "Type", "Status", "Reference", "Date", "DueDate", "FullyPaidOnDate",
    "Total", "AmountDue", "AmountPaid", "AmountCredited", "CurrencyCode", "UpdatedDateUTC",
)
_LIST_INVOICE_FIELDS = (
    "InvoiceID", "InvoiceNumber", "Type", "Status", "Reference", "Total", "AmountDue", "AmountPaid",
    "AmountCredited", "CurrencyCode", "DueDate", "FullyPaidOnDate",
)
_APPLIED_FIELDS = {
    "Payments": ("PaymentID", "Date", "Amount", "Reference", "Status"),
    "CreditNotes": ("CreditNoteID", "CreditNoteNumber", "Date", "AppliedAmount", "Total"),
    "Prepayments": ("PrepaymentID", "Date", "AppliedAmount", "Total"),
    "Overpayments": ("OverpaymentID", "Date", "AppliedAmount", "Total"),
}


def invoice_detail(inv: dict) -> dict:
    """The support view of one invoice: amounts, dates, contact, lines and everything applied to it."""
    out = _compact(inv, _INVOICE_FIELDS)
    contact = inv.get("Contact") or {}
    if contact:
        out["Contact"] = _compact(contact, ("ContactID", "Name", "EmailAddress"))
    lines = [_compact(li, ("Description", "Quantity", "UnitAmount", "LineAmount")) for li in inv.get("LineItems") or []]
    if lines:
        out["LineItems"] = lines
    for key, fields in _APPLIED_FIELDS.items():
        rows = [_compact(r, fields) for r in inv.get(key) or []]
        if rows:
            out[key] = rows
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get(path: str, *, tenant_id: str, query: dict[str, Any] | None = None, max_pages: int = 1) -> Any:
    """GET any accounting path (relative to ``/api.xro/2.0``) with the tenant header.

    ``max_pages > 1`` collects a list endpoint (single segment, e.g. ``CreditNotes``) page by page into
    ``{<key>: rows, "truncated": bool}``; otherwise the raw body is returned.
    """
    path = path.strip()
    if path.startswith(("http://", "https://")):
        if not path.startswith(BASE_URL + "/"):
            raise RuntimeError(f"get takes a path under {BASE_URL}, e.g. `Invoices` or `Contacts/<id>`")
        path = path[len(BASE_URL) + 1:]
    path = path.lstrip("/")
    if max_pages > 1:
        if "/" in path:
            raise RuntimeError("--max-pages needs a list path with one segment, e.g. `CreditNotes`")
        q = dict(query or {})
        size = q.pop("pageSize", None)
        try:
            page_size = int(size) if size is not None else PAGE_SIZE
        except ValueError as e:
            raise RuntimeError(f"pageSize must be a number, got {size!r}") from e
        rows, truncated = _xero_pages(
            path, tenant_id=tenant_id, query=q, items_key=path, max_pages=max_pages, page_size=page_size
        )
        return {path: rows, "truncated": truncated}
    try:
        return _get(path, tenant_id=tenant_id, query=query)
    except api.ApiError as e:  # only a 404 reaches here
        raise RuntimeError(f"Xero has nothing at `{path}`. ({e})") from e


def get_invoice(tenant_id: str, ref: str) -> dict | None:
    """One invoice by InvoiceNumber (``INV-0042``) or InvoiceID, with its Payments/CreditNotes/
    Prepayments/Overpayments. ``None`` when Xero has no such invoice."""
    try:
        body = _get(f"Invoices/{ref.strip()}", tenant_id=tenant_id)
    except api.ApiError as e:
        if e.status == 404:
            return None
        raise
    invoices = (body or {}).get("Invoices") or []
    return invoices[0] if invoices else None


# Xero issues an online invoice only for an approved sales invoice.
_ONLINE_STATUSES = {"AUTHORISED", "PAID"}


def online_invoice_url(tenant_id: str, inv: dict) -> tuple[str | None, str]:
    """``(url, "")`` for the customer-facing online invoice, or ``(None, reason)``."""
    if inv.get("Type") not in (None, "ACCREC"):
        return None, "only sales invoices (ACCREC) have an online invoice"
    if inv.get("Status") not in _ONLINE_STATUSES:
        return None, f"no online invoice for status {inv.get('Status')}: Xero issues one once the invoice is approved"
    # TODO(xero): confirm on the Demo Company that PAID invoices also return a URL (docs name AUTHORISED).
    try:
        body = _client().get(f"Invoices/{inv['InvoiceID']}/OnlineInvoice", headers={"Xero-tenant-id": tenant_id})
    except api.ApiError as e:
        if e.status in (400, 404):  # Xero's "not available for this invoice"; anything else is operational
            return None, f"Xero has no online invoice for it ({e})"
        raise _hinted(e) from e
    rows = (body or {}).get("OnlineInvoices") or []
    url = rows[0].get("OnlineInvoiceUrl") if rows else None
    return (url, "") if url else (None, "Xero returned no online invoice URL")


def list_invoices(
    tenant_id: str,
    *,
    status: str | None = None,
    contact_id: str | None = None,
    sales_only: bool = False,
    max_pages: int = 5,
    page_size: int = PAGE_SIZE,
) -> tuple[list[dict], bool]:
    """Invoices newest-updated first → ``(rows, truncated)``. ``status`` is Xero's ``Statuses`` list
    (``AUTHORISED,PAID``); ``sales_only`` drops bills (ACCPAY) for contacts that are also suppliers."""
    q: dict[str, Any] = {"order": "UpdatedDateUTC DESC"}
    if sales_only:
        q["where"] = 'Type=="ACCREC"'
    if status:
        q["Statuses"] = status.upper()
    if contact_id:
        q["ContactIDs"] = contact_id
    return _xero_pages(
        "Invoices", tenant_id=tenant_id, query=q, items_key="Invoices", max_pages=max_pages, page_size=page_size
    )


def find_contacts_by_email(tenant_id: str, email: str) -> list[dict]:
    """Contacts whose ``EmailAddress`` is ``email``: an exact ``where`` filter first, then Xero's
    ``searchTerm`` (case-insensitive contains over name, email and contact number) narrowed to the same
    address. Contact-person emails are not searched; the caller falls back to the company name."""
    email = email.strip()
    if "@" not in email or any(c in email for c in '"\\'):
        raise RuntimeError(f"--email needs a plain address like name@example.com, got {email!r}")
    body = _get("Contacts", tenant_id=tenant_id, query={"where": f'EmailAddress=="{email}"'})
    hits = (body or {}).get("Contacts") or []
    if hits:
        return hits
    # TODO(xero): confirm on the Demo Company whether the where filter is case-sensitive; searchTerm covers it.
    body = _get("Contacts", tenant_id=tenant_id, query={"searchTerm": email})
    want = email.lower()
    return [c for c in (body or {}).get("Contacts") or [] if (c.get("EmailAddress") or "").strip().lower() == want]


def find_contacts(tenant_id: str, ref: str) -> list[dict]:
    """Contacts by ContactID (exact) or by name/number/email fragment (``searchTerm``)."""
    ref = ref.strip()
    if _UUID_RE.match(ref):
        try:
            body = _get(f"Contacts/{ref}", tenant_id=tenant_id)
        except api.ApiError as e:
            if e.status == 404:
                return []
            raise
        return (body or {}).get("Contacts") or []
    body = _get("Contacts", tenant_id=tenant_id, query={"searchTerm": ref})
    return (body or {}).get("Contacts") or []


def contact_summary(tenant_id: str, *, ref: str | None = None, email: str | None = None, paid: int = 5) -> dict:
    """Contact + open (AUTHORISED) invoices + the latest ``paid`` PAID ones, for "is my invoice paid"."""
    if bool(ref) == bool(email):
        raise RuntimeError("contact needs exactly one of a name/ContactID or --email")
    matches = find_contacts_by_email(tenant_id, email) if email else find_contacts(tenant_id, ref or "")
    if not matches:
        return {"found": False, "ref": email or ref, "by": "email" if email else "name"}
    if len(matches) > 1 and ref:
        exact = [c for c in matches if (c.get("Name") or "").strip().lower() == ref.strip().lower()]
        matches = exact if len(exact) == 1 else matches
    if len(matches) > 1:
        # Never pick one: a draft with another customer's balance is the worst outcome here.
        return {
            "found": False,
            "ambiguous": True,
            "ref": email or ref,
            "candidates": [_compact(c, ("ContactID", "Name", "EmailAddress")) for c in matches[:10]],
        }
    contact = matches[0]
    cid = contact.get("ContactID", "")
    open_invoices, open_truncated = list_invoices(
        tenant_id, status="AUTHORISED", contact_id=cid, sales_only=True, max_pages=3
    )
    paid_invoices = []
    if paid > 0:
        paid_invoices, _ = list_invoices(
            tenant_id, status="PAID", contact_id=cid, sales_only=True, max_pages=1, page_size=paid
        )
    return {
        "found": True,
        "contact": _compact(
            contact, ("ContactID", "Name", "EmailAddress", "ContactNumber", "ContactStatus", "IsCustomer", "IsSupplier")
        ),
        "open_invoices": [_compact(i, _LIST_INVOICE_FIELDS) for i in open_invoices],
        "open_truncated": open_truncated,
        "recent_paid_invoices": [_compact(i, _LIST_INVOICE_FIELDS) for i in paid_invoices[:paid]],
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _money(amount: Any, currency: Any) -> str:
    cur = str(currency or "").strip()
    try:
        return f"{float(amount):.2f} {cur}".strip()
    except (TypeError, ValueError):
        return str(amount)


def summary_to_markdown(s: dict) -> str:
    if s.get("ambiguous"):
        lines = [f"# Xero: several contacts match `{s.get('ref', '')}`",
                 "Pick the sender's contact and re-run `contact <ContactID>`; do not guess:"]
        lines += [f"- {c.get('Name', '?')} `{c.get('ContactID', '?')}`" + (f" ({c['EmailAddress']})" if c.get("EmailAddress") else "")
                  for c in s.get("candidates") or []]
        return "\n".join(lines)
    if not s.get("found"):
        hint = (" Xero only matches the contact's main email; try the company name next "
                "(`contact \"<name>\"`) or the invoice number.") if s.get("by") == "email" else ""
        return f"# Xero contact not found\nNo contact matched `{s.get('ref', '')}`.{hint}"
    c = s["contact"]
    lines = [f"# Xero: {c.get('Name', '?')}", f"- ContactID: `{c.get('ContactID', '?')}`"]
    if c.get("EmailAddress"):
        lines.append(f"- Email: {c['EmailAddress']}")
    if c.get("ContactStatus") and c["ContactStatus"] != "ACTIVE":
        lines.append(f"- Status: {c['ContactStatus']}")

    open_inv = s.get("open_invoices") or []
    totals: dict[str, float] = {}
    for inv in open_inv:
        cur = inv.get("CurrencyCode", "")
        try:
            totals[cur] = totals.get(cur, 0.0) + float(inv.get("AmountDue") or 0)
        except (TypeError, ValueError):
            pass
    head = f"\n## Open invoices ({len(open_inv)})"
    if totals:
        head += ": " + ", ".join(_money(v, k) for k, v in totals.items()) + " due"
    lines.append(head)
    for inv in open_inv:
        num = inv.get("InvoiceNumber") or inv.get("InvoiceID", "?")
        line = f"- `{num}`: {_money(inv.get('AmountDue'), inv.get('CurrencyCode'))} due"
        if inv.get("DueDate"):
            line += f" by {inv['DueDate']}"
        if inv.get("AmountPaid"):
            line += f" ({_money(inv['AmountPaid'], inv.get('CurrencyCode'))} already paid)"
        lines.append(line)
    if not open_inv:
        lines.append("- (none)")
    if s.get("open_truncated"):
        lines.append("- (more open invoices exist than shown: the total above is a lower bound; use `invoices --contact-id`)")

    paid = s.get("recent_paid_invoices") or []
    if paid:
        lines.append(f"\n## Recently paid ({len(paid)})")
        for inv in paid:
            num = inv.get("InvoiceNumber") or inv.get("InvoiceID", "?")
            cur = inv.get("CurrencyCode")
            when = f" on {inv['FullyPaidOnDate']}" if inv.get("FullyPaidOnDate") else ""
            how = f"{_money(inv.get('AmountPaid') or 0, cur)} paid"
            if inv.get("AmountCredited"):
                how += f" + {_money(inv['AmountCredited'], cur)} credited"
            lines.append(f"- `{num}`: {_money(inv.get('Total'), cur)} settled{when} ({how})")
    lines.append("\nDetail incl. payments and credit notes: `python -m lib.connectors.xero invoice <number>`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_query(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep or not k:
            raise RuntimeError(f"--query takes K=V, got {p!r}")
        out[k] = v
    return out


def _pick_rows(body: Any, path: str, fields: str) -> Any:
    key = path.strip().lstrip("/").split("/")[0]
    if isinstance(body, list):
        return [api.pick(r, fields) for r in body]
    if isinstance(body, dict) and isinstance(body.get(key), list):
        return {**body, key: [api.pick(r, fields) for r in body[key]]}
    return api.pick(body, fields)


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lib.connectors.xero")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def org_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tenant-id", help="organisation id, only when the connection covers several (see `tenants`)")
        p.add_argument("--org", help="organisation name, instead of --tenant-id")

    sub.add_parser("tenants", help="list the connected Xero organisations")

    inv = sub.add_parser("invoice", help="one invoice by number or InvoiceID, with payments and credit notes")
    inv.add_argument("ref", help="InvoiceNumber (INV-0042) or InvoiceID")
    inv.add_argument("--online-url", action="store_true", help="add the customer-facing online invoice link")
    org_flags(inv)

    invs = sub.add_parser("invoices", help="list invoices, newest updated first")
    invs.add_argument("--status", help="AUTHORISED (approved, unpaid), PAID, DRAFT, VOIDED; comma-separated")
    invs.add_argument("--contact-id", help="only this ContactID")
    invs.add_argument("--max-pages", type=int, default=2, help="page cap (100/page)")
    org_flags(invs)

    cont = sub.add_parser("contact", help="contact + open and recently paid invoices")
    cont.add_argument("ref", nargs="?", help="contact name fragment or ContactID")
    cont.add_argument("--email", help="the sender's email address")
    cont.add_argument("--paid", type=int, default=5, help="how many recently paid invoices to show")
    org_flags(cont)

    g = sub.add_parser("get", help="GET any accounting path with the tenant header (raw JSON)")
    g.add_argument("path", help="path under /api.xro/2.0, e.g. CreditNotes, Payments, Contacts/<id>, Organisation")
    g.add_argument("--query", action="append", default=[], metavar="K=V")
    g.add_argument("--max-pages", type=int, default=1, help=">1 collects a list path page by page")
    g.add_argument("--pick", help="comma-separated dotted fields kept per row (lib.api pick syntax)")
    org_flags(g)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "tenants":
            tenants = list_tenants()
            if not tenants:
                print("No connected Xero organisations. A project admin must reconnect Xero in ReplyPen (Data sources).")
                return 0
            print("# Xero connected organisations\n")
            for t in tenants:
                print(f"- **{t.get('tenantName', '?')}**: `{t.get('tenantId', '?')}` ({t.get('tenantType', '?')})")
            return 0

        tenant_id = resolve_tenant(args.tenant_id, args.org)

        if args.cmd == "invoice":
            inv = get_invoice(tenant_id, args.ref)
            if inv is None:
                raise RuntimeError(
                    f"No Xero invoice `{args.ref}` in this organisation. Check the number with "
                    "`contact --email <sender>` or `invoices --contact-id <id>`."
                )
            out = invoice_detail(inv)
            if args.online_url:
                url, why = online_invoice_url(tenant_id, inv)
                out["OnlineInvoiceUrl" if url else "OnlineInvoiceUrlUnavailable"] = url or why
            _emit(out)
            return 0

        if args.cmd == "invoices":
            rows, truncated = list_invoices(
                tenant_id, status=args.status, contact_id=args.contact_id, max_pages=args.max_pages
            )
            out = []
            for r in rows:
                item = _compact(r, _LIST_INVOICE_FIELDS)
                if (r.get("Contact") or {}).get("Name"):
                    item["Contact"] = r["Contact"]["Name"]
                out.append(item)
            _emit({"invoices": out, "truncated": truncated})
            return 0

        if args.cmd == "contact":
            print(summary_to_markdown(contact_summary(tenant_id, ref=args.ref, email=args.email, paid=args.paid)))
            return 0

        if args.cmd == "get":
            body = get(args.path, tenant_id=tenant_id, query=_parse_query(args.query), max_pages=args.max_pages)
            _emit(_pick_rows(body, args.path, args.pick) if args.pick else body)
            return 0
    except (api.ApiError, api.MethodPolicyError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
