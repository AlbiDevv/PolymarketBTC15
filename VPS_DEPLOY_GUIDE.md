# VPS Deployment Guide — Prediction Markets Trader

## 1. ТРЕБОВАНИЯ К VPS

### Минимальные (MVP, dry_run / paper)

| Параметр | Значение | Почему |
|----------|----------|--------|
| CPU | 1 vCPU | Цикл раз в 60 сек, нагрузка минимальная |
| RAM | 1 GB | Python + SQLite + httpx, пик ~300 MB |
| Диск | 10 GB SSD | БД растёт ~50 MB/мес, логи ~100 MB/мес |
| ОС | Ubuntu 22.04 / 24.04 LTS | Стабильная, хорошая поддержка Python |
| Сеть | 1 TB трафика | API-запросы ~1-5 GB/мес |
| Расположение | EU (Франкфурт/Амстердам) | Минимальный latency до Polymarket API |

### Рекомендуемые (live, масштабирование)

| Параметр | Значение | Почему |
|----------|----------|--------|
| CPU | 2 vCPU | Параллельный бэктест + торговля |
| RAM | 2 GB | Pandas/numpy для аналитики |
| Диск | 20 GB SSD NVMe | Быстрый I/O для БД |
| ОС | Ubuntu 24.04 LTS | |
| Бэкапы | Включены | Автоматические снапшоты |

### Провайдеры и цены

| Провайдер | Тариф | Цена | CPU/RAM/Диск |
|-----------|-------|------|--------------|
| **Hetzner** CPX11 | Shared | **€4.51/мес** | 2 vCPU / 2 GB / 40 GB |
| **Hetzner** CX22 | Shared | €5.39/мес | 2 vCPU / 4 GB / 40 GB |
| DigitalOcean Basic | Regular | $6/мес | 1 vCPU / 1 GB / 25 GB |
| Vultr Cloud | Regular | $6/мес | 1 vCPU / 1 GB / 25 GB |
| AWS Lightsail | Fixed | $5/мес | 1 vCPU / 1 GB / 40 GB |
| Oracle Cloud | Free Tier | **$0** | 1 vCPU / 1 GB / 50 GB |

**Рекомендация: Hetzner CPX11** — лучшее соотношение цена/ресурсы для Европы. Если бюджет $0 — Oracle Cloud Free Tier (но менее стабильный).

---

## 2. КАКИЕ ФАЙЛЫ МОЖНО УДАЛИТЬ

### Точно удалить (не нужны на VPS):

```
# Документация для разработки (не production)
IMPLEMENTATION_ROADMAP.md      — дорожная карта, для справки
TZ_improvements.md             — анализ ТЗ, для справки
TZ_prediction_markets_v2.docx  — старое ТЗ
TZ_prediction_markets_v3.docx  — ТЗ
VPS_DEPLOY_GUIDE.md            — этот длинный гайд (после прочтения; краткая выжимка — VPS_DEPLOY.md)

# Кэши и временные файлы
__pycache__/                   — Python кэш (пересоздаётся)
.pytest_cache/                 — pytest кэш
*.pyc                          — скомпилированные файлы

# IDE/редактор
.idea/
.vscode/
.cursor/
```

### Можно удалить на production VPS (но оставить в репозитории):

```
tests/                         — тесты не нужны на боевом сервере
backtest/                      — бэктест запускается отдельно, на рабочей машине
.env.example                   — шаблон, на VPS уже есть рабочий .env
```

### НЕЛЬЗЯ удалять:

```
exchange_client/               — API клиент (ядро)
wallet_manager/                — кошелёк (ядро)
models/                        — EV, Kelly, гипотезы (ядро)
risk/                          — риск-менеджмент (ядро)
runner/                        — оркестратор (ядро)
monitor/                       — алерты (ядро)
news_feed/                     — фид для H1 (ядро)
db/                            — база данных (ядро)
config/                        — конфигурация (ядро)
main.py                        — точка входа
requirements.txt               — зависимости
VPS_DEPLOY.md                  — краткий чеклист deploy (dry/paper), актуальные env и процессы
.env                           — секреты (ТОЛЬКО на VPS, не в git!)
.gitignore                     — для git
```

### Минимальный набор для production VPS:

```
prediction_trader/
├── config/
├── db/
├── exchange_client/
├── models/
├── monitor/
├── news_feed/
├── risk/
├── runner/
├── wallet_manager/
├── __init__.py
├── main.py
├── requirements.txt
├── .env              ← создаётся вручную на VPS
└── logs/             ← создаётся автоматически
```

