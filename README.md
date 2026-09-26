# Ai_manager_crm — Autonomous AI Sales Agent for amoCRM & Kommo | Автономный AI-агент для amoCRM и Kommo

<p align="left">
  <a href="https://github.com/Behi7/Ai_manager_crm/releases/tag/v1.0.0"><img src="https://img.shields.io/badge/Release-v1.0.0-brightgreen?logo=github" alt="Release v1.0.0" /></a>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/LLM-Google%20Gemini-4285F4?logo=google&logoColor=white" alt="Google Gemini" />
  <img src="https://img.shields.io/badge/CRM-amoCRM%20%2F%20Kommo-FF3B30" alt="amoCRM / Kommo" />
  <img src="https://img.shields.io/badge/Redis-Debounce%20Queue-DC382D?logo=redis&logoColor=white" alt="Redis" />
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/Tests-82%20Passed-success" alt="Tests" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT" />
</p>

<p align="left">
  <b>🌐 Language / Язык:</b>
  <a href="#-english-documentation"><b>🇬🇧 English Documentation</b></a> &nbsp;|&nbsp;
  <a href="#-документация-на-русском"><b>🇷🇺 Документация на русском</b></a>
</p>

---

## 🇬🇧 English Documentation

**Ai_manager_crm** is a production-ready, self-hosted **Autonomous AI Sales Agent, Chatbot & Lead Qualifier for amoCRM and Kommo CRM**, built with **FastAPI**, **Google Gemini API**, **Redis**, and **PostgreSQL**.

Unlike simple CLI tools or basic webhook scripts, **Ai_manager_crm** is a complete **Dual-Agent AI Sales System** designed for real sales departments. It connects to any messaging channel in amoCRM / Kommo (WhatsApp, Telegram, Instagram, Facebook, Viber, Live Chat), conducts natural multi-turn conversations in the client's language, extracts qualification fields into CRM cards, automatically advances deals through pipeline stages, and hands over qualified or complex leads to human managers.

### ✨ Key Features (v1.0.0)

1. **Dual-Agent Architecture (AI Communicator + AI Extractor)**:
   - **AI Extractor**: Analyzes incoming client messages (including split messages buffered in Redis), extracts structured qualification parameters (Name, Budget, City, Product Interest, Timeline, etc.), and updates **amoCRM / Kommo Lead & Contact custom fields** (`leads` and `contacts` entity types).
   - **AI Communicator**: Replies naturally as a company sales manager following the **"One-Question Rule"**, using your custom **Company Knowledge Base** (with **Gemini Context Caching** support) and 3-column System Prompts.
   - **Cascading Model Fallback**: Automatically switches between backup Gemini models (`gemini-3.1-flash-lite` ➔ `gemini-2.5-flash` ➔ `gemini-3.5-flash`) on HTTP 429/503 errors so client chats never drop.

2. **Automated Pipeline Stage Progression (`0 -> 1 -> 2 -> 3 -> 4`)**:
   - Automatically moves leads across your amoCRM / Kommo pipeline stages as qualification progresses:
     - `Stage 0 -> Stage 1`: Initial AI engagement.
     - `Stage 2`: Contact qualification fields completed.
     - `Stage 3`: Full Deal + Contact qualification completed.
     - `Stage 4 (Handover)`: Client requests a human operator or AI reaches the stuck threshold — moves the lead to the Handover stage and replies in the client's language.
   - **Per-Stage AI Checkboxes**: Enable or disable AI responses on any specific pipeline stage via the Admin UI.

3. **Redis Debounce Message Queue**:
   - Aggregates rapid bursts of short client messages over a configurable time window (e.g., 10–25 seconds) into a single coherent context before invoking the LLM.
   - Atomic Lua script buffer management (`LRANGE + DEL`) and automatic buffer recovery on network failures.

4. **Multimodal Chat Processing (Voice & Images)**:
   - Native processing and transcription of voice messages (`.ogg`, `.mp3`, `.wav`, `.m4a`) and images sent by clients in amoCRM / Kommo chats.

5. **Self-Healing CRM Integration & Security**:
   - **Auto-Healing Custom Fields**: Isolates deleted amoCRM custom fields during updates and automatically recreates the hidden AI Reply field (`AI: Ответ ассистента`) if deleted.
   - **Salesbot Delivery**: Delivers replies seamlessly into WhatsApp/Telegram/Instagram via a hidden lead field and amoCRM Salesbot trigger.
   - **Security**: Fernet AES-128 token encryption at rest, `ADMIN_API_KEY` protection, SSRF domain whitelist (`.amocrm.ru`, `.amocrm.com`, `.kommo.com`), and anti-prompt-injection guardrails.

---

## 🇷🇺 Документация на русском

**Ai_manager_crm** — полнофункциональный автономный **AI-агент (ИИ-менеджер отдела продаж)** и квалификатор лидов для **amoCRM** и **Kommo CRM** на базе **FastAPI**, **Redis**, **PostgreSQL** и **Google Gemini API**.

