# Shadow Lab Deploy

This stage runs `shadow_maker` only:

- live Polymarket market data
- local maker-first execution simulation
- no real-money orders
- dashboard on `127.0.0.1:8090`
- Telegram mini app disabled until a public HTTPS URL exists

## Required Variables

`TELEGRAM_CHAT_ID` is the default outbound destination for alerts and daily summaries.  
`TELEGRAM_ADMIN_CHAT_ID` / `TELEGRAM_ADMIN_CHAT_IDS` are the inbound allowed admins.  
If admin IDs are configured, the Telegram bot is admins-only and ignores everyone else.

Use this `.env` template:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_ADMIN_CHAT_IDS=

DASHBOARD_BASE_URL=http://127.0.0.1:8090
TELEGRAM_WEBAPP_URL=

POLYGON_PRIVATE_KEY=
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
```

Notes:

- `POLYGON_PRIVATE_KEY` and `POLYMARKET_API_*` stay empty for `shadow_maker`
- `TELEGRAM_WEBAPP_URL` stays empty until you have a public HTTPS URL
- with no public URL, open the dashboard through SSH tunnel from your laptop

## Ubuntu Install

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip git

mkdir -p ~/apps
```

Upload from Windows:

```powershell
scp -r "C:\path\to\prediction_trader" trader@SERVER_IP:~/apps/prediction_trader
```

Install on VPS:

```bash
cd ~/apps/prediction_trader
python3.12 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
chmod 600 .env
python -m py_compile main.py
pytest -q
```

## Manual Start

Shadow lab:

```bash
cd ~/apps/prediction_trader
source venv/bin/activate
python main.py --mode shadow_maker
```

Dashboard:

```bash
cd ~/apps/prediction_trader
source venv/bin/activate
python main.py --dashboard-only
```

Telegram analytics:

```bash
cd ~/apps/prediction_trader
source venv/bin/activate
python -m monitor.telegram_analytics --config config/settings.yaml
```

## Dashboard Without Public URL

Keep dashboard bound locally on the VPS. From your laptop:

```bash
ssh -L 8090:127.0.0.1:8090 trader@SERVER_IP
```

Then open:

```text
http://127.0.0.1:8090/dashboard
```

Telegram mini app does not work yet in this phase because Telegram requires a public HTTPS URL.

## systemd

Templates are included in `deploy/systemd/`.

Copy them on Ubuntu:

```bash
sudo cp deploy/systemd/prediction-shadow-lab.service /etc/systemd/system/
sudo cp deploy/systemd/prediction-dashboard.service /etc/systemd/system/
sudo cp deploy/systemd/prediction-telegram.service /etc/systemd/system/
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable prediction-shadow-lab prediction-dashboard prediction-telegram
sudo systemctl start prediction-shadow-lab prediction-dashboard prediction-telegram
```

Check:

```bash
sudo systemctl status prediction-shadow-lab
sudo systemctl status prediction-dashboard
sudo systemctl status prediction-telegram

sudo journalctl -u prediction-shadow-lab -f
sudo journalctl -u prediction-dashboard -f
sudo journalctl -u prediction-telegram -f
```

## First Verification

```bash
cd ~/apps/prediction_trader
source venv/bin/activate

python main.py --mode shadow_maker
python main.py --dashboard-only
python -m monitor.telegram_analytics --config config/settings.yaml
```

What you should see:

- shadow lab refreshes markets and opens the market WebSocket
- dashboard answers on `127.0.0.1:8090`
- Telegram analytics starts only if `TELEGRAM_BOT_TOKEN` is set
- with admin IDs configured, only admins can use commands
