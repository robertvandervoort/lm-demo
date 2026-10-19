FROM python:3.12-alpine
WORKDIR /app
COPY app/ /app/
ENV PORT=8089 CONFIG_FILE=/app/config/demo.json
EXPOSE 8089
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:8089/api/health >/dev/null || exit 1
CMD ["python3", "-u", "/app/server.py"]