---

## 3. ПОШАГОВАЯ УСТАНОВКА НА VPS

### 3.1. Заказ VPS (Hetzner пример)

1. Зарегистрироваться на https://www.hetzner.com/cloud
2. Создать проект → Add Server
3. Параметры:
   - Location: **Falkenstein** или **Helsinki** (EU)
   - Image: **Ubuntu 24.04**
   - Type: **CPX11** (€4.51/мес)
   - Networking: оставить по умолчанию
   - SSH Keys: **ОБЯЗАТЕЛЬНО добавить свой SSH-ключ** (см. §4)
   - Name: `prediction-trader`
4. Записать IP-адрес сервера

### 3.2. Генерация SSH-ключа (на своём компьютере)

```bash
# Windows (PowerShell)
ssh-keygen -t ed25519 -C "trader-vps"
# Нажать Enter 3 раза (пароль на ключ — по желанию)
# Ключ сохранится в C:\Users\ВАШ_ЮЗЕР\.ssh\id_ed25519

# Показать публичный ключ для добавления в Hetzner
cat ~/.ssh/id_ed25519.pub
```

### 3.3. Первое подключение

```bash
# Подключение по SSH
ssh root@ВАШ_IP

# Если Windows и нет ssh — установить OpenSSH:
# Settings → Apps → Optional features → OpenSSH Client
```

### 3.4. Настройка сервера (выполнить по SSH)

```bash
#!/bin/bash
# === ВЫПОЛНИТЬ ПОСЛЕ ПЕРВОГО ПОДКЛЮЧЕНИЯ ===

# 1. Обновить систему
apt update && apt upgrade -y

# 2. Создать пользователя (НЕ работать под root)
adduser trader
usermod -aG sudo trader

# 3. Настроить SSH для нового пользователя
mkdir -p /home/trader/.ssh
cp /root/.ssh/authorized_keys /home/trader/.ssh/
chown -R trader:trader /home/trader/.ssh
chmod 700 /home/trader/.ssh
chmod 600 /home/trader/.ssh/authorized_keys

# 4. Установить Python 3.12 и pip
apt install -y python3.12 python3.12-venv python3-pip git ufw fail2ban

# 5. Настроить файрвол
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw --force enable

# 6. Установить unattended-upgrades (автоматические патчи безопасности)
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

### 3.5. Усиление SSH (ВАЖНО для безопасности)

```bash
# Редактировать конфиг SSH (под root)
nano /etc/ssh/sshd_config
```

Изменить следующие строки:

```
# Запретить вход по паролю (только по ключу)
PasswordAuthentication no

# Запретить вход под root
PermitRootLogin no

# Сменить порт SSH (опционально, но полезно)
Port 2222

# Отключить пустые пароли
PermitEmptyPasswords no

# Ограничить пользователей
AllowUsers trader
```

```bash
# Применить изменения
systemctl restart sshd

# Если сменили порт — обновить файрвол
ufw allow 2222/tcp
ufw delete allow 22/tcp

# Проверить вход (В ДРУГОМ ТЕРМИНАЛЕ! Не закрывать текущую сессию)
ssh -p 2222 trader@ВАШ_IP
```

### 3.6. Установка проекта

```bash
# Подключиться как trader
ssh -p 2222 trader@ВАШ_IP

# Создать директорию
mkdir -p ~/apps
cd ~/apps

# Клонировать репозиторий (если есть git remote)
git clone ВАШ_РЕПО prediction_trader
# ИЛИ загрузить файлы через scp (см. §3.7)

cd prediction_trader

# Создать виртуальное окружение
python3.12 -m venv venv
source venv/bin/activate

# Установить зависимости
pip install --upgrade pip
pip install -r requirements.txt

# Создать .env с секретами
nano .env
```

Содержимое `.env` (для **dry_run / paper** достаточно пустых или отсутствующих ключей биржи, если `config` не требует live; для **live** — полный набор):

```
# Опционально: алерты + read-only Telegram-бот (/status, /pnl, /trades, …)
TELEGRAM_BOT_TOKEN=токен_бота
TELEGRAM_CHAT_ID=ваш_chat_id

# Только для live / on-chain — не обязательны на этапе paper
POLYGON_PRIVATE_KEY=0x_ваш_приватный_ключ
POLYMARKET_API_KEY=ваш_ключ
POLYMARKET_API_SECRET=ваш_секрет
POLYMARKET_API_PASSPHRASE=ваш_пароль
```

```bash
# Права на .env (только владелец)
chmod 600 .env

# Создать директорию для логов
mkdir -p logs

