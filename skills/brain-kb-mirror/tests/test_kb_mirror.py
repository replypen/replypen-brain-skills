"""Offline fixture tests for brain-kb-mirror (synthetic Zendesk source, no network).

    cd skills/brain-kb-mirror && uv run --no-project --with pytest --with httpx --with beautifulsoup4 \
        --with lxml --with markdownify --with pillow pytest tests -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
from pathlib import Path

import pytest

RUN = "cd skills/brain-kb-mirror && uv run --no-project --with pytest --with httpx ... pytest tests -q (see docstring)"
for _mod in ("httpx", "bs4", "lxml", "markdownify", "PIL"):  # a bare kit-root `pytest` skips instead of erroring
    pytest.importorskip(_mod, reason=f"needs the script's deps: {RUN}")
from PIL import Image  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kb_mirror.py"
spec = importlib.util.spec_from_file_location("kb_mirror", SCRIPT)
kb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb)

BASE = "https://help.example.com"
API = f"{BASE}/api/v2/help_center/en"
NAME = "example-help"


def png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color).save(buf, "PNG")
    return buf.getvalue()


def img(n: int) -> str:
    return f"{BASE}/hc/article_attachments/{n}/shot.png"


def article(aid: int, sid: int, title: str, body: str, pos: int = 0) -> dict:
    return {"id": aid, "title": title, "body": body, "html_url": f"{BASE}/hc/en/articles/{aid}-x", "locale": "en",
            "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-02-01T00:00:00Z",
            "edited_at": "2025-02-01T00:00:00Z", "label_names": ["setup"], "section_id": sid, "position": pos}


class Source:
    """Fake Fetcher: a mutable Zendesk source with paginated articles and ETag'd images."""

    def __init__(self):
        self.categories = [{"id": 1, "name": "Getting started", "position": 0},
                           {"id": 2, "name": "News", "position": 1}]
        self.sections = [{"id": 10, "name": "Basics", "position": 0, "category_id": 1},
                         {"id": 20, "name": "Release notes", "position": 0, "category_id": 2}]
        self.articles = [
            article(101, 10, "Install the app", f'<p>Step one.</p><img src="{img(1)}"><img src="{img(2)}">'
                    f'<p>See <a href="/hc/en/articles/102-login#h_01">login</a> and {{{{x}}}}.</p>'),
            article(102, 10, "Log in", f'<p>Use your account.</p><img src="{img(3)}">', 1),
            article(201, 20, "Version 2.0", '<p>New: dark mode.</p><a href="ShowKnowledgeBaseEntry.do?id=7">old</a>'),
        ]
        self.images = {img(1): png("red"), img(2): png("green"), img(3): png("blue")}
        self.break_pagination = False
        self.requests: list[str] = []

    def get(self, url, headers=None, cap=None):
        self.requests.append(url)
        if url in self.images:
            data = self.images[url]
            etag = '"' + hashlib.md5(data).hexdigest() + '"'
            if (headers or {}).get("If-None-Match") == etag:
                return kb.Resp(304, {}, b"", url)
            return kb.Resp(200, {"content-type": "image/png", "etag": etag}, data, url)
        for key in ("articles", "sections", "categories"):
            items = getattr(self, key)
            if url == f"{API}/{key}.json?per_page=100":  # page 1 of 2 for articles
                page, nxt = (items[:2], f"{API}/{key}.json?page=2&per_page=100") if key == "articles" else (items, None)
            elif url == f"{API}/{key}.json?page=2&per_page=100":
                if self.break_pagination:
                    return kb.Resp(500, {}, b"", url)
                page, nxt = items[2:], None
            else:
                continue
            body = {key: page, "count": len(items), "next_page": nxt, "page_count": 2 if key == "articles" else 1}
            return kb.Resp(200, {"content-type": "application/json"}, json.dumps(body).encode(), url)
        return kb.Resp(404, {}, b"", url)


