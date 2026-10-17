# The workspace image — the SAME image prod runs, published so a client reproduces the
# box with NO replypen source. Build context is the repo root:
#
#     docker build -f docker/Dockerfile -t ghcr.io/replypen/workspace:vX.Y.Z .
#     docker push   ghcr.io/replypen/workspace:vX.Y.Z
#
# Pin the image tag to the replypen-runtime / plugin tag (see RELEASING.md): the bytes installed below ARE
# `replypen-runtime@vX.Y.Z` (we install the in-repo runtime/ package), and prod's
# replypen/runtime/Dockerfile installs the same package by the same pin — so "tested in this
# image" provably equals "runs in prod".
#
# Faithful to replypen's runtime/Dockerfile: the host starts ONE container per run
# (`docker run -d <image> sleep infinity`) driven by many `docker exec`; files/installs/tmp persist.
# The kit's docker mode runs it one-shot (`docker run --rm`) for the pre-push check, same image.
FROM python:3.12-slim

# git    : the agent reads /mirrors history (git log/show/blame/diff) — mounts are :ro, never written.
# ripgrep: `rg` — fast navigation over the read-only mirrors + brain.
# iptables: NOT for the agent (cap-dropped, non-root). The host reuses THIS image for a short-lived
#          privileged sidecar that installs the default-deny egress firewall; inert without NET_ADMIN.
# jq     : shapes/filters connector JSON in the same pipe as curl; prod runs died on `jq: command not found`.
# ffmpeg : general media tooling the agent calls ad-hoc.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ripgrep iptables jq ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Git refuses a repo owned by another uid; every /mirrors mount is host-owned while the agent runs as
# uid 10001. Marking dirs safe is sound here — the foreign-owned repos the agent reaches are
# kernel-enforced :ro mounts from our own host (writes still fail EROFS).
RUN git config --system safe.directory '*'

# uv is the only Python package manager (CLAUDE.md). Pinned by digest-tagged version, never :latest.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# Install the helper library as the `replypen-runtime` PACKAGE (NOT a bare `COPY lib`) — its pinned
# client deps (psycopg/stripe/boto3/requests/markdownify) come from runtime/pyproject.toml, the ONE
# dependency source of truth. The import name stays `lib` (`from lib import db`), so brain scripts and
# prod agree. `--system` (no venv): the workspace is single-purpose.
# The `[test]` extra bakes pytest in so docker mode can run the brain's test tiers
# offline — the image is self-sufficient, no in-container network/uv-resolve needed at test time.
# `-c requirements.lock`: constrain the transitive closure to the SAME committed lock uv mode installs
# from, so the image's import surface is byte-identical to a local uv run (no build-time PyPI drift).
COPY runtime/ /opt/replypen-runtime/
RUN uv pip install --system --no-cache -c /opt/replypen-runtime/requirements.lock "/opt/replypen-runtime[test]"

# Unbuffered so stdout/stderr reach the host promptly even on a killed (timed-out) container.
ENV PYTHONUNBUFFERED=1

# Non-root user — defense-in-depth on the shared kernel. Home path MUST match the tmpfs the host
# mounts here (internal/workspace/docker.go homeDir = /home/agent) so the read-only rootfs still gives
# a writable home. "agent" — never "sandbox" (taxonomy bans the word).
RUN useradd --create-home --uid 10001 agent
USER agent
WORKDIR /home/agent

# Host overrides CMD with `sleep infinity` to keep the detached container alive; a sane default here.
CMD ["bash"]
