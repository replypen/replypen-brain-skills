"""Shopify Admin GraphQL connector — force-code: GraphQL transport (POST) + field pre-selection.

lib.api is a GET/REST client; Shopify's Admin API is GraphQL-over-POST with per-shop base URLs.
This connector wraps GraphQL POSTs, handles cursor pagination (pageInfo.endCursor / hasNextPage
embedded in query variables, not in HTTP headers/params), and pre-selects the support-relevant
fields from the heavily nested edges/nodes response shape before returning them to the agent.

Four entry points answer real support questions:
  orders   — recent orders: status, financial state, totals, customer email, fulfillment/tracking,
             refunded total and return status
  order    — one order by name, plus each refund's amount, date, money status and items
  customer — lookup by email or GID: name, email, orders count, phone, tags
  product  — lookup by GID: title, handle, status, variants (price, inventory), tags

Auth: X-Shopify-Access-Token header (raw token, no "Bearer" prefix) or brokered host-side token.
HTTP/retry/method policy rides lib.api so broker mode and env mode share one path.

CLI:
    python -m lib.connectors.shopify --shop SLUG orders [--limit N] [--query FILTER]
    python -m lib.connectors.shopify --shop SLUG order --name '#1001'      (with refund detail)
    python -m lib.connectors.shopify --shop SLUG customer --ref EMAIL_OR_GID
    python -m lib.connectors.shopify --shop SLUG product --id PRODUCT_GID
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from lib import api

# Shopify stable Admin GraphQL version — update annually when Shopify releases a new stable version.
_API_VERSION = "2026-07"


# ---------------------------------------------------------------------------
# Low-level GraphQL caller
# ---------------------------------------------------------------------------


def _post(shop: str, query: str, variables: dict | None = None) -> dict:
    """POST a GraphQL query through lib.api so brokered credentials stay host-side."""
    slug = shop.rstrip("/").removesuffix(".myshopify.com")
    manifest = api.Manifest(
        key="shopify",
        base_url=f"https://{slug}.myshopify.com/admin/api/{_API_VERSION}",
        auth=api.Auth(strategy="api_key_header", name="X-Shopify-Access-Token"),
        allowed_post_paths=("graphql.json", "/admin/api/*/graphql.json"),
    )
    client = api.client(manifest, token_key="shopify")
    payload = {"query": query, "variables": variables or {}}
    try:
        data = client.post("graphql.json", json_body=payload)
    except api.ApiError as e:
        raise RuntimeError(f"Shopify GraphQL HTTP {e.status}: {e.body[:800]}") from e
    if "errors" in data:
        errs = json.dumps(data["errors"])[:800]
        raise RuntimeError(f"Shopify GraphQL errors: {errs}")
    return data


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

# Shopify rejects any query whose *requested* cost exceeds 1000 points before running it; a connection
# costs roughly `first` x its node cost. Refund detail (nested refund line items + transactions) costs
# ~400 points per order, so it lives only in the one-order query; the list query carries the cheap totals.
_MONEY = "{ shopMoney { amount currencyCode } presentmentMoney { amount currencyCode } }"

_ORDER_FIELDS = """
        id
        name
        email
        createdAt
        updatedAt
        displayFinancialStatus
        displayFulfillmentStatus
        totalPriceSet { shopMoney { amount currencyCode } }
        customer { email displayName }
        shippingAddress { city countryCode }
        lineItems(first: 5) {
          edges {
            node { title quantity }
          }
        }
        tags
        cancelledAt
        cancelReason
        fulfillments(first: 3) {
          status
          updatedAt
          trackingInfo { company number url }
        }
        returnStatus
        totalRefundedSet """ + _MONEY + """
"""

_REFUND_FIELDS = """
        refunds(first: 5) {
          createdAt
          processedAt
          note
          totalRefundedSet """ + _MONEY + """
          refundLineItems(first: 10) {
            nodes { quantity subtotalSet """ + _MONEY + """ lineItem { title } }
            pageInfo { hasNextPage }
          }
          transactions(first: 5) {
            nodes {
              kind status processedAt gateway formattedGateway manualPaymentGateway test
              amountSet """ + _MONEY + """
            }
            pageInfo { hasNextPage }
          }
        }
