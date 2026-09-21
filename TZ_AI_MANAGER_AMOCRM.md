# Техническое задание (ТЗ) и Архитектура: Универсальный AI-менеджер для amoCRM

Этот документ — окончательная полная спецификация для реализации проекта. Он описывает финальные, согласованные архитектурные решения.

---

## 1. Что строим

SaaS-сервис (backend на Python), к которому через админ-панель подключается множество amoCRM-аккаунтов. Для каждого аккаунта сервис:

1. Ведёт переписку с клиентами в чате amoCRM (Telegram/WhatsApp/Instagram/сайт — без разницы, все каналы приходят в единый чат amoCRM) с помощью LLM-агента **«Общитель»**.
2. Извлекает из переписки заданные администратором поля (имя, бюджет, телефон и т.д.) с помощью LLM-агента **«Экстрактор»** и записывает их в сделку amoCRM.
3. При признаках, что ИИ не справляется — передаёт диалог человеку (handover).

Ключевой архитектурный принцип: **источник правды о том, что уже сказал клиент — это история переписки в БД сервиса, а не кастомные поля amoCRM.** Экстрактор пишет данные в CRM для отчётности и работы менеджеров, но Общитель никогда не читает CRM-поля, чтобы понять контекст диалога — только полную историю сообщений.

---

## 2. Глоссарий

| Термин | Значение |
|---|---|
| Аккаунт | Один подключённый amoCRM-портал (одна запись в таблице `accounts`) |
| Лид | Одна сделка amoCRM = один диалог с одним клиентом |
| Общитель | LLM-вызов, генерирующий ответ клиенту в чат (минимальный TTFR, критический путь) |
| Экстрактор | LLM-вызов, извлекающий структурированные данные из диалога в JSON (выполняется после отправки ответа клиенту) |
| LEAD_BUSY | Блокировка лида на время полного цикла обработки одного сообщения |
| Handover | Передача диалога человеку-оператору, ИИ перестаёт отвечать автоматически |
| Дебаунс | Склейка нескольких быстрых сообщений клиента подряд в один запрос к ИИ |

---

## 3. Технологический стек

- Python 3.11+, FastAPI — HTTP-слой (админ-API + приём вебхуков).
- PostgreSQL — основное хранилище данных.
- Redis — блокировки (`LEAD_BUSY`), дебаунс-буфер, дедупликация сообщений (`SETNX`).
- Celery / Async Worker (брокер — Redis) — фоновая обработка очереди лидов, чтобы вебхук-эндпоинт отвечал `200 OK` за миллисекунды.
- LLM-провайдер — Google Gemini 3.1 Flash Lite через отдельный адаптер-модуль (`llm_client.py`).

---

## 4. Схема БД

