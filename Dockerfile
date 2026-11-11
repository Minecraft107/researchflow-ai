# One image: the React build is served by the FastAPI backend.
FROM node:24-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
# DejaVu fonts give the PDF export full Unicode coverage.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=frontend /frontend/dist /app/frontend/dist
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser
ENV DATABASE_URL=sqlite:////data/researchflow.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
# One worker: runs and the SSE feed live in process memory (see README, Known limitations).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
