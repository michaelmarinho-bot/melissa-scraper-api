FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Instalar Xvfb para display virtual (evita CAPTCHA do Google no login)
RUN apt-get update && apt-get install -y \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY . .

# Porta padrão do Render
ENV PORT=8000

# Expor porta
EXPOSE 8000

# Iniciar com Xvfb para ter display virtual (evita detecção de bot pelo Google)
CMD ["sh", "-c", "xvfb-run --auto-servernum --server-args='-screen 0 1280x720x24' uvicorn main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 300"]
