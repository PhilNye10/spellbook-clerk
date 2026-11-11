FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
ENV DATABASE_PATH=/data/mtg_tables.db
CMD ["python", "-m", "mtg_table_bot.main"]
