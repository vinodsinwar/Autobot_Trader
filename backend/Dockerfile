FROM python:3.12-slim

# ffmpeg for live-audio capture; yt-dlp is a python dep and upgraded via pip
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/autobot
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[postgres]"

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