# Инициализировать БД (создаёт таблицы и применяет additive SQLite patch:
# колонки price_history.no_mid, positions.exit_reason при обновлении с старой БД)
python -c "from db.session import init_db; init_db()"

# Тестовый запуск (dry run)
python main.py --mode dry_run
# Ctrl+C через 30 секунд, проверить что ошибок нет
```

### 3.7. Загрузка файлов без git (через scp)

```bash
# С локальной машины (Windows PowerShell):

# Загрузить весь проект
scp -P 2222 -r "C:\path\to\prediction_trader" trader@ВАШ_IP:~/apps/

# Загрузить только обновлённый файл
scp -P 2222 "C:\path\to\prediction_trader\main.py" trader@ВАШ_IP:~/apps/prediction_trader/
```

### 3.8. Запуск как systemd-сервис (автозапуск)

```bash
# Создать unit-файл (под sudo)
sudo nano /etc/systemd/system/prediction-trader.service
```

Содержимое:
```ini
[Unit]
Description=Prediction Markets Trader
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=trader
Group=trader
WorkingDirectory=/home/trader/apps/prediction_trader
Environment="PATH=/home/trader/apps/prediction_trader/venv/bin"
ExecStart=/home/trader/apps/prediction_trader/venv/bin/python main.py --mode dry_run
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

# Безопасность: ограничить процесс
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/trader/apps/prediction_trader

[Install]
WantedBy=multi-user.target
```

```bash
# Активировать и запустить
sudo systemctl daemon-reload
sudo systemctl enable prediction-trader
sudo systemctl start prediction-trader

# Проверить статус
sudo systemctl status prediction-trader

# Посмотреть логи
sudo journalctl -u prediction-trader -f

# Перезапустить после изменений
sudo systemctl restart prediction-trader

# Остановить
sudo systemctl stop prediction-trader
```

**ВАЖНО**: для смены режима dry_run → paper → live — изменить `--mode` в service-файле:
```bash
sudo nano /etc/systemd/system/prediction-trader.service
# Изменить: --mode dry_run → --mode paper → --mode live
sudo systemctl daemon-reload
sudo systemctl restart prediction-trader
```

### 3.9. Второй процесс: Telegram analytics (опционально)

Тот же `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, что и для алертов. Запуск **отдельно** от трейдера — long-poll `getUpdates`, только чтение БД, **без** торговых команд (`/status`, `/pnl`, `/positions`, `/trades`, `/gates`, `/health`).

```bash
cd ~/apps/prediction_trader
source venv/bin/activate
python -m monitor.telegram_analytics --config config/settings.yaml
```

Пример **отдельного** systemd-unit:

```ini
[Unit]
Description=Telegram analytics bot (read-only)
After=network.target

[Service]
Type=simple
User=trader
Group=trader
WorkingDirectory=/home/trader/apps/prediction_trader
Environment="PATH=/home/trader/apps/prediction_trader/venv/bin"
ExecStart=/home/trader/apps/prediction_trader/venv/bin/python -m monitor.telegram_analytics --config config/settings.yaml
Restart=on-failure
RestartSec=20

[Install]
WantedBy=multi-user.target
```

Файл: `/etc/systemd/system/prediction-trader-telegram.service` → `daemon-reload`, `enable`, `start`.

Алерты при повторяющихся ошибках цикла **не спамят** каждый раз (cooldown, сводка, сообщение о восстановлении) — `monitor/cycle_alert_throttle.py`. Кратко: `VPS_DEPLOY.md`.

---

## 4. БЕЗОПАСНОСТЬ НА VPS

### 4.1. Защита приватного ключа

```
ПРИВАТНЫЙ КЛЮЧ КОШЕЛЬКА — ЕДИНСТВЕННОЕ, ЧТО МОЖЕТ УКРАСТЬ ВАШИ ДЕНЬГИ.
```

Правила:
- `.env` файл — `chmod 600` (только owner читает)
- Никогда не передавать `.env` через незащищённый канал
- Никогда не коммитить `.env` в git
- Использовать **отдельный** кошелёк только для трейдинга
- На кошельке держать **минимум** средств (пополнять по мере нужды)
- Рассмотреть encrypted keystore вместо plain text key в `.env`

### 4.2. Защита сервера

