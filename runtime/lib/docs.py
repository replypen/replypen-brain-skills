"""Host-fetched documents: the HOST downloads a document of record and keeps a receipt of its bytes.

Attach the written file UNCHANGED and delivery knows it is the provider's own document, so it is not
held to the "does this look like code" check that a file you generated is. Edit, merge or re-save it and
it is an ordinary generated file again. Renaming is fine (the receipt is the bytes, not the name).

    from lib import docs
    # A Stripe invoice/credit-note PDF link (`invoice.invoice_pdf`, `credit_note.pdf`):
    path = docs.fetch_url(inv["invoice_pdf"], "/tmp/outbox/Invoice-2024-001.pdf")
    # A Rails ActiveStorage blob, on projects with a document store (read through your own scoped view):
    path = docs.fetch_blob(row["blob_id"], "/tmp/outbox/attest.pdf")

Only listed document sources are fetched; any other URL is refused with the reason. An expired Stripe
link answers an error: re-read the invoice for a fresh one.

CLI:
    python -m lib.docs url <url> <dest>
    python -m lib.docs blob <blob_id> <dest>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

from lib import _http_audit

BROKER_URL = "http://rc-broker.internal/docs/"
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 120.0


class DocsError(RuntimeError):
    """The host refused or failed — carries its own sentence."""


class DocsUnavailable(DocsError):
    """No docs mount in this run (an older host, or a local run without the broker)."""


def fetch_url(url: str, dest: str) -> str:
    """Have the host download a listed document link into ``dest``; returns ``dest``."""
    # The link is a bearer credential (anyone holding it can download the document): never audit it.
    return _fetch("url", {"url": url}, dest, known_secrets=(url,))


def fetch_blob(blob_id: str, dest: str) -> str:
    """Have the host download an ActiveStorage blob (by ``active_storage_blobs.id``) into ``dest``."""
    return _fetch("blob", {"blob_id": str(blob_id)}, dest)


def _fetch(kind: str, body: dict, dest: str, known_secrets: tuple[str, ...] = ()) -> str:
    import requests

    try:
        resp = _http_audit.request(
            "POST",
            BROKER_URL + kind,
            json_body=body,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            endpoint_template=f"/docs/{kind}",
            known_secrets=known_secrets,
        )
    except requests.exceptions.ConnectionError as exc:
        raise DocsUnavailable("Host document fetch is not available in this run") from exc
    if resp.status_code != 200:
        message = resp.text.strip() if resp.text else ""
        if resp.status_code == 404 and not message:
            raise DocsUnavailable("Host document fetch is not available in this run")
        raise DocsError(message or f"document fetch failed (HTTP {resp.status_code})")
    data = resp.content
    want = resp.headers.get("X-Docs-Sha256", "")
    if want and hashlib.sha256(data).hexdigest() != want:
        raise DocsError("the document arrived damaged (digest mismatch); fetch it again")
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("kind", choices=("url", "blob"))
    p.add_argument("ref", help="the document link (url) or active_storage_blobs.id (blob)")
    p.add_argument("dest", help="where to write it, e.g. /tmp/outbox/<name>.pdf")
    a = p.parse_args(argv)
    try:
        path = fetch_url(a.ref, a.dest) if a.kind == "url" else fetch_blob(a.ref, a.dest)
    except DocsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"fetched -> {path} ({os.path.getsize(path)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