"""
_REFUNDS_CAP = 5

_ORDERS_QUERY = """
query Orders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
    edges {
      cursor
      node {""" + _ORDER_FIELDS + """      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

# Name search is fuzzy (a near match can rank first), so look up a cheap page of id+name, pick the exact
# name, then fetch the expensive refund detail for that one id.
_ORDER_NAME_SEARCH_QUERY = """
query OrderNameSearch($query: String!) {
  orders(first: 10, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes { id name }
  }
}
"""

_ORDER_DETAIL_QUERY = """
query OrderDetail($id: ID!) {
  order(id: $id) {""" + _ORDER_FIELDS + _REFUND_FIELDS + """  }
}
"""


def _money(money_bag: dict | None) -> str:
    """Customer-facing amount: the currency the buyer paid in, plus the shop currency when it differs."""
    if not money_bag:
        return ""
    shop = money_bag.get("shopMoney") or {}
    buyer = money_bag.get("presentmentMoney") or shop
    try:
        text = f"{buyer['amount']} {buyer['currencyCode']}"
    except (KeyError, TypeError):
        return ""
    if shop.get("currencyCode") and shop["currencyCode"] != buyer.get("currencyCode"):
        text += f" (shop currency: {shop.get('amount')} {shop['currencyCode']})"
    return text


def _destination(t: dict) -> str:
    if t.get("test"):
        return "test transaction (no real money)"
    if (t.get("gateway") or "") == "shopify_store_credit":
        return "store credit"
    if t.get("manualPaymentGateway"):
        return "manual gateway (recorded only; Shopify moved no money)"
    return "customer's payment method"


def _refund(r: dict) -> dict:
    """One Refund record plus its REFUND transactions as-is: the record alone does not prove money moved,
    and its total can differ from what the transactions settled, so no aggregate status is derived."""
    tx_conn = r.get("transactions") or {}
    txs = [{"amount": _money(t.get("amountSet")), "status": t.get("status") or "UNKNOWN",
            "processed_at": t.get("processedAt"), "gateway": t.get("formattedGateway") or t.get("gateway"),
            "destination": _destination(t)}
           for t in tx_conn.get("nodes") or [] if t.get("kind") == "REFUND"]
    li_conn = r.get("refundLineItems") or {}
    items = [{"title": (li.get("lineItem") or {}).get("title"), "quantity": li.get("quantity"),
              "subtotal": _money(li.get("subtotalSet"))}
             for li in li_conn.get("nodes") or []]
    return {
        "amount": _money(r.get("totalRefundedSet")),
        "created_at": r.get("createdAt"),
        "processed_at": r.get("processedAt"),
        "note": r.get("note"),
        "line_items": items,
        "line_items_truncated": bool((li_conn.get("pageInfo") or {}).get("hasNextPage")),
        "transactions": txs,
        "transactions_truncated": bool((tx_conn.get("pageInfo") or {}).get("hasNextPage")),
    }


def _order_node(node: dict) -> dict:
    """Pre-select support-relevant fields from a raw order node."""
    total = _money(node.get("totalPriceSet"))

    customer_email = ""
    customer_name = ""
    try:
        c = node["customer"]
        if c:
            customer_email = c.get("email") or ""
            customer_name = c.get("displayName") or ""
    except (KeyError, TypeError):
        pass

    line_items = []
    try:
        for e in node.get("lineItems", {}).get("edges", []):
            n = e.get("node", {})
            line_items.append({"title": n.get("title"), "quantity": n.get("quantity")})
    except (KeyError, TypeError):
        pass

    ship = node.get("shippingAddress") or {}

    tracking = []
    for f in node.get("fulfillments") or []:
        for t in f.get("trackingInfo") or []:
            tracking.append({"company": t.get("company"), "number": t.get("number"), "url": t.get("url"),
                             "status": f.get("status"), "updated_at": f.get("updatedAt")})

    raw_refunds = node.get("refunds") or []
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "created_at": node.get("createdAt"),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "total": total,
        "order_email": node.get("email") or "",
        "customer_email": customer_email,
        "customer_name": customer_name,
        "shipping_city": ship.get("city"),
        "shipping_country": ship.get("countryCode"),
        "line_items": line_items,
        "tags": node.get("tags") or [],
        "cancelled_at": node.get("cancelledAt"),
        "cancel_reason": node.get("cancelReason"),
        "tracking": tracking,
        "return_status": node.get("returnStatus"),
        "total_refunded": _money(node.get("totalRefundedSet")),
        "refunds": [_refund(r) for r in raw_refunds],
        "refunds_truncated": len(raw_refunds) >= _REFUNDS_CAP,
    }


def fetch_orders(shop: str, *, limit: int = 50, query_filter: str = "") -> list[dict]:
    """Fetch up to ``limit`` recent orders, auto-paging with GraphQL cursor pagination."""
    PAGE_SIZE = min(limit, 25)  # ~20 points per order keeps a page well under the 1000-point cap
    orders: list[dict] = []
    after: str | None = None

    while len(orders) < limit:
        remaining = limit - len(orders)
        variables: dict[str, Any] = {"first": min(PAGE_SIZE, remaining)}
        if after:
            variables["after"] = after
        if query_filter:
            variables["query"] = query_filter

        data = _post(shop, _ORDERS_QUERY, variables)
        conn = data.get("data", {}).get("orders", {})
        edges = conn.get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            orders.append(_order_node(node))
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not edges:
            break
        after = page_info.get("endCursor")

    return orders[:limit]


def _search_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_order(shop: str, name: str) -> dict | None:
    """One order by its exact name (#1001) with refund detail; a bare number also tries '#<number>'."""
    name = (name or "").strip()
    if not name:
        raise RuntimeError("order name (e.g. #1001) is required")
    candidates = [name] + ([f"#{name}"] if name.isdigit() else [])
    for candidate in candidates:
        data = _post(shop, _ORDER_NAME_SEARCH_QUERY, {"query": f"name:{_search_value(candidate)}"})
        hits = (data.get("data") or {}).get("orders", {}).get("nodes") or []
        exact = next((h for h in hits if (h.get("name") or "").casefold() == candidate.casefold()), None)
        if exact:
            detail = _post(shop, _ORDER_DETAIL_QUERY, {"id": exact["id"]})
            node = (detail.get("data") or {}).get("order")
            return _order_node(node) if node else None
    return None


# ---------------------------------------------------------------------------
# Customer lookup
# ---------------------------------------------------------------------------

_CUSTOMER_BY_EMAIL_QUERY = """
query CustomerByEmail($query: String!) {
  customers(first: 1, query: $query) {
    edges {
      node {
        id
        displayName
        email
        phone
        numberOfOrders
        amountSpent { amount currencyCode }
        tags
        createdAt
        state
      }
    }
  }
}
"""

_CUSTOMER_BY_GID_QUERY = """
query CustomerById($id: ID!) {
  customer(id: $id) {
    id
    displayName
    email
    phone
    numberOfOrders
    amountSpent { amount currencyCode }
    tags
    createdAt
    state
  }
}
"""


def _customer_node(node: dict) -> dict:
    total = ""
    try:
        t = node.get("amountSpent") or {}
        total = f"{t['amount']} {t['currencyCode']}"
    except (KeyError, TypeError):
        pass
    return {
        "id": node.get("id"),
        "name": node.get("displayName"),
        "email": node.get("email"),
        "phone": node.get("phone"),
        "orders_count": node.get("numberOfOrders"),
        "total_spent": total,
        "tags": node.get("tags") or [],
        "created_at": node.get("createdAt"),
        "state": node.get("state"),
    }


def fetch_customer(shop: str, ref: str) -> dict | None:
    """Look up a customer by email or GID (gid://shopify/Customer/…). Returns None if not found."""
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("customer ref (email or GID) is required")

    if ref.startswith("gid://shopify/Customer/"):
        data = _post(shop, _CUSTOMER_BY_GID_QUERY, {"id": ref})
        node = (data.get("data") or {}).get("customer")
        return _customer_node(node) if node else None

    # Email lookup via query= filter
    data = _post(shop, _CUSTOMER_BY_EMAIL_QUERY, {"query": f"email:{ref}"})
    edges = (data.get("data") or {}).get("customers", {}).get("edges") or []
    if not edges:
        return None
    return _customer_node(edges[0]["node"])


# ---------------------------------------------------------------------------
# Product lookup
# ---------------------------------------------------------------------------

_PRODUCT_QUERY = """
query ProductById($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    status
    tags
    createdAt
    updatedAt
    variants(first: 10) {
      edges {
        node {
          id
          title
          price
          sku
          inventoryQuantity
          availableForSale
        }
      }
    }
  }
}
"""


def _product_node(node: dict) -> dict:
    variants = []
    try:
        for e in node.get("variants", {}).get("edges", []):
            v = e.get("node", {})
            variants.append({
                "id": v.get("id"),
                "title": v.get("title"),
                "price": v.get("price"),
                "sku": v.get("sku"),
                "inventory": v.get("inventoryQuantity"),
                "available": v.get("availableForSale"),
            })
    except (KeyError, TypeError):
        pass
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "handle": node.get("handle"),
        "status": node.get("status"),
        "tags": node.get("tags") or [],
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "variants": variants,
    }


