import json

import pytest

from beta_lag.cli import main


def test_demo_export_replay_and_report(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "run.duckdb")
    source = str(tmp_path / "demo.jsonl")
    monkeypatch.setattr("sys.argv", ["beta-lag", "demo", "--db", db, "--export", source])
    main()
    demo = json.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.argv", ["beta-lag", "replay", source, "--db", db])
    main()
    replay = json.loads(capsys.readouterr().out)
    assert demo["run_id"] != replay["run_id"]
    assert demo["mean_net_return_ex_funding"] == replay["mean_net_return_ex_funding"]
    monkeypatch.setattr("sys.argv", ["beta-lag", "report", "--db", db])
    main()
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_cli_invalid_input_prints_actual_line_error(tmp_path, monkeypatch, capsys):
    source = tmp_path / "bad.jsonl"
    source.write_text('{"kind":"unknown"}\n')
    monkeypatch.setattr(
        "sys.argv", ["beta-lag", "replay", str(source), "--db", str(tmp_path / "bad.duckdb")]
    )
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert f"{source}:1:" in capsys.readouterr().err
