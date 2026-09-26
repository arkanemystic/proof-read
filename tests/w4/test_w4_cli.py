import json

from proofread.cli import main
from proofread.genome.schema import DEFAULT_GENOME_PATH


def test_cli_stub_episode(tmp_path, capsys):
    rc = main(["run-episode", "--task", "stub/original/1", "--genome", str(DEFAULT_GENOME_PATH), "--mode", "enforce",
               "--model", "stub-model", "--stub", "--pristine", "--trace-dir", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["passed_workspace"] and out["passed_pristine"] and out["turns"] == 2
    assert out["mode"] == "enforce" and (tmp_path / f"{out['episode_id']}.jsonl").exists()
