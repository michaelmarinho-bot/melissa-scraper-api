"""
Classroom V3 — Endpoints fragmentados com download por tipo de arquivo
Versão: 3.10.5 — Fix timezone: browser usa America/Sao_Paulo para datas corretas
            ou título contendo palavras-chave (PT/ES/siglas) são capturados como textos

Endpoints:
  POST /scrape/classroom/turmas  - Lista todas as turmas do Classroom
  POST /scrape/classroom/turma   - Coleta materiais e arquivos de 1 turma (com download)
  GET  /scrape/classroom/files/{file_key}  - Download de arquivo temporário (NOVO v3.8.2)

Tipos de download suportados:
  - Google Docs   → .pdf (export URL direto) [v3.7.0: era .docx]
  - Google Slides → .pdf (export URL direto) [v3.7.0: era .pptx]
  - Google Sheets → .pdf (export URL direto) [v3.7.0: era .xlsx]
  - PDF           → .pdf  (Drive viewer → botão Baixar)
  - Imagem        → .png/.jpg original (Drive viewer → botão Baixar)
  - Office files  → formato original (Drive viewer → botão Baixar)

Arquitetura:
  - Cada chamada abre e fecha o browser (1 turma = 1 browser = pouca memória)
  - UMA ÚNICA aba de download é reutilizada para todos os arquivos (fix v3.6.0)
  - O n8n faz o inventário no Drive e orquestra as chamadas
  - v3.9.0: FASE 1 agora navega para aba Atividades (/w/XXXXX/t/all),
    expande todos os itens via JS, coleta anexos e textos.
    Textos são coletados APENAS de itens filtrados (prova, tarefa, lição,
    trabalho, OIA) com matéria, conteúdo e data de entrega.
  - v3.8.2: Arquivos ficam em /tmp/ no servidor. O JSON retorna apenas metadados
    (nome, tamanho, file_key). O n8n baixa 1 a 1 via GET /files/{file_key}
    e faz upload no Drive. Isso evita crash de memória no n8n.
  - Export PDF é mais leve e estável que DOCX/PPTX/XLSX (v3.7.0)
  - Fallback do editor removido — evita crash de memória no Render 512MB (v3.7.0)
  - Fix: ERR_ABORTED tratado corretamente no export URL (v3.7.1)

Changelog:
  v3.10.0 — Coleta de textos expandida:
            - Filtro ampliado com termos em espanhol, siglas e padrões
            - Itens com tipo 'Atividade' são SEMPRE capturados como texto
            - Itens com data_entrega são SEMPRE capturados como texto
            - Conteúdo vazio não impede mais a captura (título é suficiente)
  v3.9.2 — Fix navegação: entra pelo Mural (/c/XXXXX) e clica na aba Atividades
            Fix expansão: clica no div[role=button][aria-expanded] interno
            (antes clicava no li.tfGBod que não expandia corretamente)
  v3.9.1 — Fix regex fileId: mínimo 10 caracteres para evitar IDs inválidos (ex: "e")
  v3.9.0 — Coleta via aba Atividades em vez do Mural
            Expande todos os itens e coleta anexos + textos
            Filtro de textos: só coleta de prova/tarefa/lição/trabalho/OIA
            Retorna matéria, conteúdo, data_entrega nos textos
  v3.8.2 — Download em batches de 2 arquivos com reopen do browser entre batches
            Evita crash de memória no Render 512MB com muitos arquivos
  v3.8.1 — Arquivos temporários + endpoint GET /files/{file_key}
            Sem base64 no JSON de resultado → n8n não estoura memória
  v3.7.1 — Fix ERR_ABORTED no export PDF
  v3.7.0 — Export PDF para Docs/Slides/Sheets + remover fallbacks pesados
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
from pathlib import Path

from fastapi import APIRouter, HTTPException, Header, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Reutilizar config do main
API_SECRET = os.environ.get("MELISSA_API_SECRET", "") or os.environ.get("MELISSA_API_KEY", "trocar-por-uma-chave-segura")
MELISSA_EMAIL = os.environ.get("MELISSA_EMAIL", "melissa.marinho@liceujardim.g12.br")
MELISSA_PASSWORD = os.environ.get("MELISSA_PASSWORD", "elvis!!1")

logger = logging.getLogger("melissa-scraper")

# Router FastAPI
router = APIRouter(prefix="/scrape/classroom", tags=["Classroom V3"])

# Jobs store
classroom_jobs: Dict[str, Dict[str, Any]] = {}

# v3.8.2: Store de arquivos temporários
# Formato: { file_key: { "path": "/tmp/...", "filename": "nome.pdf", "size": 12345, "created_at": "..." } }
temp_files: Dict[str, Dict[str, Any]] = {}

# Diretório para arquivos temporários
TEMP_DIR = "/tmp/classroom_files"
os.makedirs(TEMP_DIR, exist_ok=True)


# ============================================================
# MODELOS
# ============================================================
class TurmasRequest(BaseModel):
    email: str = ""
    password: str = ""


class TurmaRequest(BaseModel):
    email: str = ""
    password: str = ""
    turma_link: str = ""
    turma_nome: str = ""
    arquivos_existentes: List[str] = []


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
    if len(classroom_jobs) > 30:
        oldest = sorted(classroom_jobs.keys(), key=lambda k: classroom_jobs[k]["created_at"])[:10]
        for k in oldest:
            del classroom_jobs[k]
    return job_id


def register_temp_file(file_path: str, filename: str, size: int) -> str:
    """
    v3.8.2: Registra um arquivo temporário e retorna uma file_key única.
    O n8n usa essa key para baixar o arquivo via GET /files/{file_key}.
    """
    file_key = uuid.uuid4().hex[:12]
    temp_files[file_key] = {
        "path": file_path,
        "filename": filename,
        "size": size,
        "created_at": datetime.now().isoformat(),
        "downloaded": False
    }
    # Limpar arquivos antigos (mais de 50 registros)
    if len(temp_files) > 50:
        oldest = sorted(temp_files.keys(), key=lambda k: temp_files[k]["created_at"])[:20]
        for k in oldest:
            try:
                old_path = temp_files[k]["path"]
                if os.path.exists(old_path):
                    os.remove(old_path)
            except:
                pass
            del temp_files[k]
    return file_key


# ============================================================
# LOGIN GOOGLE
# ============================================================
async def google_login(page, email: str, password: str, max_retries: int = 3):
    """
    Login no Google — v3.10.4
    Base: código idêntico ao main.py (v3.10.2) que funciona.
    Única adição: detecção de tela 'Escolher conta' ANTES do campo de email.
    URL de navegação: accounts.google.com/signin (NÃO mudar!).
    """
    for attempt in range(max_retries):
        try:
            logger.info(f"[ClassroomV3] Login tentativa {attempt + 1}/{max_retries}...")

            # Navegar para o login do Google (NÃO mudar esta URL!)
            await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Verificar se já está logado
            current_url = page.url
            if "myaccount.google.com" in current_url or "classroom.google.com" in current_url:
                logger.info("[ClassroomV3] Já está logado!")
                return True

            # === NOVO v3.10.4: Detectar tela "Escolher uma conta" ===
            choose_account = page.locator('div[data-email]')
            choose_count = await choose_account.count()
            if choose_count > 0:
                logger.info(f"[ClassroomV3] Tela 'Escolher conta' detectada ({choose_count} contas)")
                target_account = page.locator(f'div[data-email="{email}"]')
                if await target_account.count() > 0:
                    logger.info(f"[ClassroomV3] Clicando na conta {email}")
                    await target_account.first.click()
                    await page.wait_for_timeout(4000)
                    # Após clicar na conta, pode ir direto para senha
                else:
                    # Conta não está na lista — clicar em "Usar outra conta"
                    logger.info("[ClassroomV3] Conta não na lista, clicando 'Usar outra conta'")
                    use_another = page.locator('[data-identifier="other"], div[role="link"]:has-text("outra"), div[role="link"]:has-text("another")')
                    if await use_another.count() > 0:
                        await use_another.first.click()
                        await page.wait_for_timeout(3000)

            # Inserir email (só se o campo estiver visível)
            email_input = page.locator('input[type="email"]')
            if await email_input.count() > 0 and await email_input.first.is_visible():
                await email_input.fill(email)
                await page.wait_for_timeout(500)
                await page.locator('#identifierNext button').click()
                await page.wait_for_timeout(4000)

            # Aguardar tela de senha
            password_input = page.locator('input[type="password"]:not([aria-hidden="true"]):not([tabindex="-1"])')
            await password_input.wait_for(state="visible", timeout=15000)
            await password_input.fill(password)
            await page.wait_for_timeout(500)
            await page.locator('#passwordNext button').click()
            await page.wait_for_timeout(5000)

            # Verificar se login foi bem-sucedido
            current_url = page.url
            if "accounts.google.com" not in current_url:
                logger.info(f"[ClassroomV3] Login OK! URL: {current_url}")
                return True

            # Verificar CAPTCHA real
            has_visible_captcha = await page.locator('iframe[title*="recaptcha"]:visible').count() > 0
            if has_visible_captcha:
                logger.error("[ClassroomV3] CAPTCHA visível detectado!")
                return False

            # Pode ter redirecionamento extra
            await page.wait_for_timeout(5000)
            current_url = page.url
            if "accounts.google.com" not in current_url:
                logger.info(f"[ClassroomV3] Login OK (redirect)! URL: {current_url}")
                return True

            logger.warning(f"[ClassroomV3] Login pode ter falhado. URL: {current_url}")

        except Exception as e:
            logger.error(f"[ClassroomV3] Erro login tentativa {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                await page.wait_for_timeout(3000)

    return False


# ============================================================
# CRIAR BROWSER
# ============================================================
async def criar_browser(p):
    """Cria browser com config anti-detecção e accept_downloads habilitado."""
    browser = await p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,800",
            "--single-process",
            "--disable-extensions",
            "--disable-background-networking",
        ]
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",  # v3.10.5: Fix timezone para datas do Classroom
        accept_downloads=True  # Essencial para capturar downloads
    )
    page = await context.new_page()
    return browser, context, page


# ============================================================
# DOWNLOAD HELPERS — Por tipo de arquivo
# v3.8.2: Agora salvam em /tmp/ e retornam file_key (sem base64)
# ============================================================

async def download_drive_file(page, file_id: str, nome: str) -> dict:
    """
    Download de arquivo do Drive (PDF, imagem, Office, etc.)
    REUTILIZA a page existente — navega, baixa, e limpa.
    v3.8.2: Salva em /tmp/ e retorna file_key.
    """
    try:
        view_url = f"https://drive.google.com/file/d/{file_id}/view"
        logger.info(f"[ClassroomV3] download_drive_file: {nome} -> {view_url}")

        await page.goto(view_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # Procurar botão "Baixar" no viewer do Drive
        download_btn = page.locator('button:has-text("Baixar"), [aria-label*="Baixar"], [aria-label*="Download"], [data-tooltip*="Baixar"], [data-tooltip*="Download"]')
        btn_count = await download_btn.count()
        logger.info(f"[ClassroomV3] Botões de download encontrados: {btn_count}")

        if btn_count > 0:
            try:
                async with page.expect_download(timeout=30000) as download_info:
                    await download_btn.first.click()
                download = await download_info.value
                tmp_path = os.path.join(TEMP_DIR, f"dl_{uuid.uuid4().hex[:8]}")
                await download.save_as(tmp_path)
                suggested_name = download.suggested_filename or nome
                file_size = os.path.getsize(tmp_path)
                file_key = register_temp_file(tmp_path, suggested_name, file_size)
                return {
                    "file_key": file_key,
                    "size": file_size,
                    "filename": suggested_name
                }
            except Exception as e:
                logger.warning(f"[ClassroomV3] expect_download falhou: {e}, tentando fallback...")

        # Fallback: procurar via JS
        if btn_count == 0:
            found = await page.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button, [role="button"]');
                    for (const btn of btns) {
                        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                        const tooltip = (btn.getAttribute('data-tooltip') || '').toLowerCase();
                        const hint = (btn.getAttribute('hint') || '').toLowerCase();
                        if (label.includes('baixar') || label.includes('download') ||
                            tooltip.includes('baixar') || tooltip.includes('download') ||
                            hint.includes('baixar') || hint.includes('download')) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if not found:
                # Fallback final: URL direta de download
                logger.info(f"[ClassroomV3] Tentando URL direta de download...")
                export_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
                try:
                    async with page.expect_download(timeout=30000) as download_info:
                        try:
                            await page.goto(export_url)
                        except Exception:
                            pass  # ERR_ABORTED esperado
                    download = await download_info.value
                    tmp_path = os.path.join(TEMP_DIR, f"dl_{uuid.uuid4().hex[:8]}")
                    await download.save_as(tmp_path)
                    suggested_name = download.suggested_filename or nome
                    file_size = os.path.getsize(tmp_path)
                    file_key = register_temp_file(tmp_path, suggested_name, file_size)
                    return {
                        "file_key": file_key,
                        "size": file_size,
                        "filename": suggested_name
                    }
                except Exception as e2:
                    logger.error(f"[ClassroomV3] Fallback download também falhou: {e2}")
                    return {"error": f"Download falhou: {e2}"}

        # Se clicou via JS, esperar download
        await page.wait_for_timeout(5000)
        return {"error": "Download via JS clicado mas não capturado"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_drive_file erro: {e}")
        return {"error": str(e)}


async def download_google_doc(page, file_id: str, nome: str) -> dict:
    """
    Download de Google Docs como .pdf via export URL direto.
    v3.8.2: Salva em /tmp/ e retorna file_key (sem base64).
    """
    try:
        export_url = f"https://docs.google.com/document/d/{file_id}/export?format=pdf"
        logger.info(f"[ClassroomV3] download_google_doc: {nome} -> export pdf")

        try:
            async with page.expect_download(timeout=30000) as download_info:
                try:
                    await page.goto(export_url)
                except Exception:
                    pass  # ERR_ABORTED esperado para URLs de download

            download = await download_info.value
            tmp_path = os.path.join(TEMP_DIR, f"dl_{uuid.uuid4().hex[:8]}")
            await download.save_as(tmp_path)
            suggested_name = download.suggested_filename or f"{nome}.pdf"
            file_size = os.path.getsize(tmp_path)
            if file_size > 0:
                file_key = register_temp_file(tmp_path, suggested_name, file_size)
                logger.info(f"[ClassroomV3] Google Doc baixado OK: {nome} ({file_size} bytes)")
                return {
                    "file_key": file_key,
                    "size": file_size,
                    "filename": suggested_name
                }
            else:
                logger.warning(f"[ClassroomV3] Download vazio para Google Doc: {nome}")
                os.remove(tmp_path)
        except Exception as e:
            logger.warning(f"[ClassroomV3] Export PDF falhou para Google Doc: {e}")

        logger.warning(f"[ClassroomV3] Google Doc não baixado (sem fallback): {nome}")
        return {"error": f"Export PDF falhou para Google Doc: {nome}"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_google_doc erro: {e}")
        return {"error": str(e)}


async def download_google_slides(page, file_id: str, nome: str) -> dict:
    """
    Download de Google Slides como .pdf via export URL direto.
    v3.8.2: Salva em /tmp/ e retorna file_key (sem base64).
    """
    try:
        export_url = f"https://docs.google.com/presentation/d/{file_id}/export?format=pdf"
        logger.info(f"[ClassroomV3] download_google_slides: {nome} -> export pdf")

        try:
            async with page.expect_download(timeout=30000) as download_info:
                try:
                    await page.goto(export_url)
                except Exception:
                    pass  # ERR_ABORTED esperado para URLs de download

            download = await download_info.value
            tmp_path = os.path.join(TEMP_DIR, f"dl_{uuid.uuid4().hex[:8]}")
            await download.save_as(tmp_path)
            suggested_name = download.suggested_filename or f"{nome}.pdf"
            file_size = os.path.getsize(tmp_path)
            if file_size > 0:
                file_key = register_temp_file(tmp_path, suggested_name, file_size)
                logger.info(f"[ClassroomV3] Google Slides baixado OK: {nome} ({file_size} bytes)")
                return {
                    "file_key": file_key,
                    "size": file_size,
                    "filename": suggested_name
                }
            else:
                logger.warning(f"[ClassroomV3] Download vazio para Slides: {nome}")
                os.remove(tmp_path)
        except Exception as e:
            logger.warning(f"[ClassroomV3] Export PDF falhou para Slides: {e}")

        logger.warning(f"[ClassroomV3] Google Slides não baixado (sem fallback): {nome}")
        return {"error": f"Export PDF falhou para Google Slides: {nome}"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_google_slides erro: {e}")
        return {"error": str(e)}


async def download_google_sheets(page, file_id: str, nome: str) -> dict:
    """
    Download de Google Sheets como .pdf via export URL direto.
    v3.8.2: Salva em /tmp/ e retorna file_key (sem base64).
    """
    try:
        export_url = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=pdf"
        logger.info(f"[ClassroomV3] download_google_sheets: {nome} -> export pdf")

        try:
            async with page.expect_download(timeout=30000) as download_info:
                try:
                    await page.goto(export_url)
                except Exception:
                    pass  # ERR_ABORTED esperado para URLs de download

            download = await download_info.value
            tmp_path = os.path.join(TEMP_DIR, f"dl_{uuid.uuid4().hex[:8]}")
            await download.save_as(tmp_path)
            suggested_name = download.suggested_filename or f"{nome}.pdf"
            file_size = os.path.getsize(tmp_path)
            if file_size > 0:
                file_key = register_temp_file(tmp_path, suggested_name, file_size)
                logger.info(f"[ClassroomV3] Google Sheets baixado OK: {nome} ({file_size} bytes)")
                return {
                    "file_key": file_key,
                    "size": file_size,
                    "filename": suggested_name
                }
            else:
                logger.warning(f"[ClassroomV3] Download vazio para Sheets: {nome}")
                os.remove(tmp_path)
        except Exception as e:
            logger.warning(f"[ClassroomV3] Export PDF falhou para Sheets: {e}")

        logger.warning(f"[ClassroomV3] Google Sheets não baixado (sem fallback): {nome}")
        return {"error": f"Export PDF falhou para Google Sheets: {nome}"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_google_sheets erro: {e}")
        return {"error": str(e)}


async def download_arquivo(page, anexo: dict) -> dict:
    """
    Router de download — escolhe a estratégia correta por tipo de arquivo.
    REUTILIZA a mesma page para todos os downloads.
    v3.8.2: Retorna dict com file_key, size, filename ou error.
    """
    file_id = anexo.get("fileId", "")
    nome = anexo.get("nome", "arquivo")
    tipo = anexo.get("tipo", "drive_file")

    logger.info(f"[ClassroomV3] Download: {nome} | tipo={tipo} | id={file_id}")

    if tipo == "google_doc":
        return await download_google_doc(page, file_id, nome)
    elif tipo == "google_slides":
        return await download_google_slides(page, file_id, nome)
    elif tipo == "google_sheets":
        return await download_google_sheets(page, file_id, nome)
    else:
        return await download_drive_file(page, file_id, nome)


# ============================================================
# SCRAPING FUNCTIONS
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
            login_ok = await google_login(page, email, password)
            if not login_ok:
                dados["erros"].append("Falha no login Google")
                return dados

            logger.info("[ClassroomV3] Navegando para Classroom...")
            await page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            if "classroom.google.com" not in page.url:
                dados["erros"].append(f"Não acessou Classroom. URL: {page.url}")
                return dados

            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

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
                logger.info(f"[ClassroomV3] Turma: {turma['nome']}")

            if not dados["turmas"]:
                page_text = await page.evaluate("document.body.innerText")
                dados["erros"].append(f"Nenhuma turma encontrada. Texto: {page_text[:1000]}")

        except Exception as e:
            logger.error(f"[ClassroomV3] Erro listar turmas: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro: {str(e)}")
        finally:
            await browser.close()
            gc.collect()

    dados["resumo"] = {
        "total_turmas": len(dados["turmas"]),
        "total_erros": len(dados["erros"])
    }
    return dados


async def scrape_coletar_turma(req: TurmaRequest) -> dict:
    """
    Coleta materiais, textos e arquivos de 1 turma do Classroom.
    v3.9.0: Navega para aba Atividades (/w/XXXXX/t/all), expande todos os
    itens e coleta anexos. Textos são coletados apenas de itens filtrados
    (prova, tarefa, lição, trabalho, OIA) com matéria, conteúdo e data_entrega.
    v3.8.2: Download em batches de 2 arquivos. Fecha e reabre o browser
    entre batches para liberar memória (Render 512MB).
    Arquivos ficam em /tmp/, retorna file_key para download posterior.
    """
    from playwright.async_api import async_playwright

    BATCH_SIZE = 2  # v3.8.2: baixar 2 arquivos por vez

    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD
    existentes = set(req.arquivos_existentes)

    dados = {
        "turma": req.turma_nome,
        "turma_link": req.turma_link,
        "materiais": [],
        "textos": [],
        "arquivos_novos": [],
        "arquivos_existentes": [],
        "erros": [],
        "resumo": {}
    }

    # =============================================
    # FASE 1: Navegar, coletar materiais e anexos
    # v3.9.0: Navega para aba Atividades, clica item por item
    # =============================================
    arquivos_para_baixar = []

    async with async_playwright() as p:
        browser, context, page = await criar_browser(p)
        try:
            logged_in = await google_login(page, email, password)
            if not logged_in:
                dados["erros"].append("Falha no login do Google")
                return dados

            # v3.9.2: Navegar para o Mural primeiro, depois clicar na aba Atividades
            # Navegar direto para /w/XXXXX/t/all pode dar "Turma não encontrada"
            mural_url = req.turma_link
            if '/w/' in mural_url:
                # Converter /w/XXXXX/... de volta para /c/XXXXX
                course_id = mural_url.split('/w/')[-1].split('/')[0].split('?')[0]
                mural_url = f"https://classroom.google.com/c/{course_id}"
            elif '/c/' not in mural_url:
                mural_url = req.turma_link  # Usar como está

            logger.info(f"[ClassroomV3] Acessando turma (Mural): {req.turma_nome} -> {mural_url}")
            await page.goto(mural_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            current_url = page.url
            logger.info(f"[ClassroomV3] URL após Mural: {current_url}")

            # v3.9.2: Clicar na aba Atividades
            try:
                atividades_tab = page.locator('a:has-text("Atividades"), a[href*="/t/all"]').first
                await atividades_tab.click(timeout=5000)
                logger.info("[ClassroomV3] Clicou na aba Atividades")
            except Exception:
                # Fallback: tentar navegar direto
                if '/c/' in page.url:
                    course_id = page.url.split('/c/')[-1].split('/')[0].split('?')[0]
                    fallback_url = f"https://classroom.google.com/w/{course_id}/t/all"
                    logger.info(f"[ClassroomV3] Fallback: navegando direto para {fallback_url}")
                    await page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
                else:
                    logger.warning("[ClassroomV3] Não conseguiu clicar na aba Atividades")
            await page.wait_for_timeout(5000)

            current_url = page.url
            logger.info(f"[ClassroomV3] URL após aba Atividades: {current_url}")
            title = await page.title()
            logger.info(f"[ClassroomV3] Título da página: {title}")

            # v3.9.0: Scroll para carregar todos os itens
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(1000)

            # v3.9.0: Listar itens usando li.tfGBod[data-stream-item-id]
            items_ids = await page.evaluate("""
                () => {
                    const items = [];
                    document.querySelectorAll('li.tfGBod[data-stream-item-id]').forEach(el => {
                        const id = el.getAttribute('data-stream-item-id');
                        const titleSpan = el.querySelector('span.Vu2fZd.Cx437e');
                        const titulo = titleSpan ? titleSpan.textContent.trim() : '';
                        if (titulo) items.push({ id, titulo });
                    });
                    return items;
                }
            """)

            logger.info(f"[ClassroomV3] {len(items_ids)} itens encontrados na aba Atividades")
            for item in items_ids:
                logger.info(f"[ClassroomV3]   Item: {item['titulo']} (id={item['id']})")

            # v3.10.0: Filtro de textos expandido (PT + ES + siglas)
            FILTRO_PALAVRAS = [
                # Português
                'prova', 'tarefa', 'lição', 'licao', 'trabalho', 'oia',
                'avaliação', 'avaliacao', 'atividade', 'exercício', 'exercicio',
                'simulado', 'roteiro', 'projeto', 'redacao', 'redação',
                'pesquisa', 'questão', 'questao', 'dever',
                # Espanhol
                'prueba', 'tarea', 'lección', 'leccion', 'trabajo', 'proyecto',
                'ejercicio', 'examen', 'evaluación', 'evaluacion', 'actividad',
                'composición', 'composicion',
                # Siglas comuns
                'a.o', 'a.d', 'm.d',
            ]
            # v3.10.0: Padrões regex para datas no título (ex: "A.O (17/03/2026)")
            REGEX_DATA_TITULO = re.compile(r'\d{1,2}/\d{1,2}/\d{2,4}')

            # v3.9.0: Clicar item por item para expandir e coletar dados
            all_anexos_ids = set()  # Para deduplicar fileIds
            anexos_todos = []

            for idx, item in enumerate(items_ids):
                item_id = item['id']
                logger.info(f"[ClassroomV3] Expandindo item {idx+1}/{len(items_ids)}: {item['titulo']}")

                # Scroll para o item e clicar para expandir
                await page.evaluate(f"""
                    () => {{
                        const el = document.querySelector('li.tfGBod[data-stream-item-id="{item_id}"]');
                        if (el) el.scrollIntoView({{block: 'center'}});
                    }}
                """)
                await page.wait_for_timeout(500)

                # v3.9.2: Clicar com force=True no div[role="button"] interno
                # O Google coloca um overlay (div.xVnXCf) que intercepta cliques normais.
                # force=True ignora o overlay e clica diretamente no botão de expansão.
                try:
                    btn_locator = page.locator(f'li.tfGBod[data-stream-item-id="{item_id}"] div[role="button"][aria-expanded]').first
                    await btn_locator.click(timeout=5000, force=True)
                    logger.info(f"[ClassroomV3]   Expandido via force=True")
                except Exception as click_err:
                    logger.warning(f"[ClassroomV3]   Erro no clique force=True: {click_err}")
                    # Fallback: clicar via JS
                    await page.evaluate(f"""
                        () => {{
                            const el = document.querySelector('li.tfGBod[data-stream-item-id="{item_id}"]');
                            if (!el) return;
                            const btn = el.querySelector('div[role="button"]');
                            if (btn) btn.click();
                            else el.click();
                        }}
                    """)
                    logger.info(f"[ClassroomV3]   Fallback: clicou via JS")

                await page.wait_for_timeout(3000)

                # v3.9.2: Verificar se realmente expandiu
                is_expanded = await page.evaluate(f"""
                    () => {{
                        const el = document.querySelector('li.tfGBod[data-stream-item-id="{item_id}"]');
                        if (!el) return false;
                        const btn = el.querySelector('div[role="button"][aria-expanded="true"]');
                        return !!btn;
                    }}
                """)
                if not is_expanded:
                    logger.warning(f"[ClassroomV3]   Item NÃO expandiu! Tentando retry com force=True...")
                    try:
                        await btn_locator.click(timeout=5000, force=True)
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)

                # Coletar dados do LI expandido
                item_data = await page.evaluate(f"""
                    (filtro) => {{
                        const el = document.querySelector('li.tfGBod[data-stream-item-id="{item_id}"]');
                        if (!el) return null;

                        const text = el.innerText || '';
                        const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                        let tipo = '';
                        let titulo = '';
                        let data_entrega = '';
                        let data_postagem = '';
                        let conteudo_lines = [];

                        for (let j = 0; j < lines.length; j++) {{
                            const line = lines[j];

                            if (line === 'Material' || line === 'Atividade') {{
                                tipo = line;
                                continue;
                            }}
                            if (!titulo && tipo && line !== 'more_vert' && line !== 'Mais opções' &&
                                !line.startsWith('Data de entrega:') && !line.startsWith('Item postado:')) {{
                                titulo = line;
                                continue;
                            }}
                            if (line.startsWith('Data de entrega:')) {{
                                data_entrega = line.replace('Data de entrega:', '').trim();
                                continue;
                            }}
                            if (line.startsWith('Item postado:')) {{
                                data_postagem = line.replace('Item postado:', '').trim();
                                continue;
                            }}

                            // Skip UI elements
                            if (line === 'more_vert' || line === 'Mais opções' ||
                                line === 'Ver material' || line === 'Ver instruções' ||
                                line === 'Pendente' || line === 'Entregue' || line === 'Atribuída') {{
                                continue;
                            }}

                            if (titulo) {{
                                conteudo_lines.push(line);
                            }}
                        }}

                        // Coletar links de anexos DENTRO deste LI
                        const anexos = [];
                        const seen = new Set();
                        el.querySelectorAll('a[href]').forEach(a => {{
                            const url = a.href;
                            if (url.includes('/folders/')) return;

                            const match = url.match(/\\/d\\/([a-zA-Z0-9_-]{{10,}})/);
                            if (match && !seen.has(match[1])) {{
                                seen.add(match[1]);
                                let atipo = 'drive_file';
                                if (url.includes('docs.google.com/document')) atipo = 'google_doc';
                                else if (url.includes('docs.google.com/presentation') || url.includes('slides.google.com')) atipo = 'google_slides';
                                else if (url.includes('docs.google.com/spreadsheets')) atipo = 'google_sheets';

                                const nome = a.textContent?.trim()?.substring(0, 80) || '';
                                const hint = a.closest('.vwNuXe, .QRiHXd')?.querySelector('.kIKLkd, .bq3UNd')?.textContent?.trim()?.toLowerCase() || '';
                                if (hint.includes('imagem') || hint.includes('image')) atipo = 'imagem';
                                else if (hint.includes('pdf')) atipo = 'pdf';

                                anexos.push({{
                                    nome: nome,
                                    fileId: match[1],
                                    url: url.substring(0, 300),
                                    tipo: atipo,
                                    hint: ''
                                }});
                            }}
                        }});

                        const tituloLower = titulo.toLowerCase();
                        const passaFiltro = filtro.some(p => tituloLower.includes(p));

                        return {{
                            tipo,
                            titulo,
                            data_entrega,
                            data_postagem,
                            conteudo: conteudo_lines.join('\\n').substring(0, 1000),
                            passa_filtro: passaFiltro,
                            anexos
                        }};
                    }}
                """, FILTRO_PALAVRAS)

                if item_data:
                    # Salvar material
                    dados["materiais"].append({
                        "nome": item_data.get("titulo", item['titulo']),
                        "tipo": item_data.get("tipo", ""),
                        "anexos_count": len(item_data.get("anexos", []))
                    })

                    # v3.10.0: Salvar texto com lógica expandida
                    # Captura se: (1) passa no filtro de palavras, OU
                    #              (2) tipo == 'Atividade', OU
                    #              (3) tem data_entrega, OU
                    #              (4) título contém data (regex)
                    titulo_item = item_data.get("titulo", "")
                    tipo_item = item_data.get("tipo", "")
                    data_entrega_item = item_data.get("data_entrega", "")
                    conteudo_item = item_data.get("conteudo", "")
                    passa_filtro = item_data.get("passa_filtro", False)
                    eh_atividade = tipo_item.lower() in ['atividade', 'actividad']
                    tem_data_entrega = bool(data_entrega_item.strip())
                    tem_data_titulo = bool(REGEX_DATA_TITULO.search(titulo_item))

                    deve_capturar = passa_filtro or eh_atividade or tem_data_entrega or tem_data_titulo

                    if deve_capturar:
                        dados["textos"].append({
                            "materia": req.turma_nome,
                            "titulo": titulo_item,
                            "tipo": tipo_item,
                            "conteudo": conteudo_item if conteudo_item.strip() else titulo_item,
                            "data_entrega": data_entrega_item,
                            "data_postagem": item_data.get("data_postagem", "")
                        })
                        motivo = []
                        if passa_filtro: motivo.append('filtro')
                        if eh_atividade: motivo.append('atividade')
                        if tem_data_entrega: motivo.append('data_entrega')
                        if tem_data_titulo: motivo.append('data_titulo')
                        logger.info(f"[ClassroomV3]   Texto coletado: {titulo_item} | motivo={','.join(motivo)} | data_entrega={data_entrega_item}")

                    # Acumular anexos (deduplicar por fileId)
                    for a in item_data.get("anexos", []):
                        fid = a.get("fileId", "")
                        if fid and fid not in all_anexos_ids:
                            all_anexos_ids.add(fid)
                            anexos_todos.append(a)
                            logger.info(f"[ClassroomV3]   Anexo: {a['nome']} | tipo={a['tipo']} | id={fid}")

            logger.info(f"[ClassroomV3] Total: {len(dados['materiais'])} materiais, {len(dados['textos'])} textos, {len(anexos_todos)} anexos")

            # Filtrar: só baixar arquivos novos
            for anexo in anexos_todos:
                anexo_nome = anexo.get("nome", "")
                file_id = anexo.get("fileId", "")
                if not file_id:
                    continue
                if anexo_nome in existentes:
                    logger.info(f"[ClassroomV3] Já existe no Drive: {anexo_nome}")
                    dados["arquivos_existentes"].append(anexo_nome)
                    continue
                arquivos_para_baixar.append(anexo)

        except Exception as e:
            logger.error(f"[ClassroomV3] Erro geral turma (fase 1): {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro geral: {str(e)}")
        finally:
            await browser.close()
            gc.collect()
            logger.info("[ClassroomV3] Browser FASE 1 fechado e memória liberada")

    # =============================================
    # FASE 2: Download em batches de BATCH_SIZE
    # Abre e fecha o browser a cada batch
    # =============================================
    if not arquivos_para_baixar:
        logger.info("[ClassroomV3] Nenhum arquivo novo para baixar")
    else:
        total = len(arquivos_para_baixar)
        logger.info(f"[ClassroomV3] {total} arquivos para baixar em batches de {BATCH_SIZE}")

        for batch_start in range(0, total, BATCH_SIZE):
            batch = arquivos_para_baixar[batch_start:batch_start + BATCH_SIZE]
            batch_num = (batch_start // BATCH_SIZE) + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            logger.info(f"[ClassroomV3] === BATCH {batch_num}/{total_batches} ({len(batch)} arquivos) ===")

            async with async_playwright() as p:
                browser, context, page = await criar_browser(p)
                try:
                    logged_in = await google_login(page, email, password)
                    if not logged_in:
                        dados["erros"].append(f"Falha no login (batch {batch_num})")
                        continue

                    # Criar aba de download para este batch
                    download_page = await context.new_page()
                    logger.info(f"[ClassroomV3] Aba de download criada para batch {batch_num}")

                    for i, anexo in enumerate(batch):
                        anexo_nome = anexo.get("nome", "")
                        file_id = anexo.get("fileId", "")
                        global_idx = batch_start + i + 1

                        logger.info(f"[ClassroomV3] Baixando [{global_idx}/{total}]: {anexo_nome}")
                        try:
                            result = await download_arquivo(download_page, anexo)

                            if result and not result.get("error") and result.get("size", 0) > 0:
                                dados["arquivos_novos"].append({
                                    "nome": anexo_nome,
                                    "file_id": file_id,
                                    "tipo": anexo.get("tipo", ""),
                                    "tamanho": result.get("size", 0),
                                    "filename": result.get("filename", anexo_nome),
                                    "file_key": result.get("file_key", ""),
                                    "turma": req.turma_nome
                                })
                                logger.info(f"[ClassroomV3] Download OK: {anexo_nome} | {result.get('size', 0)} bytes | key={result.get('file_key', '')}")
                            else:
                                error_msg = result.get("error", "Erro desconhecido") if result else "Sem resposta"
                                dados["erros"].append(f"Download falhou: {anexo_nome} - {error_msg}")
                                logger.error(f"[ClassroomV3] Falhou: {anexo_nome} - {error_msg}")

                        except Exception as e:
                            dados["erros"].append(f"Erro download: {anexo_nome} - {str(e)}")
                            logger.error(f"[ClassroomV3] Erro: {anexo_nome} - {e}")

                        gc.collect()

                    # Fechar aba de download
                    try:
                        await download_page.close()
                    except:
                        pass

                except Exception as e:
                    logger.error(f"[ClassroomV3] Erro batch {batch_num}: {e}")
                    dados["erros"].append(f"Erro batch {batch_num}: {str(e)}")
                finally:
                    await browser.close()
                    gc.collect()
                    logger.info(f"[ClassroomV3] Browser BATCH {batch_num} fechado e memória liberada")

            # Pausa entre batches para garantir limpeza de memória
            await asyncio.sleep(2)

    dados["resumo"] = {
        "turma": req.turma_nome,
        "total_materiais": len(dados["materiais"]),
        "total_anexos": len(dados["arquivos_novos"]) + len(dados["arquivos_existentes"]),
        "total_arquivos_novos": len(dados["arquivos_novos"]),
        "total_arquivos_existentes": len(dados["arquivos_existentes"]),
        "total_textos": len(dados["textos"]),
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
    """Consulta status de um job do Classroom V3."""
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


@router.get("/files/{file_key}")
async def download_temp_file(file_key: str, authorization: str = Header(None)):
    """
    v3.8.2: Endpoint para download de arquivo temporário.
    O n8n chama este endpoint para baixar 1 arquivo por vez e fazer upload no Drive.
    Após o download, o arquivo é marcado como baixado (mas não deletado imediatamente).
    """
    verificar_auth(authorization)
    if file_key not in temp_files:
        raise HTTPException(status_code=404, detail=f"Arquivo {file_key} não encontrado ou já expirou")
    
    file_info = temp_files[file_key]
    file_path = file_info["path"]
    
    if not os.path.exists(file_path):
        del temp_files[file_key]
        raise HTTPException(status_code=404, detail=f"Arquivo {file_key} não encontrado no disco")
    
    file_info["downloaded"] = True
    logger.info(f"[ClassroomV3] Download temp file: {file_key} -> {file_info['filename']} ({file_info['size']} bytes)")
    
    return FileResponse(
        path=file_path,
        filename=file_info["filename"],
        media_type="application/octet-stream"
    )


@router.delete("/files/{file_key}")
async def delete_temp_file(file_key: str, authorization: str = Header(None)):
    """
    v3.8.2: Endpoint para limpar arquivo temporário após upload no Drive.
    O n8n chama este endpoint após confirmar o upload.
    """
    verificar_auth(authorization)
    if file_key in temp_files:
        file_info = temp_files[file_key]
        try:
            if os.path.exists(file_info["path"]):
                os.remove(file_info["path"])
        except:
            pass
        del temp_files[file_key]
        return {"status": "deleted", "file_key": file_key}
    return {"status": "not_found", "file_key": file_key}


@router.post("/turmas")
async def endpoint_listar_turmas(
    req: TurmasRequest = TurmasRequest(),
    background_tasks: BackgroundTasks = None,
    authorization: str = Header(None),
    async_mode: bool = True
):
    """Lista todas as turmas do Google Classroom."""
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
    Coleta materiais, textos e arquivos de 1 turma.
    v3.8.2: Retorna file_key para cada arquivo. O n8n baixa via GET /files/{file_key}.
    """
    verificar_auth(authorization)
    job_id = create_classroom_job(f"classroom-turma-{req.turma_nome[:20]}")
    background_tasks.add_task(run_classroom_job, job_id, f"classroom-turma", scrape_coletar_turma, req)
    return {"job_id": job_id, "status": "processing", "poll_url": f"/scrape/classroom/turmas/job/{job_id}"}
