# Melissa Scraper API v3.6.0 — Guia de Testes

**Data:** 15/03/2026  
**Versão:** 3.6.0  
**Deploy:** Render (auto-deploy via push no GitHub)  
**RAM:** 1GB  

---

## O que foi corrigido (v3.6.0)

O problema era que cada download de arquivo abria uma **nova aba do browser** (`context.new_page()`) sem fechar a anterior. Com 1GB de RAM no Render, o processo crashava ao tentar baixar o segundo arquivo.

**Antes (v3.5.3):**
- Arquivo 1: abre aba 1 → baixa → NÃO fecha aba 1
- Arquivo 2: abre aba 2 → CRASH (memória estourada com 2 abas + base64 na RAM)

**Depois (v3.6.0):**
- Cria UMA aba de download antes do loop
- Arquivo 1: reutiliza aba → baixa → `gc.collect()` → `del content`
- Arquivo 2: reutiliza MESMA aba → baixa → `gc.collect()` → `del content`
- Fecha a aba somente no final

**Mudanças adicionais:**
- `del content` após `base64.b64encode()` para liberar buffer imediatamente
- `gc.collect()` após cada download
- Args extras do Chromium: `--single-process`, `--disable-extensions`, `--disable-background-networking`

---

## Endpoints da API

### Base URL
```
https://melissa-scraper-api.onrender.com
```

### Autenticação
Header: `Authorization: Bearer {MELISSA_API_SECRET}`

### 1. Health Check
```
GET /health
```
Deve retornar `version: "3.6.0"` após o deploy.

### 2. Listar Turmas do Classroom
```
POST /scrape/classroom/turmas
```
Body (opcional):
```json
{
  "email": "melissa.marinho@liceujardim.g12.br",
  "password": "elvis!!1"
}
```
Retorna `job_id` → consultar com:
```
GET /scrape/classroom/turmas/job/{job_id}
```

### 3. Coletar 1 Turma (materiais + downloads)
```
POST /scrape/classroom/turma
```
Body:
```json
{
  "turma_nome": "8 E - Ciências",
  "turma_link": "https://classroom.google.com/c/NzM2NzI5MTk2MDk2",
  "arquivos_existentes": []
}
```
Retorna `job_id` → consultar com:
```
GET /scrape/classroom/turmas/job/{job_id}
```

### 4. Roteiro (já funciona)
```
POST /scrape/roteiro
```

### 5. SuperApp (já funciona)
```
POST /scrape/superapp
```

---

## Roteiro de Testes para Amanhã

### Teste 1: Verificar deploy
```bash
curl https://melissa-scraper-api.onrender.com/health
```
**Esperado:** `"version": "3.6.0"`

### Teste 2: Listar turmas
```bash
curl -X POST https://melissa-scraper-api.onrender.com/scrape/classroom/turmas \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {API_KEY}" \
  -d '{}'
```
Esperar 30s, depois:
```bash
curl https://melissa-scraper-api.onrender.com/scrape/classroom/turmas/job/{JOB_ID} \
  -H "Authorization: Bearer {API_KEY}"
```
**Esperado:** Lista de turmas com nomes e links.

### Teste 3: Baixar arquivos de 1 turma (TESTE PRINCIPAL)
```bash
curl -X POST https://melissa-scraper-api.onrender.com/scrape/classroom/turma \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {API_KEY}" \
  -d '{
    "turma_nome": "8 E - Ciências",
    "turma_link": "https://classroom.google.com/c/NzM2NzI5MTk2MDk2",
    "arquivos_existentes": []
  }'
```
Esperar 60-90s, depois consultar o job.

**Esperado:**
- 2+ PDFs baixados com sucesso (Slides Ciências + AD GABARITO)
- Sem crash no segundo arquivo
- `resumo.total_arquivos_novos >= 2`
- `resumo.total_erros == 0`

### Teste 4: Verificar economia de memória nos logs
No dashboard do Render, verificar:
- Logs devem mostrar: `"Aba de download criada (será reutilizada para todos os arquivos)"`
- Logs devem mostrar: `"gc.collect() após download 1"`, `"gc.collect() após download 2"`
- Logs devem mostrar: `"Aba de download fechada"`
- **NÃO** deve ter restart/crash do processo

---

## Arquivos já testados (turma 8 E - Ciências)

| Arquivo | Tipo | Tamanho | Status v3.5.3 | Status v3.6.0 |
|---------|------|---------|---------------|---------------|
| Slides Ciências - Capítulo 1 | PDF (Drive) | 1.9MB | OK | A testar |
| AD GABARITO MANHÃ - CIÊNCIAS | PDF (Drive) | ~1-2MB | CRASH | A testar |
| (outros anexos da turma) | Variados | Variados | Não testados | A testar |

---

## Status dos 3 Scrapers

| Scraper | Status | Último Teste | Resultado |
|---------|--------|--------------|-----------|
| Roteiro | Funcionando | 14/03/2026 | 18 provas, 0 erros |
| SuperApp | Funcionando | 14/03/2026 | 14 matérias, 39 avaliações, 8 registros, 0 erros |
| Classroom V3 | Corrigido (a testar) | 15/03/2026 | Login OK, coleta OK, download corrigido |

---

## Plano B (se v3.6.0 ainda crashar)

Se mesmo com a reutilização de aba o download ainda crashar:

1. **Dividir em endpoints separados:**
   - `/scrape/classroom/turmas` — lista turmas (já existe)
   - `/scrape/classroom/turma` — coleta materiais sem download
   - `/scrape/classroom/download` — baixa 1 arquivo por vez (novo endpoint)

2. **O n8n orquestraria:**
   - Chamar `/turma` para listar anexos
   - Para cada anexo, chamar `/download` com o file_id
   - Upload individual no Drive

3. **Upgrade de RAM:** Render permite 2GB ($25/mês)

---

## Credenciais

| Serviço | Email | Senha |
|---------|-------|-------|
| Google Classroom | melissa.marinho@liceujardim.g12.br | elvis!!1 |
| Google Drive (admin) | michael.marinho@gmail.com | (configurado no n8n) |

---

## Arquivos do Projeto

| Arquivo | Descrição |
|---------|-----------|
| `main.py` | FastAPI main — endpoints Roteiro, SuperApp, Classroom legacy |
| `classroom_v3.py` | Classroom V3 — turmas + turma com download (CORRIGIDO v3.6.0) |
| `roteiro_scraper.py` | Scraper do Roteiro — funcionando |
| `superapp_scraper.py` | Scraper do SuperApp — funcionando |
| `Dockerfile` | Docker config para Render |
| `requirements.txt` | Dependências Python |

---

## Git

```
Repositório: https://github.com/michaelmarinho-bot/melissa-scraper-api
Branch: main
Último commit: fix: reutilizar mesma aba para downloads - economia de memória v3.6.0
Auto-deploy: Render faz deploy automático a cada push no main
```
