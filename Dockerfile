FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver nodejs npm fonts-noto-cjk ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt package.json package-lock.json ./
RUN pip install --no-cache-dir -r requirements.txt && npm ci --omit=dev --ignore-scripts
COPY engine.py controller.py server.py push_service.py push.mjs device_auth.py auth_device.mjs ./
COPY static ./static
RUN useradd --create-home --uid 10001 seatapp && mkdir /app/data && chown seatapp:seatapp /app/data
USER seatapp
ENV HOST=0.0.0.0 PORT=8080 SECURE_COOKIES=1 DEMO_ONLY=1 CHROME_BINARY=/usr/bin/chromium CHROME_DRIVER=/usr/bin/chromedriver SE_AVOID_STATS=true
EXPOSE 8080
CMD ["python", "server.py"]