```sql
CREATE TYPE account_status AS ENUM (
    'pending_validation',
    'field_created',
    'awaiting_manual_bot',
    'bot_linked',
    'awaiting_pipeline_setup',
    'configured',
    'verified',
    'error'
);

CREATE TABLE accounts (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                     TEXT,
    subdomain                TEXT NOT NULL UNIQUE,
    amo_account_id           BIGINT,
    encrypted_token          BYTEA NOT NULL,
    encrypted_refresh_token  BYTEA,                 -- nullable для будущего OAuth 2.0
    token_created_at         TIMESTAMPTZ,
    token_expires_at         TIMESTAMPTZ,
    status                   account_status NOT NULL DEFAULT 'pending_validation',

    ai_reply_field_id        BIGINT,
    bot_id                   BIGINT,

    webhook_id               BIGINT,
    webhook_auto_registered  BOOLEAN NOT NULL DEFAULT FALSE,
    webhook_verified         BOOLEAN NOT NULL DEFAULT FALSE, -- true ТОЛЬКО после реально
                                                              -- полученного живого вебхука от клиента

    last_error               TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE pipelines (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id       UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amo_pipeline_id  BIGINT NOT NULL,
    name             TEXT NOT NULL,
    is_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, amo_pipeline_id)
);

CREATE TABLE field_mappings (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id            UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amo_field_id          BIGINT NOT NULL,
    field_name            TEXT NOT NULL,
    field_type            TEXT NOT NULL,
    is_enabled            BOOLEAN NOT NULL DEFAULT FALSE,
    ai_hint               TEXT,                -- подсказка администратора для Экстрактора
    overwrite_if_filled   BOOLEAN NOT NULL DEFAULT FALSE,
    synced_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, amo_field_id)
);

CREATE TABLE ai_configs (
    account_id           UUID PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    communicator_prompt  TEXT NOT NULL DEFAULT '',
    communicator_model   TEXT NOT NULL DEFAULT 'gemini-3.1-flash-lite',
    extractor_model      TEXT NOT NULL DEFAULT 'gemini-3.1-flash-lite',
    temperature          NUMERIC(3,2) NOT NULL DEFAULT 0.4,
    handover_after_stuck INT NOT NULL DEFAULT 4
);

CREATE TABLE leads (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id         UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amo_lead_id        BIGINT NOT NULL,
    amo_contact_id     BIGINT,
    is_busy            BOOLEAN NOT NULL DEFAULT FALSE,
    stuck_count        INT NOT NULL DEFAULT 0,
    handover_required  BOOLEAN NOT NULL DEFAULT FALSE,
    last_message_at    TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, amo_lead_id)
);

CREATE TABLE conversation_messages (
    id          BIGSERIAL PRIMARY KEY,
    lead_id     UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conv_lead_time ON conversation_messages (lead_id, created_at);

CREATE TABLE processed_webhook_messages (
    message_id    TEXT PRIMARY KEY,
    account_id    UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    lead_id       UUID REFERENCES leads(id) ON DELETE SET NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE extraction_logs (
    id             BIGSERIAL PRIMARY KEY,
    lead_id        UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    raw_response   JSONB NOT NULL,
    applied_fields JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 5. Поток подключения аккаунта (Hybrid Onboarding)

### Шаг 1 — админ вводит `subdomain` + `token`, жмёт «Готово»

Backend выполняет синхронно (UI показывает спиннер):

1. `GET https://{subdomain}.amocrm.ru/api/v4/account` с `Authorization: Bearer {token}`.
   - `401` → вернуть ошибку «неверный поддомен или токен», дальше не идти.
   - `200` → сохранить `amo_account_id`, имя аккаунта.
2. `GET https://{subdomain}.amocrm.ru/api/v4/leads/custom_fields` — искать поле с именем `"AI: Ответ ассистента"`.
3. Если не найдено — создать:
   ```
   POST https://{subdomain}.amocrm.ru/api/v4/leads/custom_fields
   [{ "name": "AI: Ответ ассистента", "type": "textarea" }]
   ```
   Сохранить `ai_reply_field_id` из ответа (`_embedded.custom_fields[0].id`).
   Статус аккаунта → `field_created`.
4. Попытаться зарегистрировать вебхук автоматически (best-effort, не блокирует переход дальше):
   ```
   POST https://{subdomain}.amocrm.ru/api/v4/webhooks
   { "destination": "https://api.<domain>/webhook/{account_uuid}",
     "settings": ["add_message"] }
   ```
   Если запрос успешен (`200`/`201`) — сохранить `webhook_id`, `webhook_auto_registered = true`. `webhook_verified` остаётся `false` до реального события от клиента.
5. Статус аккаунта → `awaiting_manual_bot`.

### Шаг 2 — экран с инструкцией для ручной настройки

UI показывает два блока «скопировать»:

- **Вебхук**: URL `https://api.<domain>/webhook/{account_uuid}`. Текст: «Если автоматическая регистрация не сработала на вашем тарифе, добавьте этот URL в Настройки → Интеграции → Webhooks (событие: Новое сообщение в чате / add_message)».
- Готовая строка для узла «Отправить сообщение» в Salesbot:
  `{{lead.cf.<ai_reply_field_id>}}` (подставить реальное число).
- Инструкция: «Соберите в конструкторе Salesbot двух-блочный сценарий:
  `[Старт] → [Отправить сообщение: {{lead.cf.<id>}}]`. НЕ добавляйте в бота никаких вебхук-шагов — вся логика уже на нашей стороне».

### Шаг 3 — возврат в приложение, привязка бота и верификация

