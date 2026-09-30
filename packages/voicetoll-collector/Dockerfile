FROM python:3.11-slim

WORKDIR /app
COPY packages/voicetoll-collector /app/packages/voicetoll-collector
RUN pip install --no-cache-dir "/app/packages/voicetoll-collector[postgres]"

EXPOSE 4319
CMD ["voicetoll-collector", "serve", "--host", "0.0.0.0", "--port", "4319"]