def fetch_product(shop: str, product_id: str) -> dict | None:
    """Fetch a product by GID. Returns None if not found."""
    if not product_id:
        raise RuntimeError("product GID is required")
    data = _post(shop, _PRODUCT_QUERY, {"id": product_id})
    node = (data.get("data") or {}).get("product")
    return _product_node(node) if node else None


# ---------------------------------------------------------------------------
# Markdown renderers
# ---------------------------------------------------------------------------


def _is_zero(money: str) -> bool:
    try:
        return float(money.split()[0]) == 0
    except (ValueError, IndexError):
        return False


def order_to_markdown(order: dict | None, name: str, shop: str) -> str:
    if not order:
        return (f"# Shopify order not found\nNo order named `{name}` on shop `{shop}`. Orders older than "
                "60 days are invisible unless the store granted read_all_orders.")
    md = orders_to_markdown([order], shop).split("\n", 1)[1]
    if not order.get("refunds"):
        md += "\n- Refunds: none"
    return f"# Shopify order {order.get('name')}: {shop}" + md


def orders_to_markdown(orders: list[dict], shop: str) -> str:
    if not orders:
        return f"# Shopify orders: {shop}\nNo orders found."
    lines = [f"# Shopify orders: {shop} ({len(orders)} shown)"]
    for o in orders:
        status = f"{o.get('financial_status') or '?'} / {o.get('fulfillment_status') or '?'}"
        lines.append(f"\n## {o.get('name') or o.get('id')}")
        lines.append(f"- Status: {status}")
        lines.append(f"- Total: {o.get('total') or '—'}")
        lines.append(f"- Customer: {o.get('customer_email') or '—'} {o.get('customer_name') or ''}".rstrip())
        if o.get("order_email") and o["order_email"] != o.get("customer_email"):
            lines.append(f"- Order email: {o['order_email']}")
        lines.append(f"- Created: {o.get('created_at') or '—'}")
        for t in o.get("tracking") or []:
            lines.append(f"- Tracking: {t.get('company') or '?'} {t.get('number') or ''} {t.get('url') or ''} ({t.get('status') or '?'})".rstrip())
        if o.get("return_status") and o["return_status"] != "NO_RETURN":
            lines.append(f"- Return: {o['return_status']}")
        if o.get("total_refunded") and not o.get("refunds") and not _is_zero(o["total_refunded"]):
            lines.append(f"- Refunded so far: {o['total_refunded']} (run `order --name \"{o.get('name')}\"` for dates and status)")
        for r in o.get("refunds") or []:
            lines.append(f"- Refund record: {r.get('amount') or '?'} created {r.get('created_at') or '?'}")
            for t in r.get("transactions") or []:
                lines.append(f"  - {t.get('amount') or '?'} to {t['destination']} via {t.get('gateway') or '?'}:"
                             f" {t['status']} at {t.get('processed_at') or '?'}")
            if not r.get("transactions"):
                lines.append("  - No refund transaction: nothing was paid back through a gateway"
                             " (store credit or a manually recorded refund; check the note)")
            if r.get("transactions_truncated"):
                lines.append("  - (more transactions exist than shown)")
            if r.get("line_items"):
                more = " (more items not shown)" if r.get("line_items_truncated") else ""
                lines.append("  - Refunded items: "
                             + ", ".join(f"{li['title']} ×{li['quantity']}" for li in r["line_items"]) + more)
            if r.get("note"):
                lines.append(f"  - Note: {r['note']}")
        if o.get("refunds_truncated"):
            lines.append(f"- (only the first {_REFUNDS_CAP} refunds are shown; later ones may exist)")
        if o.get("cancelled_at"):
            lines.append(f"- **Cancelled**: {o['cancel_reason'] or ''} at {o['cancelled_at']}")
        items = o.get("line_items") or []
        if items:
            item_str = ", ".join(f"{it['title']} ×{it['quantity']}" for it in items)
            lines.append(f"- Items: {item_str}")
        if o.get("tags"):
            lines.append(f"- Tags: {', '.join(o['tags'])}")
    return "\n".join(lines)


