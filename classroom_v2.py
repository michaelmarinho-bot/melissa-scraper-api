"""
Classroom V2 — Endpoints fragmentados para coleta por turma
Versão: 1.0.0

Endpoints:
  POST /scrape/classroom/turmas  - Lista todas as turmas do Classroom
  POST /scrape/classroom/turma   - Coleta materiais e arquivos de 1 turma (com download)

Arquitetura:
  - Cada chamada abre e fecha o browser (1 turma = 1 browser = pouca memória)
  - O n8n faz o inventário no Drive e orquestra as chamadas
  - A API só faz scraping + download, retorna arquivos em base64
"""

import os
import re
import gc
import base64
import asyncio
import logging
import traceback
import uuid
import tempfile
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel

# Reutilizar config do main
API_SECRET = os.environ.get("MELISSA_API_SECRET", "") or os.environ.get("MELISSA_API_KEY", "trocar-por-uma-chave-segura")
MELISSA_EMAIL = os.environ.get("MELISSA_EMAIL", "melissa.marinho@liceujardim.g12.br")
MELISSA_PASSWORD = os.environ.get("MELISSA_PASSWORD", "elvis!!1")

logger = logging.getLogger("melissa-scraper")

# Router FastAPI
router = APIRouter(prefix="/scrape/classroom", tags=["Classroom V2"])

# Jobs store (compartilhado via import no main)
classroom_jobs: Dict[str, Dict[str, Any]] = {}


# ============================================================
# MODELOS
# ============================================================
class TurmasRequest(BaseModel):
    email: str = ""
    password: str = ""


class TurmaRequest(BaseModel):
    email: str = ""
    password: str = ""
    turma_link: str = ""          # Link direto da turma (ex: https://classroom.google.com/c/XXX)
    turma_nome: str = ""          # Nome da turma (para log)
    arquivos_existentes: List[str] = []  # Lista de nomes de arquivos já no Drive (inventário)


class ArquivoDownload(BaseModel):
    nome: str
    file_id: str
    tamanho: int
    conteudo_base64: str
    turma: str
    topico: str
    material: str


# ============================================================
# HELPERS
# ============================================================
def verificar_auth(authorization: str = Header(None)):
    if not API_SECRET:
        return
    if not authorization or authorization.replace("Bearer ", "") != API_SECRET:
        raise HTTPException(status_code=401, detail="Chave de API inválida")


