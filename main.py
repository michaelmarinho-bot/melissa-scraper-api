#!/usr/bin/env python3
"""
Melissa Scraper API - Versão Lite (sem Playwright)
Usa requests + BeautifulSoup + OpenAI para coleta de dados.
Deploy gratuito no Render.com sem Docker.

Endpoints:
  POST /scrape/superapp   - Coleta dados do SuperApp Layers via API/session
  POST /scrape/classroom  - Coleta tarefas do Google Classroom via API
  POST /scrape/roteiro    - Coleta dados do Roteiro de Estudos
  POST /scrape/all        - Executa todos de uma vez
  GET  /health            - Health check
"""

import os
import json
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import requests as http_requests
from bs4 import BeautifulSoup

# ============================================================
# CONFIGURAÇÃO
# ============================================================
API_SECRET = os.environ.get("MELISSA_API_SECRET", "trocar-por-uma-chave-segura")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
SUPERAPP_EMAIL = os.environ.get("SUPERAPP_EMAIL", "")
SUPERAPP_PASSWORD = os.environ.get("SUPERAPP_PASSWORD", "")
CLASSROOM_EMAIL = os.environ.get("CLASSROOM_EMAIL", "")
CLASSROOM_PASSWORD = os.environ.get("CLASSROOM_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("melissa-scraper")


# ============================================================
# MODELOS
# ============================================================
class ScrapeRequest(BaseModel):
    email: str = ""
    password: str = ""
    aluna: str = "Melissa Majado Marinho"
    turma: str = "8E"
    data_referencia: str = ""
    google_token: str = ""  # Token OAuth do Google (passado pelo n8n)


class ScrapeResponse(BaseModel):
    status: str
    fonte: str
    data_coleta: str
    dados: dict
    erros: list = []


# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(
    title="Melissa Scraper API Lite",
    description="API leve de scraping para a Agente Melissa - sem Playwright",
    version="2.0.0"
)


def verificar_auth(authorization: str = Header(None)):
    if not authorization or authorization.replace("Bearer ", "") != API_SECRET:
        raise HTTPException(status_code=401, detail="Chave de API inválida")


def chamar_openai(system_prompt: str, user_prompt: str, model: str = "gpt-4.1-mini") -> str:
    """Chama a OpenAI API para processar/interpretar dados."""
    if not OPENAI_API_KEY:
        return '{"erro": "OPENAI_API_KEY não configurada"}'
    
    try:
        resp = http_requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 4000
            },
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Erro OpenAI: {e}")
        return json.dumps({"erro": str(e)})


