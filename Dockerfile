FROM python:3.11-slim

WORKDIR /app

# System deps (optional but helpful for SSL/DNS reliability)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Start the long-polling bot
CMD ["python", "bot.py"]