def create_classroom_job(fonte: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    classroom_jobs[job_id] = {
        "job_id": job_id,
        "status": "processing",
        "fonte": fonte,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "result": None
    }
    # Limpar jobs antigos
    if len(classroom_jobs) > 30:
        oldest = sorted(classroom_jobs.keys(), key=lambda k: classroom_jobs[k]["created_at"])[:10]
        for k in oldest:
            del classroom_jobs[k]
    return job_id


# ============================================================
# LOGIN GOOGLE (cópia local para não depender do main)
# ============================================================
async def google_login(page, email: str, password: str, max_retries: int = 3):
    """Login no Google com tratamento do campo hidden decoy."""
    for attempt in range(max_retries):
        try:
            logger.info(f"[ClassroomV2] Login tentativa {attempt + 1}/{max_retries}...")
            await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            current_url = page.url
            if "myaccount.google.com" in current_url or "classroom.google.com" in current_url:
                logger.info("[ClassroomV2] Já logado!")
                return True

            # Email
            email_input = page.locator('input[type="email"]')
            await email_input.wait_for(state="visible", timeout=10000)
            await email_input.fill(email)
            await page.wait_for_timeout(500)
            await page.locator('#identifierNext button').click()
            await page.wait_for_timeout(4000)

            # Senha (ignorar campo hidden decoy do Google)
            password_input = page.locator('input[type="password"]:not([aria-hidden="true"]):not([tabindex="-1"])')
            await password_input.wait_for(state="visible", timeout=15000)
            await password_input.fill(password)
            await page.wait_for_timeout(500)
            await page.locator('#passwordNext button').click()
            await page.wait_for_timeout(5000)

            current_url = page.url
            if "accounts.google.com" not in current_url:
                logger.info(f"[ClassroomV2] Login OK! URL: {current_url}")
                return True

            # Verificar CAPTCHA real
            has_captcha = await page.locator('iframe[title*="recaptcha"]:visible').count() > 0
            if has_captcha:
                logger.error("[ClassroomV2] CAPTCHA detectado!")
                return False

            await page.wait_for_timeout(5000)
            current_url = page.url
            if "accounts.google.com" not in current_url:
                logger.info(f"[ClassroomV2] Login OK (redirect)! URL: {current_url}")
                return True

            logger.warning(f"[ClassroomV2] Login pode ter falhado. URL: {current_url}")

        except Exception as e:
            logger.error(f"[ClassroomV2] Erro login tentativa {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                await page.wait_for_timeout(3000)

    return False


# ============================================================
# CRIAR BROWSER (config padrão)
# ============================================================
async def criar_browser(p):
    """Cria browser com config anti-detecção."""
    browser = await p.chromium.launch(
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,800",
        ]
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="pt-BR"
    )
    page = await context.new_page()
    return browser, context, page


# ============================================================
# SCRAPING — LISTAR TURMAS
# ============================================================
async def scrape_listar_turmas(req: TurmasRequest) -> dict:
    """Lista todas as turmas do Classroom."""
    from playwright.async_api import async_playwright

    dados = {"turmas": [], "erros": []}
    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD

    async with async_playwright() as p:
        browser, context, page = await criar_browser(p)

        try:
            # 1. Login
            login_ok = await google_login(page, email, password)
            if not login_ok:
                dados["erros"].append("Falha no login Google")
                return dados

            # 2. Navegar para o Classroom
            logger.info("[ClassroomV2] Navegando para Classroom...")
            await page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            if "classroom.google.com" not in page.url:
                dados["erros"].append(f"Não acessou Classroom. URL: {page.url}")
                return dados

            # 3. Rolar para carregar todas as turmas
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

            # 4. Extrair turmas
            turmas_raw = await page.evaluate("""
                () => {
                    const turmas = [];
                    const cards = document.querySelectorAll('[data-course-id], .gHz6xd, .YVvGBb');
                    cards.forEach(card => {
                        const nome = card.querySelector('.YVvGBb, .R4EiSb, h2')?.textContent?.trim() || '';
                        const secao = card.querySelector('.tL9Q4c, .Mdb1Xb')?.textContent?.trim() || '';
                        const link = card.querySelector('a[href*="/c/"]')?.href || '';
                        const courseId = card.getAttribute('data-course-id') || '';
                        if (nome) {
                            turmas.push({ nome, secao, link, courseId });
                        }
                    });
                    if (turmas.length === 0) {
                        document.querySelectorAll('a[href*="/c/"]').forEach(a => {
                            const nome = a.textContent?.trim() || '';
                            if (nome && nome.length > 2) {
                                turmas.push({ nome, secao: '', link: a.href, courseId: '' });
                            }
                        });
                    }
                    return turmas;
                }
            """)

            for turma in turmas_raw:
                dados["turmas"].append(turma)
                logger.info(f"[ClassroomV2] Turma: {turma['nome']}")

            if not dados["turmas"]:
                page_text = await page.evaluate("document.body.innerText")
                dados["erros"].append(f"Nenhuma turma encontrada. Texto: {page_text[:1000]}")

        except Exception as e:
            logger.error(f"[ClassroomV2] Erro listar turmas: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro: {str(e)}")
        finally:
            await browser.close()
            gc.collect()

    dados["resumo"] = {
        "total_turmas": len(dados["turmas"]),
        "total_erros": len(dados["erros"])
    }
    return dados


# ============================================================
# SCRAPING — COLETAR 1 TURMA (materiais + download)
# ============================================================
async def scrape_coletar_turma(req: TurmaRequest) -> dict:
    """
    Coleta materiais e arquivos de 1 turma.
    Compara com arquivos_existentes (inventário do Drive) e só baixa os novos.
    Retorna arquivos em base64 para o n8n fazer upload.
    """
    from playwright.async_api import async_playwright

    dados = {
        "turma": req.turma_nome,
        "turma_link": req.turma_link,
        "topicos": [],
        "materiais": [],
        "arquivos_novos": [],      # Arquivos baixados (novos)
        "arquivos_existentes": [],  # Arquivos que já estavam no Drive
        "erros": []
    }

    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD
    existentes = set(req.arquivos_existentes)  # Set para busca rápida

    if not req.turma_link:
        dados["erros"].append("turma_link é obrigatório")
        return dados

    async with async_playwright() as p:
        browser, context, page = await criar_browser(p)

        try:
            # 1. Login
            login_ok = await google_login(page, email, password)
            if not login_ok:
                dados["erros"].append("Falha no login Google")
                return dados

            # 2. Navegar para a aba Atividades da turma
            # Extrair course ID do link para ir direto na aba Atividades
            course_id_match = re.search(r'/c/(\w+)', req.turma_link)
            if course_id_match:
                atividades_url = f"https://classroom.google.com/w/{course_id_match.group(1)}/t/all"
            else:
                atividades_url = req.turma_link

            logger.info(f"[ClassroomV2] Acessando turma: {req.turma_nome} -> {atividades_url}")
            await page.goto(atividades_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            # 3. Verificar se estamos na página certa
            if "classroom.google.com" not in page.url:
                dados["erros"].append(f"Não acessou a turma. URL: {page.url}")
                return dados

            # 4. Rolar para carregar todos os materiais
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

            # 5. Expandir todos os materiais e coletar anexos
            # Primeiro, pegar a lista de materiais (botões expandíveis)
            materiais_info = await page.evaluate("""
                () => {
                    const result = [];
                    // Pegar todos os botões de material (role="button" com hint de material)
                    const buttons = document.querySelectorAll('div[role="button"][data-stream-item-id]');
                    buttons.forEach(btn => {
                        const nome = btn.getAttribute('aria-label') || btn.textContent?.trim() || '';
                        const id = btn.getAttribute('data-stream-item-id') || '';
                        const expanded = btn.getAttribute('aria-expanded');
                        result.push({ nome, id, expanded });
                    });

                    // Fallback: pegar materiais por classe CSS
                    if (result.length === 0) {
                        document.querySelectorAll('.asQXV, [jscontroller]').forEach(el => {
                            const btn = el.querySelector('div[role="button"]');
                            if (btn) {
                                const nome = btn.textContent?.trim() || '';
                                if (nome && nome.length > 3) {
                                    result.push({ nome, id: '', expanded: btn.getAttribute('aria-expanded') });
                                }
                            }
                        });
                    }

                    return result;
                }
            """)

            logger.info(f"[ClassroomV2] {len(materiais_info)} materiais encontrados")

            # 6. Para cada material, expandir e coletar anexos
            for mat in materiais_info:
                mat_nome = mat.get("nome", "")
                logger.info(f"[ClassroomV2] Processando material: {mat_nome}")

                try:
                    # Expandir o material clicando nele
                    if mat.get("id"):
                        await page.evaluate(f"""
                            () => {{
                                const btn = document.querySelector('div[data-stream-item-id="{mat["id"]}"]');
                                if (btn && btn.getAttribute('aria-expanded') !== 'true') {{
                                    btn.click();
                                }}
                            }}
                        """)
                    else:
                        # Fallback: clicar por texto
                        btn = page.locator(f'div[role="button"]:has-text("{mat_nome[:50]}")')
                        if await btn.count() > 0:
                            expanded = await btn.first.get_attribute('aria-expanded')
                            if expanded != 'true':
                                await btn.first.click()

                    await page.wait_for_timeout(2000)

                    # Coletar links de anexos após expandir
                    anexos = await page.evaluate("""
                        () => {
                            const anexos = [];
                            // Pegar todos os links de anexos visíveis
                            document.querySelectorAll('a[href*="drive.google.com/file"], a[href*="drive.google.com/open"], a[href*="docs.google.com"], a[href*="slides.google.com"]').forEach(a => {
                                const nome = a.textContent?.trim() || '';
                                const url = a.href || '';
                                if (url && nome) {
                                    const match = url.match(/\\/d\\/([a-zA-Z0-9_-]+)/);
                                    const fileId = match ? match[1] : '';
                                    // Evitar duplicatas
                                    if (fileId && !anexos.find(x => x.fileId === fileId)) {
                                        anexos.push({ nome, url, fileId });
                                    }
                                }
                            });
                            return anexos;
                        }
                    """)

                    material_data = {
                        "nome": mat_nome,
                        "anexos": anexos
                    }
                    dados["materiais"].append(material_data)

                    # 7. Para cada anexo, verificar inventário e baixar se necessário
                    for anexo in anexos:
                        anexo_nome = anexo.get("nome", "")
                        file_id = anexo.get("fileId", "")

                        if not file_id:
                            continue

                        # Verificar se já existe no Drive (inventário)
                        if anexo_nome in existentes:
                            logger.info(f"[ClassroomV2] Arquivo já existe no Drive: {anexo_nome}")
                            dados["arquivos_existentes"].append(anexo_nome)
                            continue

                        # Baixar arquivo novo
                        logger.info(f"[ClassroomV2] Baixando: {anexo_nome} (ID: {file_id})")
                        try:
                            download_url = f"https://drive.google.com/uc?export=download&id={file_id}"

                            # Usar o browser autenticado para download
                            download_page = await context.new_page()
                            response = await download_page.goto(download_url, wait_until="domcontentloaded", timeout=30000)

                            # Verificar se é download direto ou precisa confirmar
                            if response and response.url and "download" in response.url:
                                # Tentar pegar o conteúdo via fetch no contexto autenticado
                                pass

                            # Método alternativo: usar JavaScript fetch no contexto autenticado
                            file_content = await download_page.evaluate(f"""
                                async () => {{
                                    try {{
                                        const resp = await fetch('{download_url}', {{ credentials: 'include' }});
                                        if (!resp.ok) return {{ error: 'HTTP ' + resp.status }};
                                        const blob = await resp.blob();
                                        return new Promise((resolve) => {{
                                            const reader = new FileReader();
                                            reader.onload = () => resolve({{ 
                                                data: reader.result.split(',')[1],
                                                size: blob.size,
                                                type: blob.type
                                            }});
                                            reader.readAsDataURL(blob);
                                        }});
                                    }} catch(e) {{
                                        return {{ error: e.message }};
                                    }}
                                }}
                            """)

                            await download_page.close()

                            if file_content and not file_content.get("error"):
                                dados["arquivos_novos"].append({
                                    "nome": anexo_nome,
                                    "file_id": file_id,
                                    "tamanho": file_content.get("size", 0),
                                    "tipo": file_content.get("type", ""),
                                    "conteudo_base64": file_content.get("data", ""),
                                    "turma": req.turma_nome,
                                    "material": mat_nome
                                })
                                logger.info(f"[ClassroomV2] Download OK: {anexo_nome} ({file_content.get('size', 0)} bytes)")
                            else:
                                error_msg = file_content.get("error", "Erro desconhecido") if file_content else "Sem resposta"
                                dados["erros"].append(f"Download falhou: {anexo_nome} - {error_msg}")
                                logger.error(f"[ClassroomV2] Download falhou: {anexo_nome} - {error_msg}")

                        except Exception as e:
                            dados["erros"].append(f"Download erro: {anexo_nome} - {str(e)}")
                            logger.error(f"[ClassroomV2] Erro download {anexo_nome}: {e}")

                except Exception as e:
                    dados["erros"].append(f"Material '{mat_nome}': {str(e)}")
                    logger.error(f"[ClassroomV2] Erro material {mat_nome}: {e}")

        except Exception as e:
            logger.error(f"[ClassroomV2] Erro geral turma: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro geral: {str(e)}")
        finally:
            await browser.close()
            gc.collect()

    dados["resumo"] = {
        "turma": req.turma_nome,
        "total_materiais": len(dados["materiais"]),
        "total_arquivos_novos": len(dados["arquivos_novos"]),
        "total_arquivos_existentes": len(dados["arquivos_existentes"]),
        "total_erros": len(dados["erros"])
    }
    return dados


# ============================================================
# BACKGROUND RUNNERS
# ============================================================
async def run_classroom_job(job_id: str, fonte: str, scrape_func, req):
    try:
        logger.info(f"[Job {job_id}] Iniciando {fonte}...")
        dados = await scrape_func(req)
        if job_id in classroom_jobs:
            classroom_jobs[job_id]["status"] = "completed" if not dados.get("erros") else "partial"
            classroom_jobs[job_id]["completed_at"] = datetime.now().isoformat()
            classroom_jobs[job_id]["result"] = {
                "status": "success" if not dados.get("erros") else "partial",
                "fonte": fonte,
                "data_coleta": datetime.now().isoformat(),
                "dados": dados,
                "erros": dados.get("erros", [])
            }
        logger.info(f"[Job {job_id}] {fonte} concluído!")
    except Exception as e:
        logger.error(f"[Job {job_id}] Erro {fonte}: {e}\n{traceback.format_exc()}")
        if job_id in classroom_jobs:
            classroom_jobs[job_id]["status"] = "failed"
            classroom_jobs[job_id]["completed_at"] = datetime.now().isoformat()
            classroom_jobs[job_id]["result"] = {
                "status": "error",
                "fonte": fonte,
                "data_coleta": datetime.now().isoformat(),
                "dados": {},
                "erros": [str(e)]
            }
    finally:
        gc.collect()


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/turmas/job/{job_id}")
def get_turmas_job(job_id: str, authorization: str = Header(None)):
    """Consulta status de um job do Classroom V2."""
    verificar_auth(authorization)
    if job_id not in classroom_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} não encontrado")
    job = classroom_jobs[job_id]
    if job["status"] == "processing":
        return {
            "job_id": job_id,
            "status": "processing",
            "fonte": job["fonte"],
            "created_at": job["created_at"],
            "message": "Job em processamento. Tente novamente em 10-30s."
        }
    return job["result"]


@router.post("/turmas")
async def endpoint_listar_turmas(
    req: TurmasRequest = TurmasRequest(),
    background_tasks: BackgroundTasks = None,
    authorization: str = Header(None),
    async_mode: bool = True
):
    """
    Lista todas as turmas do Google Classroom.
    Sempre assíncrono (retorna job_id).
    """
    verificar_auth(authorization)
    job_id = create_classroom_job("classroom-turmas")
    background_tasks.add_task(run_classroom_job, job_id, "classroom-turmas", scrape_listar_turmas, req)
    return {"job_id": job_id, "status": "processing", "poll_url": f"/scrape/classroom/turmas/job/{job_id}"}


@router.post("/turma")
async def endpoint_coletar_turma(
    req: TurmaRequest,
    background_tasks: BackgroundTasks = None,
    authorization: str = Header(None)
):
    """
    Coleta materiais e arquivos de 1 turma.
    Compara com inventário (arquivos_existentes) e só baixa os novos.
    Retorna arquivos em base64 para o n8n fazer upload no Drive.
    Sempre assíncrono.
    """
    verificar_auth(authorization)
    job_id = create_classroom_job(f"classroom-turma-{req.turma_nome[:20]}")
    background_tasks.add_task(run_classroom_job, job_id, f"classroom-turma", scrape_coletar_turma, req)
    return {"job_id": job_id, "status": "processing", "poll_url": f"/scrape/classroom/turmas/job/{job_id}"}
