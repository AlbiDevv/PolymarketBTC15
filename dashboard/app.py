from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from html import escape
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from config import Settings, load_settings
from lab import LabStatsService


def _verify_telegram_init_data(init_data: str, bot_token: str, *, max_age_sec: int = 86400) -> bool:
    if not init_data or not bot_token:
        return False

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return False

    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            age = datetime.now(timezone.utc).timestamp() - int(auth_date)
            if age > max_age_sec:
                return False
        except ValueError:
            return False

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(pairs.items(), key=lambda item: item[0])
    )
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated, received_hash)


def _authorize_request(request: Request, settings: Settings) -> None:
    init_data = request.headers.get("x-telegram-init-data") or request.query_params.get("initData") or ""
    if not init_data and settings.telegram.dev_initdata_bypass:
        return
    if not _verify_telegram_init_data(init_data, settings.alerts.telegram_bot_token):
        raise HTTPException(status_code=401, detail="Telegram auth failed")


def _dashboard_html(base_url: str, initial_portfolio: str, auto_refresh_sec: int) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Shadow Lab</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #f5efe6;
      --bg2: #ece2d3;
      --ink: #13242d;
      --muted: #5b6c74;
      --line: rgba(19, 36, 45, 0.12);
      --panel: rgba(255,255,255,0.78);
      --accent: #c05c22;
      --good: #157347;
      --bad: #bb3e2f;
      --warn: #9d6a00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(192,92,34,0.18), transparent 34%),
        linear-gradient(135deg, var(--bg) 0%, #faf7f0 45%, var(--bg2) 100%);
      min-height: 100vh;
    }}
    .shell {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 28px 18px 56px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 20px;
      box-shadow: 0 20px 50px rgba(19, 36, 45, 0.08);
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .eyebrow {{
      font-size: 12px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    h1, h2, h3 {{
      margin: 0 0 10px;
    }}
    h1 {{
      font-size: clamp(34px, 6vw, 64px);
      line-height: 0.95;
      max-width: 9ch;
    }}
    h2 {{
      font-size: 26px;
    }}
    h3 {{
      font-size: 18px;
    }}
    .sub {{
      color: var(--muted);
      max-width: 56ch;
      line-height: 1.55;
    }}
    .cards, .track-cards, .mini-grid {{
      display: grid;
      gap: 12px;
    }}
    .cards {{
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      margin-top: 18px;
    }}
    .track-cards {{
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      margin-top: 12px;
    }}
    .mini-grid {{
      grid-template-columns: repeat(2, 1fr);
      margin-top: 16px;
    }}
    .metric, .track-card {{
      border-radius: 18px;
      border: 1px solid rgba(19, 36, 45, 0.08);
      background: rgba(255,255,255,0.8);
      padding: 14px;
    }}
    .metric strong, .track-card strong {{
      display: block;
      font-size: 30px;
      margin-top: 6px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 18px;
      margin-top: 18px;
    }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 18px;
      margin-top: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid rgba(19, 36, 45, 0.08);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
    }}
    .list {{
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .list li {{
      border: 1px solid rgba(19, 36, 45, 0.08);
      border-radius: 16px;
      background: rgba(255,255,255,0.78);
      padding: 12px 14px;
    }}
    .pill {{
      display: inline-block;
      padding: 4px 9px;
      border-radius: 999px;
      background: rgba(192,92,34,0.12);
      color: var(--accent);
      font-size: 12px;
      letter-spacing: 0.04em;
      margin-right: 6px;
      margin-bottom: 6px;
    }}
    .good {{ color: var(--good); }}
    .bad {{ color: var(--bad); }}
    .warn {{ color: var(--warn); }}
    .muted {{ color: var(--muted); }}
    .mono {{ font-family: "Courier New", monospace; }}
    select {{
      appearance: none;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.92);
      padding: 10px 14px;
      color: var(--ink);
      font: inherit;
    }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    @media (max-width: 980px) {{
      .hero, .grid, .grid-3, .mini-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="panel">
        <div class="eyebrow">Polymarket Shadow Lab</div>
        <h1>Русский мониторинг shadow lab</h1>
        <p class="sub">
          Live market feed, локальное maker-first исполнение, контрольный трек и late-stage эксперимент в одном read-only дашборде.
        </p>
        <div class="cards" id="aggregateCards"></div>
      </div>
      <div class="panel">
        <div class="eyebrow">Runtime</div>
        <h2>Состояние рантайма</h2>
        <ul class="list" id="runtimeList"></ul>
        <div class="track-cards" id="trackCards"></div>
      </div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <div>
          <div class="eyebrow">Портфели</div>
          <h2>Турнир стратегий</h2>
        </div>
        <div>
          <select id="portfolioSelect"></select>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Портфель</th>
            <th>Трек</th>
            <th>Pack</th>
            <th>Equity</th>
            <th>Realized</th>
            <th>Fill rate</th>
            <th>Hit rate</th>
            <th>Activity 24h</th>
            <th>Кандидаты / reject 24ч</th>
            <th>Gate</th>
          </tr>
        </thead>
        <tbody id="portfolioBody"></tbody>
      </table>
    </section>

    <section class="grid">
      <div class="panel">
        <div class="eyebrow">Кривая</div>
        <h2 id="portfolioTitle">Портфель</h2>
        <canvas id="equityChart" height="170"></canvas>
      </div>
      <div class="panel">
        <div class="eyebrow">Просадка</div>
        <h2>Давление на equity</h2>
        <canvas id="drawdownChart" height="170"></canvas>
      </div>
    </section>

    <section class="grid-3">
      <div class="panel">
        <div class="eyebrow">Сводка</div>
        <ul class="list" id="summaryList"></ul>
      </div>
      <div class="panel">
        <div class="eyebrow">Открытые позиции</div>
        <ul class="list" id="positionsList"></ul>
      </div>
      <div class="panel">
        <div class="eyebrow">Последние fill / close</div>
        <ul class="list" id="fillsList"></ul>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <div class="eyebrow">Отклонённые сделки</div>
        <h2>Почему бот не вошёл</h2>
        <ul class="list" id="rejectionsList"></ul>
      </div>
      <div class="panel">
        <div class="eyebrow">Late-stage кандидаты</div>
        <h2>Актуальные сигналы / допуск</h2>
        <ul class="list" id="candidatesList"></ul>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <div class="eyebrow">Сравнение</div>
        <h2>PnL по портфелям</h2>
        <canvas id="pnlChart" height="170"></canvas>
      </div>
      <div class="panel">
        <div class="eyebrow">Исполнение</div>
        <h2>Fill rate и forced exits</h2>
        <canvas id="fillChart" height="170"></canvas>
      </div>
    </section>

    <section class="panel" style="margin-top:18px">
      <div class="eyebrow">Дневные срезы</div>
      <h2>Daily summary</h2>
      <table>
        <thead>
          <tr>
            <th>Дата</th>
            <th>Портфель</th>
            <th>Сделки</th>
            <th>Hit rate</th>
            <th>Realized</th>
          </tr>
        </thead>
        <tbody id="dailyBody"></tbody>
      </table>
    </section>
  </div>

  <script>
    const baseUrl = {json.dumps(base_url.rstrip("/"))};
    const autoRefreshSec = {int(auto_refresh_sec)};
    let equityChart;
    let drawdownChart;
    let pnlChart;
    let fillChart;
    let selectedPortfolio = {json.dumps(initial_portfolio)};

    function money(value) {{
      return new Intl.NumberFormat('en-US', {{ style: 'currency', currency: 'USD' }}).format(value || 0);
    }}

    function pct(value) {{
      return `${{((value || 0) * 100).toFixed(2)}}%`;
    }}

    function safe(text) {{
      return (text || '').replace(/[&<>"]/g, (ch) => ({{ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }})[ch]);
    }}

    async function getJson(path) {{
      const response = await fetch(`${{baseUrl}}${{path}}`, {{ credentials: 'same-origin' }});
      if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
      return response.json();
    }}

    function renderRuntime(runtime, abGroups) {{
      document.getElementById('runtimeList').innerHTML = [
        `Режим: <strong>${{safe(runtime.mode || 'shadow_maker')}}</strong>`,
        `WebSocket: <strong class="${{runtime.ws_connected ? 'good' : 'bad'}}">${{runtime.ws_connected ? 'подключен' : 'нет'}}</strong>`,
        `Последний цикл: <span class="mono">${{safe(runtime.last_cycle_ts || 'n/a')}}</span>`,
        `Последняя ошибка: <span class="${{runtime.last_cycle_error ? 'bad' : 'muted'}}">${{safe(runtime.last_cycle_error || 'нет')}}</span>`,
        `Рынков в shortlist: <strong>${{runtime.eligible_markets_last || 0}}</strong>`,
        `Подписок WS: <strong>${{runtime.subscribed_tokens_last || 0}}</strong>`,
      ].map(item => `<li>${{item}}</li>`).join('');

      const trackNames = {{
        control: 'Control cohort',
        learned: 'Learned cohort',
      }};
      document.getElementById('trackCards').innerHTML = Object.entries(abGroups || {{}}).map(([key, value]) => `
        <div class="track-card">
          <div class="eyebrow">${{safe(trackNames[key] || key)}}</div>
          <strong>${{money(value.equity)}}</strong>
          <div class="${{value.realized_pnl >= 0 ? 'good' : 'bad'}}">Realized ${{money(value.realized_pnl)}}</div>
          <div class="muted">Открыто: ${{value.open_positions}} | Закрыто: ${{value.closed_trades}}</div>
        </div>
      `).join('');
    }}

    function renderOverview(data) {{
      const aggregate = data.aggregate || {{}};
      document.getElementById('aggregateCards').innerHTML = [
        ['Combined Equity', money(aggregate.equity)],
        ['Realized PnL', money(aggregate.realized_pnl)],
        ['Open Positions', aggregate.open_positions || 0],
        ['Closed Trades', aggregate.closed_trades || 0],
        ['Candidates 24h', aggregate.candidates_24h || 0],
        ['Rejects 24h', aggregate.rejections_24h || 0],
        ['Training Verdict', (data.verdict || {{}}).status || 'n/a'],
      ].map(([label, value]) => `<div class="metric"><div>${{label}}</div><strong>${{value}}</strong></div>`).join('');

      renderRuntime(data.runtime || {{}}, data.ab_groups || {{}});

      const selector = document.getElementById('portfolioSelect');
      const currentOptions = (data.portfolios || []).map((item) => item.key);
      selector.innerHTML = currentOptions.map((key) => `<option value="${{safe(key)}}">${{safe(key)}}</option>`).join('');
      if (!currentOptions.includes(selectedPortfolio)) {{
        selectedPortfolio = data.winner_key || currentOptions[0] || '';
      }}
      selector.value = selectedPortfolio;
      selector.onchange = (event) => {{
        selectedPortfolio = event.target.value;
        refreshPortfolio();
      }};

      document.getElementById('portfolioBody').innerHTML = (data.portfolios || []).map((item) => `
        <tr>
          <td><strong>${{safe(item.key)}}</strong></td>
          <td>${{safe(`${{item.track}} / ${{item.ab_group || 'single'}}`)}}</td>
          <td>${{safe(item.pack)}}</td>
          <td>${{money(item.equity)}}</td>
          <td class="${{item.realized_pnl >= 0 ? 'good' : 'bad'}}">${{money(item.realized_pnl)}}</td>
          <td>${{pct(item.fill_rate)}}</td>
          <td>${{pct(item.hit_rate)}}</td>
          <td>${{item.markets_seen_24h || 0}} seen / ${{item.signals_24h || 0}} sig / ${{item.accepted_count_24h || 0}} acc / ${{item.fills_count || 0}} fills</td>
          <td>${{item.candidate_count_24h}} / ${{item.reject_count_24h}}</td>
          <td>${{item.acceptance?.eligible ? '<span class="pill">eligible</span>' : '<span class="pill">watch</span>'}}</td>
        </tr>
      `).join('');

      document.getElementById('dailyBody').innerHTML = (data.daily || []).slice(0, 60).map((item) => `
        <tr>
          <td class="mono">${{safe(item.date)}}</td>
          <td>${{safe(item.portfolio_key)}}</td>
          <td>${{item.trades}}</td>
          <td>${{pct(item.hit_rate)}}</td>
          <td class="${{item.realized_pnl >= 0 ? 'good' : 'bad'}}">${{money(item.realized_pnl)}}</td>
        </tr>
      `).join('');

      if (pnlChart) pnlChart.destroy();
      pnlChart = new Chart(document.getElementById('pnlChart'), {{
        type: 'bar',
        data: {{
          labels: (data.portfolios || []).map((item) => item.key),
          datasets: [{{
            label: 'Realized PnL',
            data: (data.portfolios || []).map((item) => item.realized_pnl),
            backgroundColor: (data.portfolios || []).map((item) => item.realized_pnl >= 0 ? '#157347' : '#bb3e2f'),
          }}],
        }},
        options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }},
      }});

      if (fillChart) fillChart.destroy();
      fillChart = new Chart(document.getElementById('fillChart'), {{
        type: 'bar',
        data: {{
          labels: (data.portfolios || []).map((item) => item.key),
          datasets: [
            {{
              label: 'Fill rate %',
              data: (data.portfolios || []).map((item) => +(item.fill_rate * 100).toFixed(2)),
              backgroundColor: '#c05c22',
            }},
            {{
              label: 'Forced exits',
              data: (data.portfolios || []).map((item) => item.forced_exit_count),
              backgroundColor: '#9d6a00',
            }},
          ],
        }},
        options: {{ responsive: true }},
      }});
    }}

    function renderPortfolio(detail) {{
      const summary = detail.summary || {{}};
      document.getElementById('portfolioTitle').textContent = summary.key || 'Портфель';
      document.getElementById('summaryList').innerHTML = [
        `Трек: <strong>${{safe(summary.track || '')}}</strong>`,
        `Pack: <strong>${{safe(summary.pack || '')}}</strong>`,
        `Hypotheses: <strong>${{safe((summary.hypotheses || []).join(', ') || 'n/a')}}</strong>`,
        `Equity: <strong>${{money(summary.equity)}}</strong>`,
        `Realized: <strong class="${{summary.realized_pnl >= 0 ? 'good' : 'bad'}}">${{money(summary.realized_pnl)}}</strong>`,
        `Unrealized: <strong class="${{summary.unrealized_pnl >= 0 ? 'good' : 'bad'}}">${{money(summary.unrealized_pnl)}}</strong>`,
        `Fill rate: <strong>${{pct(summary.fill_rate)}}</strong>`,
        `Hit rate: <strong>${{pct(summary.hit_rate)}}</strong>`,
        `Expectancy: <strong>${{money(summary.expectancy_per_trade)}}</strong>`,
        `Avg hold: <strong>${{(summary.avg_hold_hours || 0).toFixed(1)}}h</strong>`,
        `Forced exits: <strong>${{summary.forced_exit_count || 0}}</strong>`,
      ].map((line) => `<li>${{line}}</li>`).join('');

      document.getElementById('positionsList').innerHTML = (detail.open_positions || []).length
        ? detail.open_positions.slice(0, 12).map((item) => `
            <li>
              <strong>${{safe(item.side)}} ${{money(item.entry_price * item.size)}}</strong><br>
              <span class="muted">${{safe(item.category || 'uncategorized')}}</span><br>
              <span>${{safe(item.question || '')}}</span><br>
              <span class="${{item.unrealized_pnl >= 0 ? 'good' : 'bad'}}">${{money(item.unrealized_pnl)}}</span>
            </li>
          `).join('')
        : '<li>Нет открытых позиций.</li>';

      document.getElementById('fillsList').innerHTML = (detail.recent_fills || []).length
        ? detail.recent_fills.slice(0, 12).map((item) => `
            <li>
              <strong>${{safe(item.action)}} ${{safe(item.side)}}</strong> @ ${{(item.price || 0).toFixed(4)}}<br>
              <span class="mono">${{safe((item.timestamp || '').slice(0, 19))}}</span><br>
              <span>${{safe(item.fill_type || '')}}</span>
              ${{item.order_kind ? `<span class="pill">${{safe(item.order_kind)}}</span>` : ''}}
            </li>
          `).join('')
        : '<li>Пока нет fill-событий.</li>';

      if (equityChart) equityChart.destroy();
      equityChart = new Chart(document.getElementById('equityChart'), {{
        type: 'line',
        data: {{
          labels: (detail.equity_curve || []).map((point) => (point.timestamp || '').slice(5, 19).replace('T', ' ')),
          datasets: [
            {{ label: 'Equity', data: (detail.equity_curve || []).map((point) => point.equity), borderColor: '#13242d', tension: 0.2 }},
            {{ label: 'Bankroll', data: (detail.equity_curve || []).map((point) => point.bankroll), borderColor: '#157347', tension: 0.2 }},
            {{ label: 'Realized', data: (detail.equity_curve || []).map((point) => point.realized_pnl + (summary.initial_bankroll || 0)), borderColor: '#c05c22', tension: 0.2 }},
          ],
        }},
        options: {{ responsive: true }},
      }});

      if (drawdownChart) drawdownChart.destroy();
      drawdownChart = new Chart(document.getElementById('drawdownChart'), {{
        type: 'line',
        data: {{
          labels: (detail.equity_curve || []).map((point) => (point.timestamp || '').slice(5, 19).replace('T', ' ')),
          datasets: [
            {{
              label: 'Drawdown %',
              data: (detail.equity_curve || []).map((point) => +((point.drawdown_pct || 0) * 100).toFixed(2)),
              borderColor: '#bb3e2f',
              backgroundColor: 'rgba(187,62,47,0.12)',
              fill: true,
              tension: 0.2,
            }},
          ],
        }},
        options: {{ responsive: true }},
      }});
    }}

    function renderRejections(rows) {{
      document.getElementById('rejectionsList').innerHTML = rows.length
        ? rows.map((row) => `
            <li>
              <strong>${{safe(row.portfolio_key || row.track || 'rejected')}}</strong>
              <span class="mono">${{safe((row.timestamp || '').slice(5, 19))}}</span><br>
              <span>${{safe(row.question || '')}}</span><br>
              <span class="warn">${{safe((row.reasons || []).join(', ') || 'n/a')}}</span><br>
              <span class="muted">score=${{(row.quality_score || 0).toFixed(1)}} net_edge=${{(row.expected_net_edge || 0).toFixed(4)}}</span>
            </li>
          `).join('')
        : '<li>Нет свежих reject-записей.</li>';
    }}

    function renderCandidates(rows) {{
      document.getElementById('candidatesList').innerHTML = rows.length
        ? rows.map((row) => `
            <li>
              <strong>${{safe(row.portfolio_key || row.track || '')}}</strong>
              <span class="pill">${{safe(row.decision || '')}}</span>
              <span class="mono">${{safe((row.timestamp || '').slice(5, 19))}}</span><br>
              <span>${{safe(row.question || '')}}</span><br>
              <span>side=${{safe(row.side || '')}} hypothesis=${{safe(row.hypothesis || '')}} edge=${{(row.edge || 0).toFixed(4)}} score=${{(row.quality_score || 0).toFixed(1)}}</span>
              ${{(row.reasons || []).length ? `<br><span class="muted">${{safe(row.reasons.join(', '))}}</span>` : ''}}
            </li>
          `).join('')
        : '<li>Нет свежих late-stage candidate записей.</li>';
    }}

    async function refreshOverview() {{
      const data = await getJson('/api/overview');
      renderOverview(data);
    }}

    async function refreshPortfolio() {{
      if (!selectedPortfolio) return;
      const detail = await getJson(`/api/portfolio/${{encodeURIComponent(selectedPortfolio)}}`);
      renderPortfolio(detail);
    }}

    async function refreshRejections() {{
      const data = await getJson('/api/rejections');
      renderRejections(data.items || []);
    }}

    async function refreshCandidates() {{
      const data = await getJson('/api/candidates');
      renderCandidates(data.items || []);
    }}

    async function boot() {{
      await refreshOverview();
      await refreshPortfolio();
      await refreshRejections();
      await refreshCandidates();

      setInterval(() => refreshOverview().catch(console.error), autoRefreshSec * 1000);
      setInterval(() => refreshPortfolio().catch(console.error), autoRefreshSec * 1000);
      setInterval(() => refreshRejections().catch(console.error), Math.max(15, autoRefreshSec * 3) * 1000);
      setInterval(() => refreshCandidates().catch(console.error), autoRefreshSec * 1000);
    }}

    boot().catch((error) => {{
      document.body.innerHTML = `<div style="padding:24px;font-family:Georgia,serif">Dashboard load failed: ${{safe(String(error))}}</div>`;
    }});
  </script>
</body>
</html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Prediction Trader Shadow Lab Dashboard")
    stats = LabStatsService(settings.database.url, settings.bankroll.initial, mode="shadow_maker")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        _authorize_request(request, settings)
        overview = stats.overview()
        initial = overview.get("winner_key") or (overview.get("portfolios") or [{}])[0].get("key", "")
        return HTMLResponse(_dashboard_html(settings.dashboard.base_url, initial, settings.dashboard.auto_refresh_sec))

    @app.get("/api/overview")
    async def api_overview(request: Request):
        _authorize_request(request, settings)
        return stats.overview()

    @app.get("/api/portfolio/{key}")
    async def api_portfolio(key: str, request: Request):
        _authorize_request(request, settings)
        try:
            return stats.portfolio_detail(key)
        except KeyError:
            raise HTTPException(status_code=404, detail="Portfolio not found")

    @app.get("/api/daily")
    async def api_daily(request: Request, portfolio: str | None = None):
        _authorize_request(request, settings)
        return {"daily": stats.daily_summaries(portfolio)}

    @app.get("/api/rejections")
    async def api_rejections(request: Request, limit: int = 20):
        _authorize_request(request, settings)
        return {"items": stats.rejections(limit)}

    @app.get("/api/candidates")
    async def api_candidates(request: Request, limit: int = 20):
        _authorize_request(request, settings)
        return {"items": stats.candidates(limit)}

    @app.get("/api/motifs")
    async def api_motifs(request: Request, limit: int = 10):
        _authorize_request(request, settings)
        return {"items": stats.motifs(limit)}

    @app.get("/api/learning")
    async def api_learning(request: Request):
        _authorize_request(request, settings)
        return {"artifact": stats.latest_learning_artifact()}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return PlainTextResponse(stats.prometheus_metrics(), media_type="text/plain; version=0.0.4")

    return app


def run_dashboard(settings: Settings | None = None):
    import uvicorn

    settings = settings or load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.dashboard.host,
        port=settings.dashboard.port,
        log_level="info",
    )


app = create_app()


if __name__ == "__main__":
    run_dashboard()
