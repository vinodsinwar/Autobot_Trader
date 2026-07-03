# build the dashboard
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
# vite outDir is ../backend/static relative to the frontend dir; keep it local here
RUN npx vite build --outDir /build/dist

# runtime
FROM python:3.12-slim

# ffmpeg for live-audio capture; yt-dlp is a python dep and upgraded via pip
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/autobot
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir ".[postgres]"
COPY --from=frontend /build/dist ./static

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