def customer_to_markdown(customer: dict | None, ref: str, shop: str) -> str:
    if not customer:
        return f"# Shopify customer not found\nNo customer matched `{ref}` on shop `{shop}`."
    lines = [f"# Shopify customer: {customer.get('email') or customer.get('id')}"]
    lines.append(f"- Name: {customer.get('name') or '—'}")
    if customer.get("phone"):
        lines.append(f"- Phone: {customer['phone']}")
    lines.append(f"- Orders: {customer.get('orders_count') or 0}, Total spent: {customer.get('total_spent') or '—'}")
    lines.append(f"- State: {customer.get('state') or '—'}")
    lines.append(f"- Customer since: {customer.get('created_at') or '—'}")
    if customer.get("tags"):
        lines.append(f"- Tags: {', '.join(customer['tags'])}")
    return "\n".join(lines)


def product_to_markdown(product: dict | None, product_id: str, shop: str) -> str:
    if not product:
        return f"# Shopify product not found\nNo product matched `{product_id}` on shop `{shop}`."
    lines = [f"# Shopify product: {product.get('title') or product.get('id')}"]
    lines.append(f"- Handle: {product.get('handle') or '—'}")
    lines.append(f"- Status: {product.get('status') or '—'}")
    lines.append(f"- Created: {product.get('created_at') or '—'}")
    variants = product.get("variants") or []
    if variants:
        lines.append(f"- Variants ({len(variants)}):")
        for v in variants:
            inv = v.get("inventory")
            avail = "in stock" if v.get("available") else "out of stock"
            sku = f" SKU:{v['sku']}" if v.get("sku") else ""
            lines.append(f"  - {v.get('title') or '?'}: {v.get('price') or '?'}{sku}, {inv if inv is not None else '?'} units ({avail})")
    if product.get("tags"):
        lines.append(f"- Tags: {', '.join(product['tags'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lib.connectors.shopify",
        description="Shopify Admin GraphQL connector — read-only grounding for support runs.",
    )
    shop_help = "Shopify subdomain slug (e.g. mystore)"
    parser.add_argument("--shop", help=shop_help)
    # --shop is accepted after the subcommand too; SUPPRESS keeps a missing sub-level flag from
    # overwriting a top-level one.
    shop_opt = argparse.ArgumentParser(add_help=False)
    shop_opt.add_argument("--shop", default=argparse.SUPPRESS, help=shop_help)
    sub = parser.add_subparsers(dest="cmd", required=True)

    ord_p = sub.add_parser("orders", parents=[shop_opt], help="list recent orders")
    ord_p.add_argument("--limit", type=int, default=50, help="max orders to fetch (default 50)")
    ord_p.add_argument("--query", default="", dest="query_filter", help="Shopify order query filter (e.g. 'financial_status:unpaid')")

    one_p = sub.add_parser("order", parents=[shop_opt], help="one order by name, with refund detail")
    one_p.add_argument("--name", required=True, help="order name as the customer quotes it, e.g. '#1001'")

    cust_p = sub.add_parser("customer", parents=[shop_opt], help="look up a customer by email or GID")
    cust_p.add_argument("--ref", required=True, help="customer email or GID (gid://shopify/Customer/…)")

    prod_p = sub.add_parser("product", parents=[shop_opt], help="fetch a product by GID")
    prod_p.add_argument("--id", dest="product_id", required=True, help="product GID (gid://shopify/Product/…)")

    args = parser.parse_args(argv)
    if not args.shop:
        parser.error("--shop is required")

    if args.cmd == "orders":
        orders = fetch_orders(args.shop, limit=args.limit, query_filter=args.query_filter)
        print(orders_to_markdown(orders, args.shop))
        return 0
    if args.cmd == "order":
        print(order_to_markdown(fetch_order(args.shop, args.name), args.name, args.shop))
        return 0
    if args.cmd == "customer":
        customer = fetch_customer(args.shop, args.ref)
        print(customer_to_markdown(customer, args.ref, args.shop))
        return 0
    if args.cmd == "product":
        product = fetch_product(args.shop, args.product_id)
        print(product_to_markdown(product, args.product_id, args.shop))
        return 0

    parser.error("unknown command")
    return 2
