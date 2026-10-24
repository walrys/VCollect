FROM python:3.12-slim

# yt-dlp needs a JavaScript runtime to solve YouTube's challenges; Deno is its default.
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore \
    -r requirements.txt -r requirements-dev.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
EXPOSE 8000
ENTRYPOINT ["sh", "/srv/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
