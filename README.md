# PolymarketBTC15

Исследовательский Python-проект для анализа маркетов Polymarket и симуляции maker-логики без акцента на live-trading по умолчанию.

Репозиторий собран как инженерный стенд: работа с рыночными данными, риск-логикой, on-chain кошельком, мониторингом и локальным dashboard-слоем. Для публичного показа проект описан как исследовательская и shadow/paper-trading система, а не как «готовый торговый бот под деньги».

## Что есть в проекте

- клиент для работы с Polymarket/CLOB;
- модуль кошелька для Polygon с загрузкой ключа только из окружения;
- риск-менеджмент и логика принятия решений;
- мониторинг, алерты и Telegram-аналитика;
- локальный dashboard;
- deployment-документация для shadow-режима.

## Безопасность

- реальные секреты не должны храниться в коде и git;
- приватный ключ загружается только через `POLYGON_PRIVATE_KEY`;
- `.env.example` содержит только пустые шаблоны переменных;
- `shadow_maker` предполагает безопасный режим без реальных ордеров по умолчанию.

## Переменные окружения

Основные переменные описаны в `.env.example`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ADMIN_CHAT_ID`
- `TELEGRAM_ADMIN_CHAT_IDS`
- `POLYGON_PRIVATE_KEY`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`
- `QWEN_API_KEY`

## Позиционирование для публичного репозитория

Этот проект лучше воспринимается как:

- research / experimentation sandbox;
- paper-trading / shadow execution environment;
- инженерный проект про API, данные, риск и инфраструктуру.
