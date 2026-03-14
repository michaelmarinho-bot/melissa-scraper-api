# Melissa Scraper API v3.0 (Playwright Edition)

API FastAPI com Playwright para coleta de dados escolares da Agente Melissa.
Usa navegação real via browser headless (Chromium) para acessar portais que requerem login.

## Endpoints

- `GET /health` - Health check
- `POST /scrape/classroom` - Coleta turmas, atividades e materiais do Google Classroom
- `POST /scrape/superapp` - Coleta dados do SuperApp Layers (notas, conteúdos, registros)
- `POST /scrape/roteiro` - Coleta dados do Roteiro de Estudos (provas AO/AD)
- `POST /scrape/all` - Coleta todos os dados (execução paralela)

## Deploy no Render (Docker)

1. Conecte este repositório ao Render.com
2. Selecione **Docker** como Environment
3. Configure as variáveis de ambiente:
   - `MELISSA_API_SECRET` - Chave de autenticação da API
   - `MELISSA_EMAIL` - Email da Melissa (melissa.marinho@liceujardim.g12.br)
   - `MELISSA_PASSWORD` - Senha da Melissa
   - `SUPERAPP_EMAIL` - Email do SuperApp (se diferente)
   - `SUPERAPP_PASSWORD` - Senha do SuperApp (se diferente)
4. Deploy automático!

## Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `MELISSA_API_SECRET` | Sim | Chave para autenticar requests |
| `MELISSA_EMAIL` | Sim | Email de login nos portais |
| `MELISSA_PASSWORD` | Sim | Senha de login nos portais |
| `SUPERAPP_EMAIL` | Não | Email do SuperApp (se diferente) |
| `SUPERAPP_PASSWORD` | Não | Senha do SuperApp (se diferente) |

## Arquitetura

```
n8n (Orquestrador)
  │
  ├── HTTP Request → POST /scrape/classroom
  │                    → Playwright navega no Classroom
  │                    → Retorna turmas, atividades, arquivos
  │
  ├── HTTP Request → POST /scrape/superapp
  │                    → Playwright navega no SuperApp
  │                    → Retorna notas, conteúdos, registros
  │
  └── HTTP Request → POST /scrape/roteiro
                       → Playwright navega no Roteiro
                       → Retorna provas AO/AD/Inglês
```