| Мера | Статус | Как проверить |
|------|--------|---------------|
| SSH только по ключу | Обязательно | `grep PasswordAuth /etc/ssh/sshd_config` |
| Вход root запрещён | Обязательно | `grep PermitRoot /etc/ssh/sshd_config` |
| UFW файрвол | Обязательно | `sudo ufw status` |
| fail2ban | Обязательно | `sudo systemctl status fail2ban` |
| Нестандартный SSH-порт | Желательно | `grep Port /etc/ssh/sshd_config` |
| Автоматические обновления | Обязательно | `systemctl status unattended-upgrades` |
| Пользователь не root | Обязательно | `whoami` → `trader` |

### 4.3. Бэкап БД (ежедневный cron)

```bash
# Создать скрипт бэкапа
nano ~/backup_db.sh
```

```bash
#!/bin/bash
BACKUP_DIR="/home/trader/backups"
DB_PATH="/home/trader/apps/prediction_trader/prediction_trader.db"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"
cp "$DB_PATH" "$BACKUP_DIR/trader_${TIMESTAMP}.db"

# Удалить бэкапы старше 30 дней
find "$BACKUP_DIR" -name "trader_*.db" -mtime +30 -delete
```

```bash
chmod +x ~/backup_db.sh

# Добавить в cron (каждый день в 3:00)
crontab -e
# Добавить строку:
0 3 * * * /home/trader/backup_db.sh
```

### 4.4. Мониторинг uptime

```bash
# Установить простой мониторинг
# Вариант 1: uptimerobot.com (бесплатно, внешний)
# — Не подходит напрямую (нет веб-сервера), но можно мониторить SSH-порт

# Вариант 2: watchdog через systemd (уже настроен — Restart=on-failure)

# Вариант 3: Telegram — алерты цикла (с дедупликацией) + опциональный бот /health, /status
```

### 4.5. Что делать при компрометации

Если подозреваете, что сервер взломан:
1. **Немедленно** отозвать API ключи Polymarket (создать новые)
2. **Перевести средства** с торгового кошелька на другой
3. Остановить сервис: `sudo systemctl stop prediction-trader`
4. Проверить логи: `sudo journalctl --since "1 hour ago"`
5. Проверить процессы: `ps aux | grep -v trader`
6. Пересоздать VPS с нуля (не пытаться «лечить»)

---

## 5. ТИПИЧНЫЕ КОМАНДЫ ДЛЯ РАБОТЫ

```bash
# ─── Подключение ───
ssh -p 2222 trader@ВАШ_IP

# ─── Статус ───
sudo systemctl status prediction-trader
sudo journalctl -u prediction-trader --since "1 hour ago"
tail -f ~/apps/prediction_trader/logs/trader.log

# ─── Управление ───
sudo systemctl stop prediction-trader
sudo systemctl start prediction-trader
sudo systemctl restart prediction-trader

# ─── Обновление кода ───
cd ~/apps/prediction_trader
git pull                    # если через git
# или scp с локальной машины
source venv/bin/activate
pip install -r requirements.txt  # если обновились зависимости
sudo systemctl restart prediction-trader

# ─── Проверка gate-критериев ───
cd ~/apps/prediction_trader
source venv/bin/activate
python -c "
from db.session import get_session
from runner.gate_checks import check_dry_to_paper, check_paper_to_live
session = get_session()
print(check_dry_to_paper(session).summary)
print()
print(check_paper_to_live(session).summary)
"

# ─── Бэкап БД вручную ───
cp prediction_trader.db ~/backups/manual_$(date +%Y%m%d).db

# ─── Мониторинг ресурсов ───
htop                        # процессы и RAM
df -h                       # диск
free -h                     # память

# ─── Перезагрузка VPS ───
sudo reboot
# Сервис перезапустится автоматически (systemd enable)
```

---

## 6. ЧЕКЛИСТ ПЕРЕД ЗАПУСКОМ НА VPS

```
[ ] VPS заказан (Hetzner CPX11 или аналог)
[ ] SSH-ключ создан и добавлен
[ ] Пароль для SSH отключён
[ ] Root-вход запрещён
[ ] UFW настроен (только SSH-порт открыт)
[ ] fail2ban установлен и работает
[ ] Пользователь trader создан
[ ] Python 3.12 + venv установлены
[ ] Проект загружен
[ ] .env создан с правами 600
[ ] pip install -r requirements.txt выполнен
[ ] БД инициализирована
[ ] dry_run работает без ошибок
[ ] systemd сервис создан и включён
[ ] cron-бэкап БД настроен
[ ] Telegram-алерты тестируются (при включённом `telegram_enabled`)
[ ] Опционально: второй сервис `telegram_analytics` для /status, /trades, …
[ ] Кошелёк отдельный (не основной!) — только перед live
[ ] На кошельке минимум средств
```