Сервис автоматически обрабатывает входящие сообщения от клиентов из любых подключенных к amoCRM мессенджеров (WhatsApp, Telegram, Instagram, Avito, VK, онлайн-чаты), ведет естественный диалог на языке клиента, защищен от prompt-инъекций, производит поэтапную квалификацию лидов, заполняет поля Сделки и Контакта и автоматически двигает сделку по этапам воронки.

### 🚀 Основные возможности (Версия 1.0.0)

1. **Двухагентная архитектура (Экстрактор + Общитель)**:
   - **ИИ-Экстрактор**: извлекает данные из переписки (имя, бюджет, город, услугу, сроки и т.д.) и записывает их в кастомные и системные поля **Контакта** (`contacts`) и **Сделки** (`leads`) в amoCRM. Повторно прогоняется по буферу перед ответом Общителя, чтобы склеивать раздельные сообщения клиента.
   - **ИИ-Общитель**: ведет живой диалог по 3-колоночному системному промпту и **Базе знаний компании** (с поддержкой **Gemini Context Caching**), соблюдая *«правило одного вопроса»*.
   - **Каскадный Fallback моделей**: при перегрузке основной модели (`429` / `503`) автоматически переключается на резервные модели Gemini без прерывания диалога.

2. **Автоматическое продвижение по воронке (`0 -> 1 -> 2 -> 3 -> 4`)**:
   - Переводит сделку по этапам воронки amoCRM по мере заполнения квалификационных полей контакта и сделки.
   - Гибкие галочки этапов в админ-панели: вы сами выбираете, на каких этапах воронки ИИ должен отвечать, а на каких — молчать.
   - Умный **Handover (перевод на оператора)**: при запросе менеджера или превышении лимита непониманий ИИ вежливо отвечает клиенту на его языке и переводит сделку на этап подключения живого менеджера.

3. **Debounce-очередь сообщений (Redis)**:
   - Объединяет серии коротких сообщений от клиента за настраиваемый интервал в единый контекст перед отправкой в нейросеть.
   - Атомарные операции через Lua-скрипты и возврат сообщений в буфер при сетевых сбоях.

4. **Мультимодальность (Голос + Фото)**:
   - Распознавание голосовых сообщений и изображений, присланных клиентом в чат amoCRM.

5. **Самовосстановление (Self-Healing) и Админ-панель (`/admin`)**:
   - Удобная веб-админка для управления несколькими аккаунтами amoCRM, воронками, полями, базой знаний и промптами.
   - Поштучная изоляция удалённых в amoCRM полей и автоматическое пересоздание поля ответа ИИ для Salesbot.

---

## 🛠 Tech Stack / Технологический стек

- **Backend**: Python 3.11+, FastAPI, Uvicorn, Pydantic v2
- **Database**: PostgreSQL 16 (asyncpg, SQLAlchemy 2.0 Async, Alembic)
- **Cache & Queue**: Redis 7 (Lua atomic scripts, Debounce queue)
- **AI / LLM**: Google Gemini API (`gemini-3.1-flash-lite`, `gemini-2.5-flash`, Multimodal, Context Caching)
- **Security**: Cryptography (Fernet symmetric encryption), Admin API Key Auth, SSRF Guard
- **Containers**: Docker, Docker Compose

---

## 📦 Quick Start / Быстрый старт

### 1. Clone the repository / Клонирование репозитория

```bash
git clone https://github.com/Behi7/Ai_manager_crm.git
cd Ai_manager_crm
```

### 2. Virtual environment & dependencies / Окружение и зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment Configuration / Настройка `.env`

```bash
cp .env.example .env
```

Fill in your `.env` file / Заполните переменные в `.env`:
- `DATABASE_URL` — PostgreSQL connection string (`postgresql+asyncpg://...`)
- `REDIS_URL` — Redis connection URL (`redis://127.0.0.1:6380/0`)
- `SECRET_KEY` — 32-byte Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
- `GEMINI_API_KEY` — Google Gemini API Key
- `ADMIN_API_KEY` — Secret key for accessing `/admin` and management API
- `BASE_URL` — Public HTTPS URL of your server for amoCRM webhooks

### 4. Start Infrastructure & Run Migrations / Запуск БД и миграций

```bash
docker compose up -d
alembic upgrade head
```

### 5. Run the Server / Запуск сервера

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

- **Admin Panel / Админ-панель**: `http://localhost:8080/admin`
- **Swagger API Docs**: `http://localhost:8080/docs`
- **Health Check**: `http://localhost:8080/health`

---

## 🧪 Running Tests / Запуск тестов

```bash
./.venv/bin/python -m unittest discover tests
```
*(82 automated unit & resilience tests included).*

---

## 🔍 Search Keywords / Ключевые слова для поиска

`amoCRM AI Agent` • `Kommo AI Agent` • `AI Salesbot amoCRM` • `Google Gemini amoCRM integration` • `Autonomous AI Sales Manager` • `ИИ агент для amoCRM` • `ИИ бот для amoCRM` • `Интеграция Gemini и ChatGPT с amoCRM` • `Автоматическая квалификация лидов amoCRM` • `Salesbot ИИ менеджер продаж`

---

## 📄 License / Лицензия

MIT License.