@pytest.fixture
def brain(tmp_path):
    root = tmp_path / "brain"
    (root / "_internal/kb-sources").mkdir(parents=True)
    (root / "_internal/kb-sources" / f"{NAME}.toml").write_text(
        f'adapter = "zendesk"\nbase_url = "{BASE}"\nlocale = "en"\ntarget = "knowledge/example-help"\n')
    return root


def refresh(brain, src, **kw):
    return kb.refresh(kb.Profile(brain, NAME), fetcher=src, **kw)


def tree(brain: Path) -> dict[str, bytes]:
    files = [*(brain / "knowledge").rglob("*"), *(brain / "_internal/kb-sources").glob("*.json")]
    return {str(p.relative_to(brain)): p.read_bytes() for p in files if p.is_file()}


def describe_all(brain, tmp_path, text="Login screen with 'Sign in' button."):
    p = kb.Profile(brain, NAME)
    outs = []
    for b in kb.describe_todo(p, tmp_path / "batches", 60_000):
        rows = [json.loads(x) for x in b.read_text().splitlines()]
        out = b.with_name(b.name.replace(".jsonl", ".out.jsonl"))
        out.write_text("".join(json.dumps({**r, "text": f"{text} {r['sha'][:6]}"}) + "\n" for r in rows))
        outs.append(out)
    assert kb.describe_apply(p, outs) == 0


A101 = "knowledge/example-help/getting-started/basics/101-install-the-app.md"


def test_render_shape(brain):
    refresh(brain, Source())
    text = (brain / A101).read_text()
    assert "zendesk_id: 101" in text and "content_hash: " in text and "fetched_at" not in text
    assert f"Source: {BASE}/hc/en/articles/101-x" in text
    assert "](102-log-in.md)" in text and "{ {x} }" in text and "{{" not in text
    assert "![image 1](../../_assets/101/1.png)" in text
    rn = (brain / "knowledge/example-help/news/release-notes/201-version-2-0.md").read_text()
    assert "kind: release-note" in rn and "ShowKnowledgeBaseEntry" not in rn


def test_readmes_stay_under_the_doc_budget(brain, monkeypatch):
    monkeypatch.setattr(kb, "README_BUDGET", 480)
    src = Source()
    for i, aid in enumerate((202, 203, 204)):
        src.articles.append({**article(aid, 20, f"Version 2.{i + 1}", "<p>Fixes.</p>"),
                             "created_at": f"2025-0{i + 3}-01T00:00:00Z"})
    refresh(brain, src)
    root = brain / "knowledge/example-help"
    rn = (root / "news/release-notes/README.md").read_text()
    assert rn.index("Version 2.3") < rn.index("Version 2.2")  # release notes newest first under the cap
    assert "Version 2.0" not in rn and "more articles in this folder" in rn
    assert "rg -i '^title:' /brain/knowledge/example-help/news/release-notes" in rn
    index = (root / "INDEX.md").read_text()
    assert "| section (folder) |" not in index and "[`news`](news/README.md)" in index


