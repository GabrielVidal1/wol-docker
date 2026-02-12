# Use lightweight Python image
FROM python:3.12-slim

WORKDIR /app

# Install required packages (wakeonlan needs network access)
RUN pip install --no-cache-dir \
    fastapi==0.115.6 uvicorn[standard]==0.34.0 wakeonlan==3.0 httpx==0.28.1 python-dotenv==1.0.1

COPY main.py .

# Set default ports
EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]