import json

from phone_events import main


def test_selects_same_evidence_from_old_and_new_dumps(tmp_path, capsys):
    events = [
        {"t": 400, "kind": "transcript", "text": "bonjour"},
        {"t": 10, "kind": "model", "seq": 1, "status": "started"},
        {"t": 500, "kind": "model", "seq": 1, "delegation": "d", "exec_ms": 490,
         "result": {"tool_calls": [{"arguments": "bounded"}]}, "future": True},
    ]
    for suffix in (".json", ".jsonl"):
        path = tmp_path / ("phone" + suffix)
        if suffix == ".json":
            path.write_text(json.dumps({"events": events}))
        else:
            path.write_text("\n".join(json.dumps(row) for row in [
                {"type": "phone_run", "run": {"run_id": "r"}},
                *({"type": "event", "index": i, "event": e} for i, e in enumerate(events)),
            ]))
        assert main([str(path), "--kind", "model", "--delegation", "d", "--min-exec-ms", "400",
                     "--after-ms", "100", "--before-ms", "600"]) == 0
        assert json.loads(capsys.readouterr().out) == {"type": "event", "index": 2, "event": events[2]}


def test_invalid_dump_fails(tmp_path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"truncated"')
    assert main([str(path)]) == 1
    assert "phone_events:" in capsys.readouterr().err
