import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cleanup_project import apply_cleanup, collect_cleanup_candidates


def test_cleanup_dry_run_protects_env_main_db_data_and_artifacts(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "prediction_trader.db").write_text("db", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("pyc", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "shadow_lab_smoke.db").write_text("smoke", encoding="utf-8")
    protected_cache = tmp_path / "data" / "crypto_ohlcv" / "__pycache__"
    protected_cache.mkdir(parents=True)
    (protected_cache / "x.pyc").write_text("pyc", encoding="utf-8")
    protected_artifact = tmp_path / "research" / "artifacts" / "model.pyc"
    protected_artifact.parent.mkdir(parents=True)
    protected_artifact.write_text("pyc", encoding="utf-8")

    candidates = collect_cleanup_candidates(tmp_path)
    rendered = "\n".join(candidates["delete"] + candidates["archive"])

    assert ".env" not in rendered
    assert "prediction_trader.db" not in rendered
    assert "data\\crypto_ohlcv" not in rendered and "data/crypto_ohlcv" not in rendered
    assert "research\\artifacts" not in rendered and "research/artifacts" not in rendered
    assert any("__pycache__" in item for item in candidates["delete"])
    assert any("shadow_lab_smoke.db" in item for item in candidates["archive"])


def test_cleanup_apply_archives_old_smoke_db_and_deletes_cache(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_text("pyc", encoding="utf-8")
    smoke = tmp_path / "shadow_lab_smoke.db"
    smoke.write_text("smoke", encoding="utf-8")

    candidates = collect_cleanup_candidates(tmp_path)
    result = apply_cleanup(candidates, tmp_path)

    assert not cache.exists()
    assert not smoke.exists()
    assert result["archived"]
    assert Path(result["archive_dir"][0]).exists()
