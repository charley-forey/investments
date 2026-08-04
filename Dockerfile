FROM python:3.12-slim

# The scheduler builds every cron trigger against America/New_York. slim images
# ship no zoneinfo database, so without this APScheduler raises at startup.
ENV TZ=America/New_York \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY . .
RUN pip install tzdata && pip install -e ".[web]"

# Mutable state lives on the mounted volume, never the image layer. The volume is
# not present at build time, so nothing here may write to it — cmd_serve seeds it
# on first boot instead.
ENV TRADING_ROOT=/state

CMD ["python", "-m", "trading", "serve"]