1. `GET /accounts/{id}/bots` → backend делает `GET https://{subdomain}.amocrm.ru/api/v4/bots`, возвращает список для выпадающего меню (по `name`).
2. Админ выбирает бота → `POST /accounts/{id}/link-bot { bot_id }`. Статус → `bot_linked`.
3. Кнопка **«Проверить связку бота»**: backend делает тестовый `PATCH /api/v4/leads/{lead_id}` в поле `ai_reply_field_id` и `POST /api/v4/bots/{bot_id}/run`. Если получен `202 Accepted` — бот успешно привязан.
4. **Окончательная верификация (`webhook_verified = true`)**:
   Выставляется автоматически, как только на `/webhook/{account_uuid}` физически прилетает **первое живое входящее сообщение от клиента/тестера** (`author[type] == "external"`). Статус аккаунта переходит в `verified`.

### Шаг 4 — выбор воронок и полей

`GET /accounts/{id}/pipelines` и `GET /accounts/{id}/fields` — синхронизация через `GET /api/v4/leads/pipelines` и `GET /api/v4/leads/custom_fields`, сохранение выбора админа в `pipelines` / `field_mappings`. Статус → `configured` (если уже был `verified` — `verified` сохраняется).

---

## 6. Приём входящих сообщений

### 6.1. Публичный вебхук-приёмник

`POST /webhook/{account_uuid}`

**Обязательное требование: ответ `200 OK` со статусом `{"status": "ok"}` должен уходить меньше чем за 100 мс.** Вся обработка — асинхронная.

Фильтры при получении (до постановки в очередь):
1. `message[add][0][author][type] != "external"` → игнорировать (отсекаем собственные исходящие сообщения бота и менеджеров).
2. `time.time() - message[add][0][created_at] > 90` → игнорировать (устаревшие ретраи).
3. Если это первый входящий вебхук для данного `account_uuid` и `webhook_verified == false` — выставить `webhook_verified = true`, `status = 'verified'`.
4. Дедупликация: `message[add][0][id]` — ключ в Redis (`SETNX`, TTL 10 минут). Если ключ уже есть — вернуть `200 OK` и не дублировать обработку.
5. Положить текст сообщения в дебаунс-буфер по ключу `lead_id` (окно тишины 1.5–2.0 сек).

---

### 6.2. Фоновая обработка лида (Строгий порядок: Чат ➔ Экстрактор ➔ Освобождение)

```python
async def process_lead_after_debounce(account_uuid: str, lead_id: str):
    """
    1. Ответ клиенту отправляется максимально быстро (критический путь).
    2. Экстрактор выполняется сразу после отправки, ПОКА ЕЩЁ ДЕРЖИТСЯ LEAD_BUSY лок
       (защита от гонок при обновлении полей CRM следующими сообщениями).
    """
    if lead_is_busy(account_uuid, lead_id):
        return  # Сообщения остаются в буфере дебаунса
    set_lead_busy(account_uuid, lead_id, True)

    try:
        if handover_required(account_uuid, lead_id):
            create_operator_task(account_uuid, lead_id, reason="handover")
            return

        new_text = pop_debounced_text(account_uuid, lead_id)
        save_message(lead_id, role="user", content=new_text)
        history = load_history(lead_id, limit=20)

        # ------------------------------------------------------------------
        # 1) КРИТИЧЕСКИЙ ПУТЬ: Клиент ждёт ответ в мессенджере
        # ------------------------------------------------------------------
        reply_text = await run_communicator(account_uuid, lead_id, history)
        
        # Строгая последовательность: сначала запись в поле, потом запуск бота
        patch_ok = await amo_patch_lead_field(account_uuid, lead_id, "ai_reply", reply_text)
        if not patch_ok:
            create_operator_task(account_uuid, lead_id, reason="patch_failed")
            return
            
        await amo_run_bot(account_uuid, lead_id)
        save_message(lead_id, role="assistant", content=reply_text)
        # Клиент УЖЕ получил ответ в мессенджер (<1.5 сек)!

        # ------------------------------------------------------------------
        # 2) ЭКСТРАКТОР: Выполняется сразу следом, пока лок ещё активен
        # ------------------------------------------------------------------
        full_dialog = history + [{"role": "model", "parts": [{"text": reply_text}]}]
        extracted_fields = await run_extractor(account_uuid, lead_id, full_dialog)
        
        if extracted_fields:
            await amo_patch_lead_fields(account_uuid, lead_id, extracted_fields)
            log_extraction(lead_id, extracted_fields)
            reset_stuck_counter(account_uuid, lead_id)
        else:
            increment_stuck_counter(account_uuid, lead_id)

    finally:
        # Лок освобождается только после полного завершения цикла
        set_lead_busy(account_uuid, lead_id, False)
```