# ============================================================
# SCRAPING - SUPERAPP LAYERS
# ============================================================
def scrape_superapp(req: ScrapeRequest) -> dict:
    """
    Coleta dados do SuperApp Layers usando requests + session.
    Tenta login via formulário e extrai dados das páginas.
    Se falhar, usa OpenAI para sugerir abordagem alternativa.
    """
    dados = {"conteudos": [], "notas": [], "registros": [], "erros": []}
    email = req.email or SUPERAPP_EMAIL
    password = req.password or SUPERAPP_PASSWORD
    
    if not email or not password:
        dados["erros"].append("Credenciais do SuperApp não configuradas. Configure SUPERAPP_EMAIL e SUPERAPP_PASSWORD.")
        return dados
    
    session = http_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    
    try:
        # Tentar acessar a página de login
        logger.info("Acessando SuperApp Layers...")
        login_page = session.get("https://app.layers.education", timeout=15)
        
        # Tentar encontrar API de login
        # Layers geralmente usa uma API REST para autenticação
        login_endpoints = [
            "https://app.layers.education/api/auth/login",
            "https://app.layers.education/api/v1/auth/login",
            "https://api.layers.education/v1/auth/login",
            "https://app.layers.education/auth/login",
        ]
        
        login_success = False
        for endpoint in login_endpoints:
            try:
                resp = session.post(endpoint, json={
                    "email": email,
                    "password": password
                }, timeout=15)
                
                if resp.status_code in [200, 201]:
                    login_data = resp.json()
                    logger.info(f"Login bem-sucedido via {endpoint}")
                    
                    # Extrair token se disponível
                    token = login_data.get("token") or login_data.get("access_token") or login_data.get("data", {}).get("token", "")
                    if token:
                        session.headers["Authorization"] = f"Bearer {token}"
                    
                    login_success = True
                    dados["login"] = "success"
                    break
                    
            except Exception as e:
                continue
        
        if not login_success:
            # Tentar login via formulário HTML
            try:
                soup = BeautifulSoup(login_page.text, "html.parser")
                form = soup.find("form")
                if form:
                    action = form.get("action", "")
                    resp = session.post(
                        action if action.startswith("http") else f"https://app.layers.education{action}",
                        data={"email": email, "password": password},
                        timeout=15
                    )
                    if resp.status_code in [200, 201, 302]:
                        login_success = True
                        dados["login"] = "success_form"
            except Exception as e:
                dados["erros"].append(f"Login form: {str(e)}")
        
        if login_success:
            # Tentar acessar páginas de dados
            paginas = {
                "conteudos": [
                    "https://app.layers.education/api/conteudo-de-aula",
                    "https://app.layers.education/conteudo-de-aula",
                ],
                "notas": [
                    "https://app.layers.education/api/notas-academicas",
                    "https://app.layers.education/notas-academicas",
                ],
                "registros": [
                    "https://app.layers.education/api/registros-academicos",
                    "https://app.layers.education/registros-academicos",
                ]
            }
            
            for tipo, urls in paginas.items():
                for url in urls:
                    try:
                        resp = session.get(url, timeout=15)
                        if resp.status_code == 200:
                            # Tentar parsear como JSON
                            try:
                                dados[tipo] = resp.json()
                                break
                            except:
                                # Parsear como HTML
                                soup = BeautifulSoup(resp.text, "html.parser")
                                
                                # Extrair tabelas
                                tables = soup.find_all("table")
                                if tables:
                                    for table in tables:
                                        rows = []
                                        for tr in table.find_all("tr"):
                                            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                                            if cells:
                                                rows.append(cells)
                                        dados[tipo].extend(rows)
                                    break
                                else:
                                    # Extrair texto geral
                                    text = soup.get_text(separator="\n", strip=True)
                                    if len(text) > 50:
                                        dados[tipo] = [{"texto_raw": text[:5000]}]
                                        break
                    except Exception as e:
                        continue
        else:
            dados["erros"].append("Não foi possível fazer login no SuperApp. Verifique credenciais.")
            
            # Usar OpenAI para sugerir abordagem
            ai_resp = chamar_openai(
                "Você é um especialista em web scraping e APIs educacionais.",
                f"O login no SuperApp Layers (layers.education) falhou com email={email}. "
                "Sugira abordagens alternativas para extrair dados de notas, conteúdos e registros acadêmicos. "
                "Considere: APIs públicas do Layers, integrações OAuth, ou métodos alternativos."
            )
            dados["sugestao_ai"] = ai_resp
    
    except Exception as e:
        dados["erros"].append(f"Erro geral SuperApp: {str(e)}")
    
    return dados


