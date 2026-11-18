"""QuickBooks Online support connector — reads Customers, Invoices, Payments, CompanyInfo.

Force-code triggers that required a script (not just manifest.yaml):
(a) Field pre-selection: Customer/Invoice objects carry 50+ fields; support needs 5–8.
(d) Non-standard pagination: QB embeds ``STARTPOSITION N MAXRESULTS M`` *inside* the SQL string,
    which lib.api's offset style can't inject.
(e) QB query DSL: pre-built queries prevent footguns (unquoted values, wrong field names).

One OAuth connection = one QuickBooks company. The host captures the company id (``realmId``) at
connect time and resolves the API base (``https://{api_host}/v3/company/{realm_id}``, sandbox or
production host) on the broker route, so in a run every call is a path relative to that company.
Local smoke without the broker: set ``RC_CONN_QUICKBOOKS`` (access token) and
``RC_CONN_QUICKBOOKS_REALM_ID`` (+ ``RC_CONN_QUICKBOOKS_API_HOST=sandbox-quickbooks.api.intuit.com`` for
a sandbox company).

CLI:
    python -m lib.connectors.quickbooks customer "Acme Corp"
    python -m lib.connectors.quickbooks customer --email acme@example.com
    python -m lib.connectors.quickbooks invoices --customer-id 42 [--open]
    python -m lib.connectors.quickbooks invoice --number 1037
    python -m lib.connectors.quickbooks invoice 1001
    python -m lib.connectors.quickbooks payments --customer-id 42
    python -m lib.connectors.quickbooks company
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

from lib import api

# Below 75 Intuit serves 75 anyway (since 2025-08-01); pin it so responses are reproducible.
_MINOR_VERSION = "75"
# Upper bound on the local email scan when the server-side filter finds nothing (customers per company).
_EMAIL_SCAN_MAX = 5000
_PROD_HOST = "quickbooks.api.intuit.com"
_ALLOWED_HOSTS = (_PROD_HOST, "sandbox-quickbooks.api.intuit.com")
# Templated on purpose: the broker resolves it from the connection, so requests go out as relative paths.
_TEMPLATE_BASE = "https://{api_host}/v3/company/{realm_id}"
# Intuit documents column projection in SELECT as unsupported: always SELECT *, pick fields when rendering.

def _manifest(base_url: str) -> api.Manifest:
    return api.Manifest(
        key="quickbooks",
        base_url=base_url,
        auth=api.Auth(strategy="bearer"),
        pagination=api.Pagination(style="none"),  # connector drives SQL-embedded pagination
        rate_limit_remaining_header="",
        default_headers={"Accept": "application/json"},
    )


api.register(_manifest(_TEMPLATE_BASE))


def _local_base() -> str | None:
    """Concrete base for a local smoke run (env token, no broker); None when no realm is set."""
    realm = os.environ.get("RC_CONN_QUICKBOOKS_REALM_ID", "").strip()
    if not realm:
        return None
    if not realm.isdigit():
        raise RuntimeError("RC_CONN_QUICKBOOKS_REALM_ID must be the numeric QuickBooks company id")
    host = os.environ.get("RC_CONN_QUICKBOOKS_API_HOST", "").strip() or _PROD_HOST
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(f"RC_CONN_QUICKBOOKS_API_HOST must be one of {', '.join(_ALLOWED_HOSTS)}")
    return f"https://{host}/v3/company/{realm}"


def _client() -> api.Client:
    templated = _manifest(_TEMPLATE_BASE)
    # Broker mode wins: stale local smoke variables must never turn run calls into absolute targets.
    if api._manifest_with_runtime_broker_flag(templated, token_key="quickbooks").brokered:
        return api.client(templated, token_key="quickbooks")
    local = _local_base()
    if local is None:
        raise RuntimeError(
            "QuickBooks is not connected in this run. Connect it under Settings → Integrations "
            "(one connection per QuickBooks company)."
        )
    return api.client(_manifest(local), token_key="quickbooks")


def _get(path: str, query: dict[str, str] | None = None) -> Any:
    q = {"minorversion": _MINOR_VERSION, **(query or {})}
    try:
        return _client().get(path, query=q)
    except api.ApiError as exc:
        raise _friendly(exc) from exc


def _friendly(exc: api.ApiError) -> Exception:
    if exc.status in (401, 403):
        return RuntimeError(
            f"QuickBooks refused the request ({exc.status}): the connection expired or lost access. "
            "Ask the project admin to reconnect QuickBooks under Settings → Integrations."
        )
    if exc.status == 429:
        return RuntimeError(
            "QuickBooks rate limit hit (429, max 500 requests/min per company). Wait a minute, then retry "
            "with fewer calls."
        )
    return exc


def _q(value: str) -> str:
    """Quote a QB SQL string literal (backslash-escape backslashes and single quotes)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _id(value: str, what: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise RuntimeError(f"invalid QuickBooks {what}: {value!r} (expected the numeric Id)")
    return value


def _qb_query(sql: str, entity: str, *, page_size: int = 100, max_records: int = 500) -> list[dict]:
    """Run a QB SQL query, paging with SQL-embedded STARTPOSITION/MAXRESULTS (1-based)."""
    records: list[dict] = []
    start = 1
    while len(records) < max_records:
        body = _get("query", {"query": f"{sql} STARTPOSITION {start} MAXRESULTS {page_size}"})
        page = list((body.get("QueryResponse") or {}).get(entity) or [])
        records.extend(page)
        if len(page) < page_size:
            break
        start += len(page)
    return records[:max_records]


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------


def query_customer(
    display_name: str | None = None, *, email: str | None = None, customer_id: str | None = None
) -> list[dict]:
    """Customers by display name (LIKE), primary email, or QB customer Id."""
    if not display_name and not email and not customer_id:
        raise RuntimeError("at least one of display_name, email, or customer_id is required")
    if customer_id:
        cust = _get(f"customer/{_id(customer_id, 'customer id')}").get("Customer")
        return [cust] if cust else []
    if email and not display_name:
        return _customers_by_email(email.strip())
    clauses = [f"DisplayName LIKE {_q('%' + display_name + '%')}"]
    found = _qb_query(f"SELECT * FROM Customer WHERE {' AND '.join(clauses)}", "Customer", max_records=50)
    if email:
        found = [c for c in found if _email_of(c) == email.strip().lower()]
    return found


def _email_of(customer: dict) -> str:
    return str(_val(customer, "PrimaryEmailAddr.Address") or "").strip().lower()


def _customers_by_email(email: str) -> list[dict]:
    """Server-side filter first; if Intuit refuses PrimaryEmailAddr as a filter (400), match locally over a
    bounded scan, case-insensitively either way."""
    # TODO(quickbooks): confirm on a sandbox company whether the server-side filter is accepted and
    # case-insensitive (spec §6); drop whichever branch proves useless.
    try:
        found = _qb_query(f"SELECT * FROM Customer WHERE PrimaryEmailAddr = {_q(email)}", "Customer", max_records=50)
    except api.ApiError as exc:
        if exc.status != 400:
            raise
        found = []
    if found:
        return found
    scan = _qb_query("SELECT * FROM Customer", "Customer", page_size=1000, max_records=_EMAIL_SCAN_MAX)
    return [c for c in scan if _email_of(c) == email.lower()]


def query_invoices(
    *,
    customer_id: str | None = None,
    invoice_id: str | None = None,
    number: str | None = None,
    open_only: bool = False,
    limit: int = 20,
) -> list[dict]:
    """Invoices by QB Id, by invoice number (DocNumber), or for a customer (newest first)."""
    if invoice_id:
        inv = _get(f"invoice/{_id(invoice_id, 'invoice id')}").get("Invoice")
        return [inv] if inv else []
    where = []
    if number:
        where.append(f"DocNumber = {_q(number.strip())}")
    if customer_id:
        where.append(f"CustomerRef = {_q(_id(customer_id, 'customer id'))}")
    if open_only:
        where.append("Balance > '0'")
    sql = "SELECT * FROM Invoice"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDERBY MetaData.LastUpdatedTime DESC"
    return _qb_query(sql, "Invoice", max_records=limit)


def query_payments(*, customer_id: str | None = None, limit: int = 20) -> list[dict]:
    """Payments, optionally for one customer (newest first)."""
    sql = "SELECT * FROM Payment"
    if customer_id:
        sql += f" WHERE CustomerRef = {_q(_id(customer_id, 'customer id'))}"
    sql += " ORDERBY TxnDate DESC"
    return _qb_query(sql, "Payment", max_records=limit)


def query_company_info() -> dict | None:
    """The connected company (CompanyInfo is a one-row entity; querying it needs no realm id)."""
    rows = _qb_query("SELECT * FROM CompanyInfo", "CompanyInfo", max_records=1)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _val(obj: Any, path: str) -> Any:
    found, v = api._dget(obj, path.split("."))
    return v if found else None


def _money(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _customer_to_md(c: dict) -> str:
    name = _val(c, "DisplayName") or "(unknown)"
    company = _val(c, "CompanyName") or ""
    email = _val(c, "PrimaryEmailAddr.Address") or ""
    phone = _val(c, "PrimaryPhone.FreeFormNumber") or ""
    balance = _val(c, "Balance")
    lines = [f"### Customer {_val(c, 'Id') or ''}: {name}"]
    if company and company != name:
        lines.append(f"- Company: {company}")
    if email:
        lines.append(f"- Email: {email}")
    if phone:
        lines.append(f"- Phone: {phone}")
    if balance is not None:
        lines.append(f"- Open balance: {_money(balance)}")
    if _val(c, "Active") is False:
        lines.append("- **Inactive**")
    return "\n".join(lines)


def _invoice_to_md(inv: dict) -> str:
    iid = _val(inv, "Id") or ""
    doc = _val(inv, "DocNumber") or iid
    date = (_val(inv, "TxnDate") or "")[:10]
    due = (_val(inv, "DueDate") or "")[:10]
    total = _val(inv, "TotalAmt")
    balance = _val(inv, "Balance")
    cust = _val(inv, "CustomerRef.name") or _val(inv, "CustomerRef.value") or ""
    bill_email = _val(inv, "BillEmail.Address") or ""
    email_status = _val(inv, "EmailStatus") or ""
    lines = [f"### Invoice {doc} (Id {iid})"]
    lines.append(f"- Date: {date}" + (f" | Due: {due}" if due else ""))
    if cust:
        lines.append(f"- Customer: {cust} (Id {_val(inv, 'CustomerRef.value') or '?'})")
    if total is not None:
        status = ""
        if balance is not None:
            status = " | **paid**" if float(balance) == 0 else f" | open balance {_money(balance)}"
        lines.append(f"- Total: {_money(total)}{status}")
    if bill_email:
        lines.append(f"- Billed to: {bill_email}")
    if email_status:
        lines.append(f"- Email status: {email_status}")
    return "\n".join(lines)


def _payment_to_md(p: dict) -> str:
    lines = [f"### Payment {_val(p, 'Id') or ''} on {(_val(p, 'TxnDate') or '')[:10]}"]
    cust = _val(p, "CustomerRef.name") or _val(p, "CustomerRef.value") or ""
    if cust:
        lines.append(f"- Customer: {cust}")
    lines.append(f"- Amount: {_money(_val(p, 'TotalAmt'))}")
    unapplied = _val(p, "UnappliedAmt")
    if unapplied not in (None, 0, 0.0):
        lines.append(f"- Unapplied: {_money(unapplied)}")
    linked = [
        str(txn.get("TxnId"))
        for line in (_val(p, "Line") or [])
        for txn in ((line or {}).get("LinkedTxn") or [])
        if txn.get("TxnType") == "Invoice"
    ]
    if linked:
        lines.append(f"- Applied to invoice Ids: {', '.join(linked)}")
    return "\n".join(lines)


def _list_md(items: list[dict], title: str, empty: str, render) -> str:
    if not items:
        return f"# {title}\n\n{empty}"
    return f"# {title} ({len(items)} found)\n\n" + "\n\n".join(render(i) for i in items)


def customers_to_markdown(customers: list[dict], *, title: str = "QuickBooks Customers") -> str:
    return _list_md(customers, title, "No customers found.", _customer_to_md)


def invoices_to_markdown(invoices: list[dict], *, title: str = "QuickBooks Invoices") -> str:
    return _list_md(invoices, title, "No invoices found.", _invoice_to_md)


def payments_to_markdown(payments: list[dict], *, title: str = "QuickBooks Payments") -> str:
    return _list_md(payments, title, "No payments found.", _payment_to_md)


def company_to_markdown(info: dict | None) -> str:
    if info is None:
        return "# QuickBooks Company\n\nNo company info found."
    name = _val(info, "CompanyName") or ""
    legal = _val(info, "LegalName") or ""
    addr = _val(info, "CompanyAddr") or {}
    lines = ["# QuickBooks Company Info"]
    if name:
        lines.append(f"- Name: {name}")
    if legal and legal != name:
        lines.append(f"- Legal name: {legal}")
    if isinstance(addr, dict) and addr.get("City"):
        lines.append(f"- City: {addr['City']}")
    if _val(info, "Country"):
        lines.append(f"- Country: {_val(info, 'Country')}")
    if _val(info, "FiscalYearStartMonth"):
        lines.append(f"- Fiscal year starts: {_val(info, 'FiscalYearStartMonth')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.quickbooks",
        description="Read QuickBooks Online customers, invoices, payments, and company info.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    cust_p = sub.add_parser("customer", help="find customers by name, email, or QB Id")
    cust_p.add_argument("name", nargs="?", default="", help="display name (partial match)")
    cust_p.add_argument("--email", default="", help="primary email (exact)")
    cust_p.add_argument("--customer-id", default="", dest="customer_id", help="QB customer Id")

    inv_p = sub.add_parser("invoices", help="list invoices, newest first")
    inv_p.add_argument("--customer-id", default="", dest="customer_id", help="QB customer Id")
    inv_p.add_argument("--open", action="store_true", dest="open_only", help="only unpaid (Balance > 0)")
    inv_p.add_argument("--limit", type=int, default=20)

    inv1_p = sub.add_parser("invoice", help="one invoice by QB Id or by invoice number")
    inv1_p.add_argument("id", nargs="?", default="", help="QB invoice Id")
    inv1_p.add_argument("--number", default="", help="invoice number as the customer sees it (DocNumber)")

    pay_p = sub.add_parser("payments", help="list payments, newest first")
    pay_p.add_argument("--customer-id", default="", dest="customer_id", help="QB customer Id")
    pay_p.add_argument("--limit", type=int, default=20)

    sub.add_parser("company", help="show the connected company")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "customer":
            if not args.name and not args.email and not args.customer_id:
                parser.error("customer requires a name, --email, or --customer-id")
            print(customers_to_markdown(query_customer(
                args.name or None, email=args.email or None, customer_id=args.customer_id or None)))
        elif args.cmd == "invoices":
            print(invoices_to_markdown(query_invoices(
                customer_id=args.customer_id or None, open_only=args.open_only, limit=args.limit)))
        elif args.cmd == "invoice":
            if bool(args.id) == bool(args.number):
                parser.error("invoice takes either an Id or --number")
            invs = query_invoices(invoice_id=args.id or None, number=args.number or None, limit=5)
            print(invoices_to_markdown(invs, title=f"QuickBooks Invoice {args.number or args.id}"))
        elif args.cmd == "payments":
            print(payments_to_markdown(query_payments(customer_id=args.customer_id or None, limit=args.limit)))
        elif args.cmd == "company":
            print(company_to_markdown(query_company_info()))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
