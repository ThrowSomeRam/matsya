# Owner-view API (platform/src/matsya_platform/api). Serves precomputed payloads mounted at /payloads.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29"
COPY platform/src/matsya_platform/__init__.py /app/matsya_platform/__init__.py
COPY platform/src/matsya_platform/api /app/matsya_platform/api
ENV PYTHONPATH=/app MATSYA_PAYLOAD_DIR=/payloads
EXPOSE 8000
CMD ["uvicorn", "matsya_platform.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
