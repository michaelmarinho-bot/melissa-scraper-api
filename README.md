# Melissa Scraper API Lite

API FastAPI leve para coleta de dados escolares da Agente Melissa.

## Endpoints

- `GET /health` - Health check
- `POST /scrape/roteiro` - Coleta roteiro de estudos
- `POST /scrape/superapp` - Coleta dados do SuperApp Layers
- `POST /scrape/classroom` - Coleta dados do Google Classroom
- `POST /scrape/all` - Coleta todos os dados

## Deploy no Render

1. Conecte este repositório ao Render.com
2. Configure as variáveis de ambiente:
   - `OPENAI_API_KEY` - Chave da API OpenAI
   - `MELISSA_API_SECRET` - Chave de autenticação da API
   - `SUPERAPP_EMAIL` - Email do SuperApp (opcional)
   - `SUPERAPP_PASSWORD` - Senha do SuperApp (opcional)
3. Deploy automático!

## Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `OPENAI_API_KEY` | Sim | Chave API da OpenAI |
| `MELISSA_API_SECRET` | Sim | Chave para autenticar requests |
| `SUPERAPP_EMAIL` | Não | Email de login no SuperApp |
| `SUPERAPP_PASSWORD` | Não | Senha do SuperApp |