def test_rerun_is_byte_identical_noop(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    before = tree(brain)
    refresh(brain, src)
    assert tree(brain) == before
    assert any(r in src.requests for r in src.images)  # images were revalidated, not skipped


def test_empty_cache_rebuild(brain, tmp_path):
    refresh(brain, Source())
    describe_all(brain, tmp_path)
    before = tree(brain)
    shutil.rmtree(brain / "_internal/kb-sources/.cache")
    refresh(brain, Source())
    assert tree(brain) == before


def test_changed_bytes_at_same_url_need_reannotation(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    md = brain / "knowledge/example-help/_assets/101/1.md"
    assert md.exists()
    src.images[img(1)] = png("yellow")
    refresh(brain, src)
    assert not md.exists()
    assert "![image 1](" in (brain / A101).read_text()
    todo = kb.describe_todo(kb.Profile(brain, NAME), tmp_path / "again", 60_000)
    rows = [json.loads(x) for b in todo for x in b.read_text().splitlines()]
    assert [r["sha"] for r in rows] == [hashlib.sha256(png("yellow")).hexdigest()]


def test_interrupted_pagination_leaves_live_corpus_untouched(brain):
    src = Source()
    refresh(brain, src)
    before = tree(brain)
    src.articles.append(article(103, 10, "New article", "<p>new</p>", 2))
    src.break_pagination = True
    with pytest.raises(kb.MirrorError, match="HTTP 500"):
        refresh(brain, src)
    assert tree(brain) == before


def test_inventory_collapse_stops(brain):
    src = Source()
    refresh(brain, src)
    del src.articles[1:]
    with pytest.raises(kb.MirrorError, match="inventory collapse"):
        refresh(brain, src)


def test_article_and_image_deletion_pruned(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    del src.articles[1]  # 102 and its image
    src.articles[0]["body"] = src.articles[0]["body"].replace(f'<img src="{img(2)}">', "")
    (brain / "knowledge/example-help/handwritten.md").write_text("not owned\n")
    refresh(brain, src, allow_shrink=True)
    k = brain / "knowledge/example-help"
    assert not (k / "getting-started/basics/102-log-in.md").exists()
    assert not (k / "_assets/102").exists() and not (k / "_assets/101/2.png").exists()
    assert (k / "handwritten.md").exists()  # only manifest-owned paths are pruned


def test_reordered_images_keep_descriptions(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    k = brain / "knowledge/example-help/_assets/101"
    first, second = (k / "1.md").read_text(), (k / "2.md").read_text()
    src.articles[0]["body"] = f'<img src="{img(2)}"><img src="{img(1)}"><p>Swapped.</p>'
    refresh(brain, src)
    assert (k / "1.md").read_text() == second and (k / "2.md").read_text() == first


def test_check_offline_on_fresh_copy(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    assert kb.main(["--brain", str(brain), "check", NAME]) == 1  # undescribed images block publishing
    describe_all(brain, tmp_path)
    fresh = tmp_path / "clone"
    shutil.copytree(brain, fresh, ignore=shutil.ignore_patterns(".cache"))
    assert kb.main(["--brain", str(fresh), "check", NAME]) == 0
    (fresh / A101).write_text((fresh / A101).read_text() + "\n[broken](nope.md)\n")
    assert kb.main(["--brain", str(fresh), "check", NAME]) == 1


def test_describe_apply_rejects_incomplete_batches(brain, tmp_path):
    refresh(brain, Source())
    p = kb.Profile(brain, NAME)
    (batch,) = kb.describe_todo(p, tmp_path / "b", 60_000)
    rows = [json.loads(x) for x in batch.read_text().splitlines()]
    out = batch.with_name("batch-001.out.jsonl")
    out.write_text(json.dumps({**rows[0], "text": "A screen."}) + "\n")  # others missing
    assert kb.describe_apply(p, [out]) == 1
    assert not p.descriptions.exists()


def test_fetcher_blocks_unlisted_and_private_hosts():
    f = kb.Fetcher(["help.example.com", "localhost"], 2.0)
    with pytest.raises(kb.Blocked, match="not allowed"):
        f.check("https://evil.example.net/x.png")
    with pytest.raises(kb.Blocked, match="not allowed"):
        f.check("http://help.example.com/x")
    with pytest.raises(kb.Blocked, match="non-public"):
        f.check("https://localhost/x")


# ---------------------------------------------------------------- review regressions (Astra, 0c7f37a)

def test_unowned_file_is_never_overwritten(brain):
    src = Source()
    refresh(brain, src)
    path = brain / "knowledge/example-help/getting-started/basics/103-custom.md"
    path.write_text("HANDWRITTEN DATA")
    src.articles.append(article(103, 10, "Custom", "<p>upstream</p>", 2))
    before = tree(brain)
    with pytest.raises(kb.MirrorError, match="not owned"):
        refresh(brain, src)
    assert tree(brain) == before
    refresh(brain, src, adopt=True)  # explicit takeover
    assert "upstream" in path.read_text()


def test_conflict_found_before_any_write(brain):
    src = Source()
    refresh(brain, src)
    src.articles[0]["body"] = "<p>changed</p>"
    src.articles.append(article(202, 20, "New", "<p>new</p>"))
    (brain / "knowledge/example-help/news/release-notes/202-new.md").mkdir()
    before = tree(brain)
    with pytest.raises(kb.MirrorError, match="not a regular file"):
        refresh(brain, src)
    assert tree(brain) == before


def test_write_failure_rolls_back(brain, monkeypatch):
    src = Source()
    refresh(brain, src)
    before = tree(brain)
    src.articles[0]["body"] = "<p>changed</p>"
    del src.articles[1]
    real, calls = shutil.copyfile, []

    def flaky(a, b):
        calls.append(b)
        if len(calls) == 3:
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(kb.shutil, "copyfile", flaky)
    with pytest.raises(OSError, match="disk full"):
        refresh(brain, src, allow_shrink=True)
    assert tree(brain) == before


def test_processing_version_is_part_of_the_cache_key(brain, monkeypatch):
    p = kb.Profile(brain, NAME)
    monkeypatch.setattr(kb, "process_image", lambda raw, ct: {"data": kb.PROC_VERSION.encode(), "ext": "png",
                                                                 "width": 1, "height": 1, "downscaled": True})
    monkeypatch.setattr(kb, "PROC_VERSION", "v1")
    kb.processed(p, png("red"), "image/png")
    monkeypatch.setattr(kb, "PROC_VERSION", "v2")
    assert kb.processed(p, png("red"), "image/png")["data"] == b"v2"


def test_language_or_prompt_change_requeues_descriptions(brain, tmp_path):
    refresh(brain, Source())
    describe_all(brain, tmp_path)
    p = kb.Profile(brain, NAME)
    assert kb.describe_todo(p, tmp_path / "same", 60_000) == []
    p.annotation_language = "nl"
    assert kb.describe_todo(p, tmp_path / "nl", 60_000)
    store = json.loads(p.descriptions.read_text())
    p.descriptions.write_text(json.dumps({k: {**v, "prompt_version": "v0-legacy"} for k, v in store.items()}))
    p = kb.Profile(brain, NAME)
    assert kb.describe_todo(p, tmp_path / "legacy", 60_000)
    p.accepted_prompt_versions.add("v0-legacy")  # = profile [annotation] accepted_prompt_versions
    assert kb.describe_todo(p, tmp_path / "accepted", 60_000) == []


def test_lost_upstream_image_degrades_and_blocks_publish(brain, tmp_path):
    src = Source()
    refresh(brain, src)
    describe_all(brain, tmp_path)
    md = brain / "knowledge/example-help/_assets/101/1.md"
    del src.images[img(1)]  # still referenced, now 404
    refresh(brain, src)
    assert md.exists() and "_assets/101/1.png" in (brain / A101).read_text()  # last-good bytes kept
    assert kb.main(["--brain", str(brain), "check", NAME]) == 1
    fresh = tmp_path / "clone"  # no cache: last-good comes from the committed asset
    shutil.copytree(brain, fresh, ignore=shutil.ignore_patterns(".cache"))
    shutil.copytree(brain / "_internal/kb-sources/.cache", fresh / "_internal/kb-sources/.cache",
                    ignore=shutil.ignore_patterns("images"))
    kb.refresh(kb.Profile(fresh, NAME), offline=True)
    assert (fresh / "knowledge/example-help/_assets/101/1.png").exists()
    refresh(brain, src, accept_degraded=True)
    assert kb.main(["--brain", str(brain), "check", NAME]) == 0


# ---------------------------------------------------------------- videos

VID = "AbCdEfGhIjK"


def with_video(src: Source) -> Source:
    src.articles.append(article(104, 10, "Wait list video", f'<iframe src="https://www.youtube-nocookie.com/embed/{VID}">'
                                                           "</iframe>", 3))
    return src


def fake_transcribe(p, ref, meta):
    segs = [{"t": t, "text": f"step at {t}", "spoken": t != 30} for t in (0, 15, 30, 120, 290, 400)]
    return {"title": "Wait list", "prompt_version": kb.TRANSCRIPT_VERSION, "source": "gemini:test",
            "duration_s": 420, "language": "en", "segments": segs, "tokens": {"in": 1000, "out": 100}}


def summarize(brain, tmp_path, chapters):
    p = kb.Profile(brain, NAME)
    (batch,) = kb.video_todo(p, tmp_path / "v", 80_000)
    row = json.loads(batch.read_text())
    out = batch.with_name("batch-001.out.jsonl")
    out.write_text(json.dumps({**row, "summary": "Propose this video when someone asks how the wait list works.",
                               "chapters": chapters}) + "\n")
    return kb.video_apply(p, [out])


V104 = "knowledge/example-help/getting-started/basics/104-wait-list-video.md"
VFILE = f"knowledge/example-help/_videos/{VID}.md"


def test_video_transcript_summary_render_and_snapping(brain, tmp_path, monkeypatch):
    src = with_video(Source())
    refresh(brain, src)
    describe_all(brain, tmp_path)
    assert kb.main(["--brain", str(brain), "check", NAME]) == 1  # untranscribed
    monkeypatch.setattr(kb, "transcribe", fake_transcribe)
    monkeypatch.setattr(kb, "video_meta", lambda p, ref, state: {"title": "Wait list", "duration": 420})
    assert kb.video_transcribe(kb.Profile(brain, NAME), None, True, False) == 0
    assert "[00:15](https://www.youtube.com/watch?v=AbCdEfGhIjK&t=15s) step at 15" in (brain / VFILE).read_text()
    assert "*(on screen: step at 30)*" in (brain / VFILE).read_text()
    assert kb.main(["--brain", str(brain), "check", NAME]) == 1  # missing summary
    far = [{"t": 0, "title": "Intro"}, {"t": 60, "title": "Nowhere"}, {"t": 290, "title": "End"}]
    assert summarize(brain, tmp_path, far) == 1  # 60 s has no segment start within 10 s
    near = [{"t": 2, "title": "Intro"}, {"t": 118, "title": "Open [the] list"}, {"t": 295, "title": "Done"}]
    p = kb.Profile(brain, NAME)
    (batch,) = kb.video_todo(p, tmp_path / "cli", 80_000)
    out = batch.with_name("batch-001.out.jsonl")
    out.write_text(json.dumps({**json.loads(batch.read_text()), "summary": "Propose this video when someone asks "
                               "how the wait list works.", "chapters": near}) + "\n")
    assert kb.main(["--brain", str(brain), "video-apply", NAME, str(out)]) == 0  # CLI argument order
    store = json.loads(kb.Profile(brain, NAME).videos.read_text())
    assert [c["t"] for c in store[VID]["chapters"]] == [0, 120, 290]
    art = (brain / V104).read_text()
    assert "Video (07:00): Propose this video when" in art and "transcript niet" not in art
    assert f"- [02:00 Open (the) list](https://www.youtube.com/watch?v={VID}&t=120s)" in art
    assert f"[Transcript](../../_videos/{VID}.md)" in art
    assert kb.main(["--brain", str(brain), "check", NAME]) == 0
    before = tree(brain)
    refresh(brain, src)  # no-op, transcripts and summaries come from committed stores
    assert tree(brain) == before


def test_video_short_needs_no_chapters_long_needs_three(brain, tmp_path, monkeypatch):
    refresh(brain, with_video(Source()))
    monkeypatch.setattr(kb, "transcribe", fake_transcribe)
    monkeypatch.setattr(kb, "video_meta", lambda p, ref, state: {"title": "Wait list", "duration": 420})
    kb.video_transcribe(kb.Profile(brain, NAME), None, True, False)
    assert summarize(brain, tmp_path, []) == 1  # 420 s is long: 3-8 chapters


def test_deleted_video_is_pruned(brain, tmp_path, monkeypatch):
    src = with_video(Source())
    refresh(brain, src)
    monkeypatch.setattr(kb, "transcribe", fake_transcribe)
    monkeypatch.setattr(kb, "video_meta", lambda p, ref, state: {"title": "Wait list", "duration": 420})
    kb.video_transcribe(kb.Profile(brain, NAME), None, True, False)
    assert (brain / VFILE).exists()
    src.articles[-1]["body"] = "<p>Video removed.</p>"
    refresh(brain, src)
    assert not (brain / VFILE).exists()
    assert VID not in json.loads(kb.Profile(brain, NAME).manifest.read_text())["videos"]


def test_transcribe_needs_yes_for_all(brain, monkeypatch, capsys):
    refresh(brain, with_video(Source()))
    monkeypatch.setattr(kb, "transcribe", fake_transcribe)
    monkeypatch.setattr(kb, "video_meta", lambda p, ref, state: {"title": "Wait list", "duration": 420})
    assert kb.video_transcribe(kb.Profile(brain, NAME), None, False, False) == 0
    assert "--yes" in capsys.readouterr().out and not kb.Profile(brain, NAME).transcripts.exists()


def test_untimed_segments_join_the_previous_one():
    raw = [{"t": "0:00", "text": "a"}, {"t": "speech in t", "text": "screen", "spoken": False},
           *({"t": f"{m}:00", "text": "n"} for m in range(1, 9))]
    out = kb.segments_ok(raw, 600)
    assert out[0]["text"] == "a speech in t" and len(out) == 9
    with pytest.raises(kb.MirrorError, match="garbled"):
        kb.segments_ok([{"t": "0:00", "text": "a"}, *({"t": "words", "text": "x"} for _ in range(3))], 600)


def test_gemini_response_is_reused_not_repaid(brain, monkeypatch):
    p = kb.Profile(brain, NAME)
    calls = []
    resp = {"candidates": [{"content": {"parts": [{"text": json.dumps({"language": "en", "duration": "1:00",
                                                                         "segments": []})}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}
    monkeypatch.setattr(kb, "gemini_request", lambda p, body: calls.append(1) or resp)
    ref = {"id": VID, "watch": f"https://www.youtube.com/watch?v={VID}"}
    assert kb.gemini_transcript(p, ref)[1:] == ({"in": 10, "out": 2}, True)
    assert kb.gemini_transcript(p, ref)[1:] == ({"in": 10, "out": 2}, False) and len(calls) == 1


def test_segments_are_measurements():
    with pytest.raises(kb.MirrorError, match="beyond"):
        kb.segments_ok([{"t": "0:00", "text": "a"}, {"t": "15:00", "text": "b"}], 300)
    with pytest.raises(kb.MirrorError, match="garbled"):
        kb.segments_ok([{"t": 9 - i, "text": "x"} for i in range(5)], 10)
    segs = [{"t": "0:00", "text": "a"}, {"t": "1:05", "text": "b"}, {"t": "1:05", "text": "c"},
            {"t": "0:30", "text": "glitch"}, *({"t": f"{m}:00", "text": "n"} for m in range(2, 12)),
            {"t": "1:02:03", "text": "late"}]
    out = kb.segments_ok(segs, 3800)
    assert out[:3] == [{"t": 0, "text": "a", "spoken": True}, {"t": 65, "text": "b c", "spoken": True},
                       {"t": 120, "text": "n", "spoken": True}] and out[-1]["t"] == 3723


def test_manifest_write_failure_rolls_back_the_corpus(brain, monkeypatch):
    src = Source()
    refresh(brain, src)
    before = tree(brain)
    p = kb.Profile(brain, NAME)
    src.articles[0]["body"] = "<p>updated</p>"
    real = kb.jdump

    def fail_manifest(path, obj):
        if path == p.manifest:
            raise OSError("manifest write failed")
        return real(path, obj)

    monkeypatch.setattr(kb, "jdump", fail_manifest)
    with pytest.raises(OSError, match="manifest write failed"):
        refresh(brain, src)
    assert tree(brain) == before


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_limit_never_transcribes(brain, monkeypatch, limit):
    refresh(brain, with_video(Source()))
    calls = []
    monkeypatch.setattr(kb, "video_meta", lambda p, ref, state: {"title": "test", "duration": 420})
    monkeypatch.setattr(kb, "transcribe", lambda p, ref, meta: calls.append(ref["id"]))
    with pytest.raises(kb.MirrorError):
        kb.video_transcribe(kb.Profile(brain, NAME), limit, False, False)
    assert calls == []
