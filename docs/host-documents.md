# Host-fetched documents (`lib.docs`)

When a reply should carry a provider's **document of record** (a Stripe invoice, a tenant's stored
attestation or contract), the HOST downloads it, not your script. The host keeps a receipt of the exact
bytes it served to this run; a file attached **unchanged** is then delivered as that document and is not
held to the "does this look like code" check generated files get. That check is what used to withhold
genuine invoices and tenant PDFs.

```python
from lib import docs
docs.fetch_url(invoice["invoice_pdf"], "/tmp/outbox/Invoice-2024-001.pdf")  # Stripe link
docs.fetch_blob(row["blob_id"], "/tmp/outbox/attest.pdf")                   # ActiveStorage blob
```

CLI: `python -m lib.docs url <link> <dest>` · `python -m lib.docs blob <blob_id> <dest>`.

- **Use it for** every download of a listed source, also when you only READ the document (on a project
  with a document store the workspace holds no S3 keys, so this is the only way in). Never
  `requests.get` a Stripe PDF link or `boto3` the bucket yourself.
- **Sources (host-owned list).**
  - `url`: Stripe `invoice_pdf` (invoice) and `pdf` (credit note) links, exactly as the API returns them.
    Anything else is refused with the reason.
  - `blob`: a Rails ActiveStorage `active_storage_blobs.id`, on projects whose operator configured a
    document store. The host re-reads the row through THIS run's scoped view, so tenant, end-user and
    hidden-file rules apply exactly as to your own query.
- **The receipt.** Bytes, not name: renaming is fine; re-saving, merging, stamping, unzipping or
  converting makes it an ordinary generated file again. It is valid only in the run that fetched it.
  Everything else at delivery (size caps, file-type check, the mirror-copy check) still applies.
- **Errors** (`DocsError`, the host's sentence): link expired (Stripe answers 400 — re-read the invoice
  for a fresh link), not a listed source, blob not visible to this run (outside scope, hidden, absent),
  no document store configured (operator setting — say so, don't work around it), one fetch at a time
  per run, 60 per run, 50 MB per document. `DocsUnavailable` = no mount in this run (local dev).
- **Not for** files you generate, Google Drive, or arbitrary URLs. A new source is a host change, admitted
  only when no run can write to it (otherwise an earlier run could plant content a later run fetches as
  "trusted"): URL rules in ReplyPen `internal/docfetch/rules.go`, stores via sealed `DOCSTORE_*` env
  (`DSN_ENV`, `S3_BUCKET`, `S3_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).

Design record: ReplyPen `.agents/skills/chat/confidentiality.md` ("Host-fetched documents").
