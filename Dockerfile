FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm gcc g++ git ca-certificates libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# Usuario sin privilegios: el código que corre el bot no es root
RUN useradd -m bot && chown -R bot /app
USER bot

CMD ["python", "bot.py"]
