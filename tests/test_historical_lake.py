import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from historical.lake import append_manifest, read_dataset, write_partitioned_parquet


def test_partitioned_parquet_roundtrip(tmp_path):
    rows = [
        {"date": "2026-04-10", "market_id": "m1", "outcome": "YES"},
        {"date": "2026-04-11", "market_id": "m2", "outcome": "NO"},
    ]
    written = write_partitioned_parquet(tmp_path, "resolutions", rows)
    assert len(written) == 2
    frame = read_dataset(tmp_path, "resolutions")
    assert set(frame["market_id"]) == {"m1", "m2"}


def test_manifest_appends_entries(tmp_path):
    path = append_manifest(tmp_path, "gamma_sync", {"rows": 3})
    append_manifest(tmp_path, "gamma_sync", {"rows": 5})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert payload[-1]["rows"] == 5


def test_jsonl_read_preserves_large_token_ids_as_strings(tmp_path):
    token_id = "91899166844773021941847720953663572312257904642931905726627468139752635861052"
    target = tmp_path / "resolutions" / "date=2026-04-12"
    target.mkdir(parents=True)
    (target / "resolutions.jsonl").write_text(
        json.dumps({"date": "2026-04-12", "yes_token_id": token_id}) + "\n",
        encoding="utf-8",
    )

    frame = read_dataset(tmp_path, "resolutions")

    assert frame["yes_token_id"].iloc[0] == token_id
