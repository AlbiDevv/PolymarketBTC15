from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from research.crypto15m import normalize_ohlcv_rows


def _exchange_instance(exchange_id: str):
    import ccxt

    cls = getattr(ccxt, exchange_id)
    return cls({"enableRateLimit": True})


def _fetch_symbol_timeframe(exchange, symbol: str, timeframe: str, since_ms: int) -> list[list[float]]:
    all_rows: list[list[float]] = []
    cursor = since_ms
    limit = 1000
    iterations = 0
    while iterations < 5000:
        iterations += 1
        rows = exchange.fetch_ohlcv(symbol, timeframe, cursor, limit)
        if not rows:
            break
        all_rows.extend(rows)
        next_cursor = int(rows[-1][0]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if rows[-1][0] >= exchange.milliseconds() - 60_000:
            break
    return all_rows


def main():
    parser = argparse.ArgumentParser(description="Fetch BTC/ETH OHLCV candles through CCXT")
    parser.add_argument("--config", default=None)
    parser.add_argument("--skip-existing", action="store_true", help="Reuse existing candle parquet files instead of refreshing them")
    args = parser.parse_args()

    settings = load_settings(args.config)
    cfg = settings.crypto_data
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    since = datetime.now(timezone.utc) - timedelta(days=cfg.history_days)
    since_ms = int(since.timestamp() * 1000)
    exchanges = [cfg.exchange_primary, *cfg.exchange_fallbacks]
    written = []

    for symbol in cfg.symbols:
        for timeframe in cfg.timeframes:
            path = out_dir / f"symbol={symbol.replace('/', '-')}" / f"timeframe={timeframe}"
            file_path = path / "candles.parquet"
            if args.skip_existing and file_path.exists() and file_path.stat().st_size > 0:
                written.append(str(file_path))
                continue
            last_error: Exception | None = None
            for exchange_id in exchanges:
                exchange = _exchange_instance(exchange_id)
                try:
                    exchange.load_markets()
                    rows = _fetch_symbol_timeframe(exchange, symbol, timeframe, since_ms)
                    frame = normalize_ohlcv_rows(rows, exchange_id=exchange_id, symbol=symbol, timeframe=timeframe)
                    if frame.empty:
                        raise RuntimeError(f"no candles for {symbol} {timeframe}")
                    path.mkdir(parents=True, exist_ok=True)
                    frame.to_parquet(file_path, index=False)
                    written.append(str(file_path))
                    print(f"fetched {len(frame)} {symbol} {timeframe} from {exchange_id} -> {file_path}")
                    break
                except Exception as exc:  # pragma: no cover - network fallback
                    last_error = exc
                    print(f"fetch failed {exchange_id} {symbol} {timeframe}: {type(exc).__name__}: {str(exc)[:220]}")
                finally:
                    try:
                        exchange.close()
                    except Exception:
                        pass
            else:
                raise RuntimeError(f"failed to fetch {symbol} {timeframe}: {last_error}")

    print({"written": written, "history_days": cfg.history_days})


if __name__ == "__main__":
    main()