# ============================================================
# SCRAPING - GOOGLE CLASSROOM (via API)
# ============================================================
def scrape_classroom(req: ScrapeRequest) -> dict:
    """
    Coleta dados do Google Classroom usando a API oficial do Google.
    Requer um token OAuth passado pelo n8n (que já tem credenciais Google).
    """
    dados = {"turmas": [], "tarefas": [], "erros": []}
    
    google_token = req.google_token
    
    if not google_token:
        dados["erros"].append(
            "Token Google OAuth não fornecido. "
            "O n8n deve passar o token via campo 'google_token'. "
            "No n8n, use o nó 'Google Classroom' ou extraia o token das credenciais Google."
        )
        
        # Sugestão alternativa via OpenAI
        ai_resp = chamar_openai(
            "Você é um especialista em Google APIs e n8n.",
            "Preciso acessar o Google Classroom para extrair tarefas de uma aluna. "
            "O n8n já tem credenciais Google OAuth configuradas. "
            "Como posso extrair o access_token das credenciais do n8n e passá-lo para uma API externa? "
            "Ou existe uma forma melhor de usar a API do Google Classroom diretamente no n8n?"
        )
        dados["sugestao_ai"] = ai_resp
        return dados
    
    headers = {"Authorization": f"Bearer {google_token}"}
    base_url = "https://classroom.googleapis.com/v1"
    
    try:
        # 1. Listar turmas
        logger.info("Listando turmas do Google Classroom...")
        resp = http_requests.get(f"{base_url}/courses", headers=headers, timeout=15,
                                 params={"courseStates": "ACTIVE"})
        
        if resp.status_code == 200:
            courses = resp.json().get("courses", [])
            dados["turmas"] = [{"id": c["id"], "nome": c.get("name", ""), "secao": c.get("section", "")} for c in courses]
            
            # 2. Para cada turma, listar tarefas
            for course in courses:
                course_id = course["id"]
                course_name = course.get("name", "")
                
                try:
                    # Listar courseWork (tarefas)
                    resp_work = http_requests.get(
                        f"{base_url}/courses/{course_id}/courseWork",
                        headers=headers, timeout=15,
                        params={"orderBy": "dueDate desc", "pageSize": 20}
                    )
                    
                    if resp_work.status_code == 200:
                        works = resp_work.json().get("courseWork", [])
                        for work in works:
                            tarefa = {
                                "turma": course_name,
                                "titulo": work.get("title", ""),
                                "descricao": work.get("description", ""),
                                "tipo": work.get("workType", ""),
                                "estado": work.get("state", ""),
                                "dataEntrega": "",
                                "materiais": []
                            }
                            
                            # Extrair data de entrega
                            due = work.get("dueDate", {})
                            if due:
                                tarefa["dataEntrega"] = f"{due.get('day', '')}/{due.get('month', '')}/{due.get('year', '')}"
                            
                            # Extrair materiais/anexos
                            materials = work.get("materials", [])
                            for mat in materials:
                                if "driveFile" in mat:
                                    tarefa["materiais"].append({
                                        "tipo": "drive",
                                        "titulo": mat["driveFile"].get("driveFile", {}).get("title", ""),
                                        "link": mat["driveFile"].get("driveFile", {}).get("alternateLink", "")
                                    })
                                elif "link" in mat:
                                    tarefa["materiais"].append({
                                        "tipo": "link",
                                        "titulo": mat["link"].get("title", ""),
                                        "url": mat["link"].get("url", "")
                                    })
                            
                            dados["tarefas"].append(tarefa)
                    
                    # Listar submissions (entregas da aluna)
                    resp_sub = http_requests.get(
                        f"{base_url}/courses/{course_id}/courseWork/-/studentSubmissions",
                        headers=headers, timeout=15,
                        params={"pageSize": 50}
                    )
                    
                    if resp_sub.status_code == 200:
                        subs = resp_sub.json().get("studentSubmissions", [])
                        # Processar submissions se necessário
                        
                except Exception as e:
                    dados["erros"].append(f"Turma {course_name}: {str(e)}")
        
        elif resp.status_code == 401:
            dados["erros"].append("Token Google expirado. O n8n precisa renovar o token OAuth.")
        elif resp.status_code == 403:
            dados["erros"].append("Sem permissão para acessar Google Classroom. Verifique os escopos OAuth.")
        else:
            dados["erros"].append(f"Erro API Classroom: {resp.status_code} - {resp.text[:200]}")
    
    except Exception as e:
        dados["erros"].append(f"Erro geral Classroom: {str(e)}")
    
    return dados


