import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, AsyncSessionLocal
from app.services.debounce_service import debounce_service
from app.api.routes_accounts import router as accounts_router
from app.api.routes_webhook import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("Main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Инициализация AI Manager Backend...")
    # Проверка подключения к БД
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        logger.info("Подключение к PostgreSQL успешно.")
    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")

    # Проверка подключения к Redis
    try:
        r = await debounce_service.get_redis()
        pong = await r.ping()
        logger.info(f"Подключение к Redis успешно: {pong}")
    except Exception as e:
        logger.error(f"Ошибка подключения к Redis: {e}")

    yield

    logger.info("Завершение работы AI Manager Backend...")
    if debounce_service.redis:
        await debounce_service.redis.close()
    await engine.dispose()
    logger.info("Ресурсы БД и Redis освобождены.")


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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api.routes_admin import router as admin_router

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

