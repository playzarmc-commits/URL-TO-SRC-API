FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py capture.py pack.py keystore.py admin.py ./
ENV PORT=10000
# ONE process (jobs live in memory), many threads (SSE + sync requests)
CMD gunicorn -k gthread -w 1 --threads 32 --timeout 300 -b 0.0.0.0:$PORT app:app