# ============================================================
# SCRAPING - ROTEIRO DE ESTUDOS
# ============================================================
def scrape_roteiro(req: ScrapeRequest) -> dict:
    """
    Acessa roteiro.jardim.li e extrai datas de provas.
    Usa requests + BeautifulSoup + OpenAI.
    """
    dados = {"provas": [], "conteudos": [], "html_raw": "", "erros": []}
    
    try:
        logger.info("Acessando roteiro.jardim.li...")
        resp = http_requests.get("https://roteiro.jardim.li", timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        
        # Remover scripts e styles
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        
        # Extrair texto limpo
        texto = soup.get_text(separator="\n", strip=True)
        dados["html_raw"] = texto[:10000]
        
        # Extrair datas com regex
        padroes = [
            r'(AO\d?|AD\d?|OIA|Prova|Avaliação|Simulado|Teste)[^\n]{0,100}?(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)',
            r'(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)[^\n]{0,100}?(AO\d?|AD\d?|OIA|Prova|Avaliação|Simulado|Teste)',
        ]
        
        encontrados = set()
        for padrao in padroes:
            matches = re.finditer(padrao, texto, re.IGNORECASE)
            for m in matches:
                ctx = m.group(0)[:200]
                if ctx not in encontrados:
                    encontrados.add(ctx)
                    dados["provas"].append({
                        "contexto": ctx,
                        "match_groups": [m.group(1), m.group(2)]
                    })
        
        # Extrair tabelas
        tables = soup.find_all("table")
        for table in tables:
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if rows:
                dados["conteudos"].append({"tabela": rows})
        
        # Usar OpenAI para interpretar o conteúdo completo
        if OPENAI_API_KEY and len(texto) > 50:
            logger.info("Usando OpenAI para interpretar roteiro...")
            ai_resp = chamar_openai(
                system_prompt=(
                    "Você é um assistente que extrai informações acadêmicas de páginas web. "
                    "Extraia TODAS as datas de provas, avaliações (AO, AD, OIA), trabalhos e atividades. "
                    "Para cada item, identifique: tipo (AO/AD/OIA/Prova/Trabalho), matéria, data (DD/MM/YYYY), descrição. "
                    "Padronize: 'Matemática 1' = 'Álgebra', 'Matemática 2' = 'Geometria'. "
                    "Retorne APENAS JSON válido: {\"provas\": [{\"tipo\": \"\", \"materia\": \"\", \"data\": \"\", \"descricao\": \"\"}]}"
                ),
                user_prompt=f"Texto extraído do roteiro de estudos (roteiro.jardim.li):\n\n{texto[:6000]}"
            )
            
            try:
                # Tentar extrair JSON da resposta
                json_match = re.search(r'\{[\s\S]*\}', ai_resp)
                if json_match:
                    dados["provas_ai"] = json.loads(json_match.group(0))
                else:
                    dados["provas_ai_raw"] = ai_resp
            except json.JSONDecodeError:
                dados["provas_ai_raw"] = ai_resp
        
        dados["texto_length"] = len(texto)
        logger.info(f"Roteiro: {len(texto)} chars, {len(dados['provas'])} provas regex, tabelas: {len(dados['conteudos'])}")
    
    except Exception as e:
        dados["erros"].append(f"Erro ao acessar roteiro: {str(e)}")
    
    return dados


# ============================================================
# ENDPOINTS
# ============================================================
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "melissa-scraper-lite",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat(),
        "openai_configured": bool(OPENAI_API_KEY),
        "superapp_configured": bool(SUPERAPP_EMAIL),
    }


@app.get("/")
def root():
    return {
        "service": "Melissa Scraper API Lite",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": ["/scrape/superapp", "/scrape/classroom", "/scrape/roteiro", "/scrape/all"]
    }


@app.post("/scrape/superapp", response_model=ScrapeResponse)
def endpoint_superapp(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping SuperApp para {req.aluna}")
    dados = scrape_superapp(req)
    return ScrapeResponse(
        status="success" if not dados.get("erros") else "partial",
        fonte="superapp",
        data_coleta=datetime.now().isoformat(),
        dados=dados,
        erros=dados.get("erros", [])
    )


@app.post("/scrape/classroom", response_model=ScrapeResponse)
def endpoint_classroom(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping Classroom para {req.aluna}")
    dados = scrape_classroom(req)
    return ScrapeResponse(
        status="success" if not dados.get("erros") else "partial",
        fonte="classroom",
        data_coleta=datetime.now().isoformat(),
        dados=dados,
        erros=dados.get("erros", [])
    )


@app.post("/scrape/roteiro", response_model=ScrapeResponse)
def endpoint_roteiro(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info("Scraping Roteiro de Estudos")
    dados = scrape_roteiro(req)
    return ScrapeResponse(
        status="success" if not dados.get("erros") else "partial",
        fonte="roteiro",
        data_coleta=datetime.now().isoformat(),
        dados=dados,
        erros=dados.get("erros", [])
    )


@app.post("/scrape/all")
def endpoint_all(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping completo para {req.aluna}")
    
    return {
        "status": "success",
        "data_coleta": datetime.now().isoformat(),
        "superapp": scrape_superapp(req),
        "classroom": scrape_classroom(req),
        "roteiro": scrape_roteiro(req)
    }


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