---

## 7. Два ИИ-агента

### 7.1. Общитель (`communicator`)
- Вход: системный промпт + история переписки (до 20 последних реплик).
- Не читает кастомные поля CRM — источник правды только история диалога.
- Выход: чистый текст без квадратных скобок и плейсхолдеров.
- Базовый системный промпт:
```
Ты — профессиональный, живой и вежливый ИИ-менеджер по работе с клиентами.
Отвечай кратко, по делу, на том языке, на котором пишет клиент (русский или узбекский).
СТРОГО ЗАПРЕЩЕНО:
1. Использовать плейсхолдеры в квадратных скобках вида [цена], [имя], [ссылка].
2. Здороваться повторно, если диалог уже идёт.
3. Игнорировать то, что клиент уже сообщил ранее в этом диалоге.
Если клиент торопит («алло», «вы тут?») — подтверди присутствие и продолжи диалог по теме.
```

### 7.2. Экстрактор (`extractor`)
- Вход: история диалога + список активных полей из `field_mappings` (`amo_field_id`, `field_name`, `field_type`, `ai_hint`).
- Выход: строго валидный JSON по схеме.
- Заполняет только те поля, которые клиент реально сообщил в диалоге.

---

## 8. Защита от гонок и дублей

| Ситуация | Решение |
|---|---|
| Повторный вебхук amoCRM | Redis `SETNX` по `message[add][0][id]`, TTL 10 минут |
| Эхо собственного сообщения | Фильтр `author[type] == external` |
| Устаревший ретрай после простоя | Фильтр `created_at` > 90 секунд |
| Клиент шлёт 2–3 фразы подряд | Дебаунс-буфер в Redis (окно 1.5–2.0 сек) |
| Параллельная обработка лида | Redis-лок `LEAD_BUSY` на весь цикл функции |
| Зависание сбора данных | Счётчик `stuck_count` ➔ задача оператору (`POST /api/v4/tasks`) |

---

## 9. Список API-роутов

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/accounts` | Подключение аккаунта (проверка токена, создание поля, попытка вебхука) |
| GET | `/accounts` | Список подключенных аккаунтов |
| GET | `/accounts/{id}` | Детали и статус аккаунта |
| GET | `/accounts/{id}/bots` | Список ботов amoCRM для выпадающего списка |
| POST | `/accounts/{id}/link-bot` | Привязка выбранного `bot_id` |
| POST | `/accounts/{id}/test-connection` | Кнопка «Проверить связку бота» |
| GET | `/accounts/{id}/pipelines` | Список воронок amoCRM |
| PATCH | `/accounts/{id}/pipelines` | Сохранение включенных воронок |
| GET | `/accounts/{id}/fields` | Список кастомных полей сделок |
| PATCH | `/accounts/{id}/fields` | Сохранение выбранных полей + `ai_hint` |
| GET | `/accounts/{id}/status` | Поллинг статуса онбординга |
| POST | `/webhook/{account_uuid}` | Публичный приёмник входящих сообщений amoCRM |

---

## 10. Критические ограничения

1. **Никаких вебхук-шагов внутри Salesbot.** Только системный вебхук amoCRM.
2. **Salesbot собирается вручную за 30 секунд** из 2 блоков: `[Старт] -> [Отправить {{lead.cf.<id>}}]`.
3. **Никакого автозапуска Salesbot в Воронке** на входящее сообщение. Запуск производит только бэкенд по API.
4. **Строгая последовательность:** `PATCH reply` ➔ `200 OK` ➔ `POST /bots/run` ➔ `Экстрактор` ➔ снятие `LEAD_BUSY`.
