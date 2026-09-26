import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, AsyncSessionLocal
from app.services.debounce_service import debounce_service
from app.services.amocrm_client import amocrm_client
from app.services.gemini_client import gemini_http_client
from app.api.routes_admin import router as admin_router
from app.api.routes_accounts import router as accounts_router
from app.api.routes_webhook import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("Main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Инициализация AI Manager Backend...")
    # Проверка подключения к БД (Fail-fast, IMPORTANT-07)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            await session.execute(text("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS stages_json JSONB DEFAULT '[]'::jsonb;"))
            await session.execute(text("ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS enabled_stage_ids JSONB DEFAULT NULL;"))
            await session.execute(text("ALTER TABLE ai_configs ADD COLUMN IF NOT EXISTS debounce_delay_seconds NUMERIC(4, 1) DEFAULT 2.5 NOT NULL;"))
            await session.execute(text("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'leads' AND column_name = 'is_busy'
                    ) THEN
                        ALTER TABLE leads ALTER COLUMN is_busy SET DEFAULT false;
                        ALTER TABLE leads ALTER COLUMN is_busy DROP NOT NULL;
                    END IF;
                END $$;
            """))
            await session.commit()
        logger.info("Подключение к PostgreSQL и проверка схемы успешны.")
    except Exception as e:
        logger.critical(f"Критическая ошибка подключения к PostgreSQL при запуске: {e}")
        raise RuntimeError(f"Не удалось подключиться к PostgreSQL: {e}") from e

    # Проверка подключения к Redis (Fail-fast, IMPORTANT-10)
    try:
        r = await debounce_service.get_redis()
        pong = await r.ping()
        logger.info(f"Подключение к Redis успешно: {pong}")
    except Exception as e:
        logger.critical(f"Критическая ошибка подключения к Redis при запуске: {e}")
        raise RuntimeError(f"Не удалось подключиться к Redis: {e}") from e

    try:
        yield
    finally:
        # Гарантированное освобождение ресурсов при любом исходе (IMPORTANT-06)
        logger.info("Завершение работы AI Manager Backend...")
        try:
            for timer in list(debounce_service._timers.values()):
                timer.cancel()
            debounce_service._timers.clear()
            if debounce_service._background_tasks:
                logger.info(f"Ожидание завершения {len(debounce_service._background_tasks)} фоновых задач дебаунса...")
                await asyncio.gather(*debounce_service._background_tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Ошибка ожидания фоновых задач при остановке: {e}")

        try:
            await amocrm_client.close()
        except Exception as e:
            logger.error(f"Ошибка закрытия AmoCRM HTTP client: {e}")

        try:
            await gemini_http_client.close()
        except Exception as e:
            logger.error(f"Ошибка закрытия Gemini HTTP client: {e}")

        try:
            if debounce_service.redis:
                await debounce_service.redis.close()
        except Exception as e:
            logger.error(f"Ошибка закрытия Redis: {e}")

        try:
            await engine.dispose()
        except Exception as e:
            logger.error(f"Ошибка закрытия DB engine: {e}")

        logger.info("Ресурсы БД, Redis и HTTP-пула освобождены.")


app = FastAPI(
    title="AI Manager SaaS for amoCRM",
    description=(
        "Масштабируемый бэкенд ИИ-менеджера для amoCRM на базе Google Gemini 3.1 Flash Lite. "
        "Асинхронная доставка ответов в чаты мессенджеров через скрытое поле сделки и Salesbot."
    ),
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение роутеров
app.include_router(admin_router)
app.include_router(accounts_router)
app.include_router(webhook_router)


@app.get("/health", tags=["Health"])
async def health_check():
    """Проверка жизнеспособности сервиса, PostgreSQL и Redis"""
    db_ok = False
    redis_ok = False

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.error(f"Health check DB failed: {e}")

    try:
        r = await debounce_service.get_redis()
        redis_ok = await r.ping()
    except Exception as e:
        logger.error(f"Health check Redis failed: {e}")

    overall_ok = db_ok and redis_ok
    return {
        "status": "healthy" if overall_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "gemini_api_key_configured": bool(settings.GEMINI_API_KEY),
        "base_url": settings.BASE_URL
    }

