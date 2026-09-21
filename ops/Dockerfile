# Образ торгового процесса.
#
# Три решения, каждое из ТЗ:
#
#   * база с фиксированным дайджестом, а не тегом latest — одинаковое
#     поведение на любом хосте, включая версию Python и libc (docs/15, 15.2);
#   * зависимости ставятся по хэшам и отдельным слоем — пересборка при правке
#     кода не тянет установку заново;
#   * процесс работает от непривилегированного пользователя, секретов
#     в образе нет: ключи приходят только через окружение (docs/10, 10.2).

FROM python:3.12-slim-bookworm@sha256:8bfe1b3b1b1e3b6f5a0bcd76d0cdb5cd0e7cff11a4a1f0e2b1f4d3b3b2e1f0a9 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# --- зависимости отдельным слоем -------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- код ---------------------------------------------------------------
COPY src/ ./src/
COPY config/ ./config/

# Непривилегированный пользователь. Каталог данных — единственное место,
# куда процессу нужна запись: перенос на другой хост = копирование /app/data.
RUN useradd --system --uid 10001 --create-home cashmash \
    && mkdir -p /app/data \
    && chown -R cashmash:cashmash /app/data
USER cashmash

ENV PYTHONPATH=/app/src

# Признак жизни для оркестратора. Процесс, переставший писать heartbeat,
# считается мёртвым, даже если его PID ещё существует.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import json,sys,time; \
hb=json.load(open('/app/data/heartbeat_trader.json')); \
sys.exit(0 if time.time()*1000-hb['ts_ms'] < 60000 else 1)"

ENTRYPOINT ["python", "-m", "cashmash.app"]
CMD ["--config", "config/testnet.yaml"]
