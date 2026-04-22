# Research sprint — H2/H4, dataset v1, p_model v1, validation

## 1. Состояние проекта (кратко)

Инженерный контур (broker, side-aware YES/NO, gates, WS, тесты) достаточен для **честного** research: bottleneck сместился в **данные и alpha**. Следующий спринт — не оркестратор, а **dataset + baselines + калибровка + временная валидация**.

## 2. Формулировка edge (проверяемая)

**Гипотеза:** в зонах около **0.10 / 0.50 / 0.90** (H2) и в **хвостах** низкой/высокой вероятности (H4) условное распределение исходов **систематически отличается** от рыночного prior (`p_market`) после учёта издержек, в подвыборках по **category, liquidity, spread, TTE** — и это **воспроизводимо** на hold-out и walk-forward.

Рынок = **prior**; модель = **коррекция**, обучаемая только на train (+ выбор гиперпараметров на val).

## 3. Deliverables в репозитории

| Артефакт | Файл |
|----------|------|
| Dataset spec v1 | `research/dataset_spec_v1.yaml` |
| Строка датасета (тип) | `research/dataset_row.py` |
| p_market / target | `research/definitions.py` |
| Baselines A–E | `research/baselines.py` |
| Формула p_model v1 | `research/model_v1.py` |
| Temporal split | `research/splits.py` |
| Метрики | `research/metrics.py` |
| Валидация строк | `research/data_quality.py` |
| Frozen report шаблон | `research/FROZEN_REPORT_TEMPLATE.md` |
| Cost / EV assumptions (versioned) | `research/cost_assumptions.py` |

## 4. Определения (зафиксировано в коде)

### p_market (side-aware)

- **YES-строка:** prior = **mid на YES** в `decision_ts` → в датасете **`p_market_source=native_yes`** (`PriceHistoryRow.mid`, исторический коллектор — см. `scripts/collect_historical.py`).
- **NO-строка:** приоритет — **нативный NO mid** (`PriceHistoryRow.no_mid`: Gamma NO при сборе истории; в цикле оркестратора — mid NO-книги, если книга запрошена). Если нативной цены нет — **complement** из YES через `research/definitions.p_market_fallback_no_from_yes_complement` с флагом **`fallback_complement_no_book`** и **`p_market_source=complement_fallback`** (никогда «тихо»).
- Колонка экспорта **`p_market_source`**; в **`evaluate`** — отдельные сегменты и предупреждение при высокой доле complement.

### EV в `evaluate` (исследовательский proxy)

- Единое место: **`research/cost_assumptions.py`** (`ev_proxy_v1`: `y - p - flat_fee`). Это **не** realized EV исполнения; ограничения явно попадают в JSON отчёт и блок в frozen report.

### Target

- `resolved_outcome_for_side` ∈ {0,1}: **1 iff выиграл исход для выбранной стороны** (`research/definitions.resolved_outcome_for_side`).

## 5. Baselines

| ID | Имя | Содержание |
|----|-----|------------|
| A | `baseline_market` | `p = p_market` |
| B | `baseline_calibrated` | калибровка `p_market` без H2/H4 |
| C | `baseline_h2_only` | B + H2 |
| D | `baseline_h4_only` | B + H4 |
| E | `model_v1` | B + H2 + H4 + micro (ограниченный) |

Сравнение на **одинаковых** сплитах и **одинаковых** допущениях по комиссиям/исполнению.

## 6. План валидации

1. Построить датасет с `decision_ts` и строго **временным** train / val / hold-out (`research/splits.py`).  
2. Обучить калибровку и смещения на train; **гиперпараметры** (бакеты, τ хвостов) — только по val.  
3. Заморозить параметры; оценить A–E на **hold-out**.  
4. Walk-forward (скользящие окна).  
5. Bootstrap CI для EV / среднего PnL.  
6. Сегменты: category, TTE, liquidity, round vs non-round, tail vs non-tail.  
7. Оформить **frozen report** (`FROZEN_REPORT_TEMPLATE.md`).

## 7. Checklist: `collect_historical` и живые данные

Перед обучением:

1. Ответ API: список vs dict, поля `tokens`, `winner`, `resolved`.  
2. `condition_id` / `id` согласованы с `market_id` в спеке.  
3. Outcome: согласован с `resolved_outcome_for_side` при разворачивании в **две строки** (YES и NO) или одной — **политика строк** должна быть одна на весь датасет.  
4. Timestamps: `decision_ts` не после известного resolution; `resolved_ts` ≥ `decision_ts`.  
5. Исторический коллектор пишет **синтетический** spread вокруг YES mid (`spread=0.02`) — строки помечаются **`synthetic_mid_from_collector`** в `quality_flags`; при наличии **Gamma** цен задаются **`mid` (YES)** и **`no_mid` (NO)**. Live-цикл оркестратора дополняет **`no_mid`** из NO orderbook при записи `PriceHistoryRow`.  
6. Дубликаты: ключ `(dataset_version, market_id, token_id, side, decision_ts)`.  
7. Leakage: фичи только из данных ≤ `decision_ts`.

## 8. Leakage и fake edge — что проверять

| Риск | Проверка |
|------|-----------|
| Будущее в фичах | Жёсткий cutoff по `decision_ts` |
| Подгонка на hold-out | Запрет правок модели после просмотра hold-out |
| Survivorship | Правила включения закрытых/разрешённых рынков задокументированы |
| Неверный target | Юнит-тесты `resolved_outcome_for_side` |
| YES/NO цена | Нативные книги; fallback помечен |
| Оптимистичное исполнение | Единые `entry_fee_*` / `execution_price_assumption` в метриках |
| Слишком много параметров H2/H4 | Регуляризация, мало зон, OOS |

## 9. Критерий «edge подтверждена» (напоминание)

Одновременно: E лучше A и B на hold-out; EV после издержек > 0; метрики согласованы; Sharpe не шум; bootstrap CI не уничтожает тезис; эффект не только в одной категории/месяце; walk-forward воспроизводим.

## 10. Roadmap по модулям / файлам

| Шаг | Действие | Где |
|-----|----------|-----|
| A1 | Прогнать collect, зафиксировать расхождения API | `scripts/collect_historical.py` |
| A2 | ETL: Gamma/БД → `ResearchDatasetRowV1` + Parquet/CSV | `research/build_dataset.py` (создать) |
| A3 | Bucket policies (TTE, spread, liq) | `research/buckets.py` (создать) |
| B | Обучение калибровки (isotonic/Platt) | `research/train_calibration.py` (создать) |
| C | Оценка A–E + отчёт | `research/evaluate.py` (создать) или ноутбук |
| D | Frozen report заполнить | `FROZEN_REPORT_TEMPLATE.md` |

## 11. Что не делать в этом спринте

H1/news, LLM, тяжёлый dashboard, мультибиржа, новые execution-фичи без блокировки research.

---

**Главный вопрос этапа:** *можем ли показать, что простая калиброванная H2/H4 модель статистически лучше рынка после всех издержек на hold-out и walk-forward?*
