"""
Classroom V3 — Endpoints fragmentados com download por tipo de arquivo
Versão: 3.6.1 — Fix: reutilizar mesma aba para downloads (economia de memória)

Endpoints:
  POST /scrape/classroom/turmas  - Lista todas as turmas do Classroom
  POST /scrape/classroom/turma   - Coleta materiais e arquivos de 1 turma (com download)

Tipos de download suportados:
  - Google Docs   → .docx (export URL direto)
  - Google Slides → .pptx (export URL direto)
  - Google Sheets → .xlsx (export URL direto)
  - PDF           → .pdf  (Drive viewer → botão Baixar)
  - Imagem        → .png/.jpg original (Drive viewer → botão Baixar)
  - Office files  → formato original (Drive viewer → botão Baixar)

Arquitetura:
  - Cada chamada abre e fecha o browser (1 turma = 1 browser = pouca memória)
  - UMA ÚNICA aba de download é reutilizada para todos os arquivos (fix v3.6.0)
  - O n8n faz o inventário no Drive e orquestra as chamadas
  - A API faz scraping + download, retorna arquivos em base64
  - Nenhuma conversão de formato — tudo no formato original
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


# ============================================================
# LOGIN GOOGLE
# ============================================================
async def google_login(page, email: str, password: str, max_retries: int = 3):
    """Login no Google com tratamento do campo hidden decoy."""
    for attempt in range(max_retries):
        try:
            logger.info(f"[ClassroomV3] Login tentativa {attempt + 1}/{max_retries}...")
            await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            current_url = page.url
            if "myaccount.google.com" in current_url or "classroom.google.com" in current_url:
                logger.info("[ClassroomV3] Já logado!")
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
                logger.info(f"[ClassroomV3] Login OK! URL: {current_url}")
                return True

            has_captcha = await page.locator('iframe[title*="recaptcha"]:visible').count() > 0
            if has_captcha:
                logger.error("[ClassroomV3] CAPTCHA detectado!")
                return False

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
        accept_downloads=True  # Essencial para capturar downloads
    )
    page = await context.new_page()
    return browser, context, page


# ============================================================
# DOWNLOAD HELPERS — Por tipo de arquivo
# Todas as funções agora recebem uma `page` existente e a REUTILIZAM
# em vez de criar novas abas (fix v3.6.0 — economia de memória)
# ============================================================

async def download_drive_file(page, file_id: str, nome: str) -> dict:
    """
    Download de arquivo do Drive (PDF, imagem, Office, etc.)
    REUTILIZA a page existente — navega, baixa, e limpa.
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
            # Usar expect_download para capturar o arquivo
            try:
                async with page.expect_download(timeout=30000) as download_info:
                    await download_btn.first.click()
                download = await download_info.value
                # Salvar em temp e ler
                tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
                await download.save_as(tmp_path)
                suggested_name = download.suggested_filename or nome
                with open(tmp_path, "rb") as f:
                    content = f.read()
                os.remove(tmp_path)
                result = {
                    "data": base64.b64encode(content).decode(),
                    "size": len(content),
                    "filename": suggested_name
                }
                del content  # Liberar memória imediatamente
                gc.collect()
                return result
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
                        await page.goto(export_url)
                    download = await download_info.value
                    tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
                    await download.save_as(tmp_path)
                    suggested_name = download.suggested_filename or nome
                    with open(tmp_path, "rb") as f:
                        content = f.read()
                    os.remove(tmp_path)
                    result = {
                        "data": base64.b64encode(content).decode(),
                        "size": len(content),
                        "filename": suggested_name
                    }
                    del content
                    gc.collect()
                    return result
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
    Download de Google Docs como .docx via export URL direto.
    REUTILIZA a page existente.
    """
    try:
        # Export URL direto — mais confiável e usa menos memória
        export_url = f"https://docs.google.com/document/d/{file_id}/export?format=docx"
        logger.info(f"[ClassroomV3] download_google_doc: {nome} -> export docx")

        try:
            async with page.expect_download(timeout=30000) as download_info:
                await page.goto(export_url)
            download = await download_info.value
            tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
            await download.save_as(tmp_path)
            suggested_name = download.suggested_filename or f"{nome}.docx"
            with open(tmp_path, "rb") as f:
                content = f.read()
            os.remove(tmp_path)
            if len(content) > 0:
                result = {
                    "data": base64.b64encode(content).decode(),
                    "size": len(content),
                    "filename": suggested_name
                }
                del content
                gc.collect()
                return result
        except Exception as e:
            logger.warning(f"[ClassroomV3] Export URL falhou para Google Doc: {e}")

        # Fallback: abrir editor e usar menu Arquivo → Baixar
        doc_url = f"https://docs.google.com/document/d/{file_id}/edit"
        await page.goto(doc_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        try:
            file_menu = page.locator('#docs-file-menu, [aria-label="Arquivo"]')
            if await file_menu.count() > 0:
                await file_menu.first.click()
                await page.wait_for_timeout(1000)

                download_menu = page.locator('[aria-label*="Baixar"], [aria-label*="Download"], :text("Baixar")')
                if await download_menu.count() > 0:
                    await download_menu.first.click()
                    await page.wait_for_timeout(1000)

                    async with page.expect_download(timeout=30000) as download_info:
                        docx_option = page.locator(':text("Microsoft Word"), :text(".docx")')
                        if await docx_option.count() > 0:
                            await docx_option.first.click()

                    download = await download_info.value
                    tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
                    await download.save_as(tmp_path)
                    suggested_name = download.suggested_filename or f"{nome}.docx"
                    with open(tmp_path, "rb") as f:
                        content = f.read()
                    os.remove(tmp_path)
                    result = {
                        "data": base64.b64encode(content).decode(),
                        "size": len(content),
                        "filename": suggested_name
                    }
                    del content
                    gc.collect()
                    return result
        except Exception as e:
            logger.error(f"[ClassroomV3] Menu download falhou para Google Doc: {e}")

        return {"error": "Não conseguiu baixar Google Doc"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_google_doc erro: {e}")
        return {"error": str(e)}


async def download_google_slides(page, file_id: str, nome: str) -> dict:
    """
    Download de Google Slides como .pptx via export URL direto.
    REUTILIZA a page existente.
    """
    try:
        # Export URL direto
        export_url = f"https://docs.google.com/presentation/d/{file_id}/export?format=pptx"
        logger.info(f"[ClassroomV3] download_google_slides: {nome} -> export pptx")

        try:
            async with page.expect_download(timeout=30000) as download_info:
                await page.goto(export_url)
            download = await download_info.value
            tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
            await download.save_as(tmp_path)
            suggested_name = download.suggested_filename or f"{nome}.pptx"
            with open(tmp_path, "rb") as f:
                content = f.read()
            os.remove(tmp_path)
            if len(content) > 0:
                result = {
                    "data": base64.b64encode(content).decode(),
                    "size": len(content),
                    "filename": suggested_name
                }
                del content
                gc.collect()
                return result
        except Exception as e:
            logger.warning(f"[ClassroomV3] Export URL falhou para Slides: {e}")

        # Fallback: abrir editor e usar menu
        slides_url = f"https://docs.google.com/presentation/d/{file_id}/edit"
        await page.goto(slides_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        try:
            file_menu = page.locator('#docs-file-menu, [aria-label="Arquivo"]')
            if await file_menu.count() > 0:
                await file_menu.first.click()
                await page.wait_for_timeout(1000)

                download_menu = page.locator('[aria-label*="Baixar"], [aria-label*="Download"], :text("Baixar")')
                if await download_menu.count() > 0:
                    await download_menu.first.click()
                    await page.wait_for_timeout(1000)

                    async with page.expect_download(timeout=30000) as download_info:
                        pptx_option = page.locator(':text("Microsoft PowerPoint"), :text(".pptx")')
                        if await pptx_option.count() > 0:
                            await pptx_option.first.click()

                    download = await download_info.value
                    tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
                    await download.save_as(tmp_path)
                    suggested_name = download.suggested_filename or f"{nome}.pptx"
                    with open(tmp_path, "rb") as f:
                        content = f.read()
                    os.remove(tmp_path)
                    result = {
                        "data": base64.b64encode(content).decode(),
                        "size": len(content),
                        "filename": suggested_name
                    }
                    del content
                    gc.collect()
                    return result
        except Exception as e:
            logger.error(f"[ClassroomV3] Menu download falhou para Slides: {e}")

        return {"error": "Não conseguiu baixar Google Slides"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_google_slides erro: {e}")
        return {"error": str(e)}


async def download_google_sheets(page, file_id: str, nome: str) -> dict:
    """
    Download de Google Sheets como .xlsx via export URL direto.
    REUTILIZA a page existente.
    """
    try:
        # Export URL direto
        export_url = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx"
        logger.info(f"[ClassroomV3] download_google_sheets: {nome} -> export xlsx")

        try:
            async with page.expect_download(timeout=30000) as download_info:
                await page.goto(export_url)
            download = await download_info.value
            tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
            await download.save_as(tmp_path)
            suggested_name = download.suggested_filename or f"{nome}.xlsx"
            with open(tmp_path, "rb") as f:
                content = f.read()
            os.remove(tmp_path)
            if len(content) > 0:
                result = {
                    "data": base64.b64encode(content).decode(),
                    "size": len(content),
                    "filename": suggested_name
                }
                del content
                gc.collect()
                return result
        except Exception as e:
            logger.warning(f"[ClassroomV3] Export URL falhou para Sheets: {e}")

        # Fallback: abrir editor e usar menu
        sheets_url = f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
        await page.goto(sheets_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        try:
            file_menu = page.locator('#docs-file-menu, [aria-label="Arquivo"]')
            if await file_menu.count() > 0:
                await file_menu.first.click()
                await page.wait_for_timeout(1000)

                download_menu = page.locator('[aria-label*="Baixar"], [aria-label*="Download"], :text("Baixar")')
                if await download_menu.count() > 0:
                    await download_menu.first.click()
                    await page.wait_for_timeout(1000)

                    async with page.expect_download(timeout=30000) as download_info:
                        xlsx_option = page.locator(':text("Microsoft Excel"), :text(".xlsx")')
                        if await xlsx_option.count() > 0:
                            await xlsx_option.first.click()

                    download = await download_info.value
                    tmp_path = f"/tmp/classroom_dl_{uuid.uuid4().hex[:8]}"
                    await download.save_as(tmp_path)
                    suggested_name = download.suggested_filename or f"{nome}.xlsx"
                    with open(tmp_path, "rb") as f:
                        content = f.read()
                    os.remove(tmp_path)
                    result = {
                        "data": base64.b64encode(content).decode(),
                        "size": len(content),
                        "filename": suggested_name
                    }
                    del content
                    gc.collect()
                    return result
        except Exception as e:
            logger.error(f"[ClassroomV3] Menu download falhou para Sheets: {e}")

        return {"error": "Não conseguiu baixar Google Sheets"}

    except Exception as e:
        logger.error(f"[ClassroomV3] download_google_sheets erro: {e}")
        return {"error": str(e)}


async def download_arquivo(page, anexo: dict) -> dict:
    """
    Router de download — escolhe a estratégia correta por tipo de arquivo.
    REUTILIZA a mesma page para todos os downloads.
    Retorna dict com data (base64), size, filename ou error.
    """
    file_id = anexo.get("fileId", "")
    nome = anexo.get("nome", "arquivo")
    tipo = anexo.get("tipo", "drive_file")

    if not file_id:
        return {"error": "file_id vazio"}

    logger.info(f"[ClassroomV3] Download: {nome} | tipo={tipo} | id={file_id}")

    if tipo == "google_doc":
        return await download_google_doc(page, file_id, nome)
    elif tipo == "google_slides":
        return await download_google_slides(page, file_id, nome)
    elif tipo == "google_sheets":
        return await download_google_sheets(page, file_id, nome)
    else:
        # drive_file: PDF, imagem, Office, etc — formato original
        return await download_drive_file(page, file_id, nome)


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


# ============================================================
# SCRAPING — COLETAR 1 TURMA (materiais + texto + download)
# ============================================================
async def scrape_coletar_turma(req: TurmaRequest) -> dict:
    """
    Coleta materiais, textos e arquivos de 1 turma.
    Compara com arquivos_existentes (inventário do Drive) e só baixa os novos.
    Retorna arquivos em base64 para o n8n fazer upload.
    
    v3.6.0: Reutiliza UMA ÚNICA aba de download para todos os arquivos.
    Isso reduz drasticamente o uso de memória (de N abas para 1 aba).
    """
    from playwright.async_api import async_playwright

    dados = {
        "turma": req.turma_nome,
        "turma_link": req.turma_link,
        "materiais": [],
        "arquivos_novos": [],
        "arquivos_existentes": [],
        "textos": [],
        "erros": []
    }

    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD
    existentes = set(req.arquivos_existentes)

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

            # 2. Primeiro ir ao Classroom para garantir sessão
            logger.info("[ClassroomV3] Navegando para Classroom principal...")
            await page.goto("https://classroom.google.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            logger.info(f"[ClassroomV3] URL após ir ao Classroom: {page.url}")

            # 3. Navegar para a aba Atividades da turma
            course_id_match = re.search(r'/c/(\w+)', req.turma_link)
            if course_id_match:
                course_id = course_id_match.group(1)
                atividades_url = f"https://classroom.google.com/w/{course_id}/t/all"
            else:
                atividades_url = req.turma_link

            logger.info(f"[ClassroomV3] Acessando turma: {req.turma_nome} -> {atividades_url}")
            await page.goto(atividades_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            logger.info(f"[ClassroomV3] URL final após navegar para turma: {page.url}")

            # Se redirecionou para a página principal, tentar URL alternativa
            if "/w/" not in page.url and "/c/" not in page.url:
                logger.warning(f"[ClassroomV3] Redirecionou! Tentando URL direta da turma...")
                await page.goto(req.turma_link, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
                logger.info(f"[ClassroomV3] URL após turma_link direto: {page.url}")
                
                # Agora tentar clicar na aba "Atividades"
                atividades_tab = page.locator('a[href*="/w/"], [aria-label*="Atividades"], :text("Atividades")')
                if await atividades_tab.count() > 0:
                    await atividades_tab.first.click()
                    await page.wait_for_timeout(3000)
                    logger.info(f"[ClassroomV3] URL após clicar Atividades: {page.url}")

            if "classroom.google.com" not in page.url:
                dados["erros"].append(f"Não acessou a turma. URL: {page.url}")
                return dados

            logger.info(f"[ClassroomV3] Dentro da turma! URL: {page.url}")

            # 3. Rolar para carregar todos os materiais
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

            # 4. Coletar materiais: expandir e extrair info
            materiais_info = await page.evaluate("""
                () => {
                    const result = [];
                    const ignorar = ['menu principal', 'google apps', 'minhas inscrições', 
                                     'conta do google', 'ajuda e comentários', 'filtro de tópicos',
                                     'opções de temas', 'opções do material', 'opções da atividade',
                                     'fechar todas', 'recolher tudo', 'seus trabalhos'];
                    
                    const items = document.querySelectorAll('div[role="button"]');
                    items.forEach((el) => {
                        const hint = el.getAttribute('hint') || '';
                        const ariaLabel = el.getAttribute('aria-label') || '';
                        const text = el.textContent?.trim()?.substring(0, 200) || '';
                        const nome = hint || ariaLabel || text;
                        const nomeLower = nome.toLowerCase();
                        
                        // Filtrar UI e tópicos
                        const ehUI = ignorar.some(ig => nomeLower.startsWith(ig));
                        const ehTopico = nomeLower.startsWith('tópico:') || nomeLower.startsWith('topico:');
                        const ehOpcoes = nomeLower.startsWith('opções');
                        const ehNavegacao = ['avançar', 'voltar', 'diminuir', 'redefinir', 'aumentar', 
                                            'fechar', 'abrir com', 'adicionar atalho', 'imprimir', 
                                            'mais ações', 'editar'].some(n => nomeLower.startsWith(n));
                        
                        if (!ehUI && !ehTopico && !ehOpcoes && !ehNavegacao && nome.length > 3) {
                            result.push({
                                nome: nome.substring(0, 200),
                                hasExpanded: el.hasAttribute('aria-expanded'),
                                expanded: el.getAttribute('aria-expanded')
                            });
                        }
                    });
                    return result;
                }
            """)

            logger.info(f"[ClassroomV3] {len(materiais_info)} materiais encontrados")
            for m in materiais_info:
                logger.info(f"[ClassroomV3]   Material: {m['nome'][:80]}")

            # 5. Expandir todos os materiais que estão fechados
            await page.evaluate("""
                () => {
                    const ignorar = ['menu principal', 'google apps', 'minhas inscrições', 
                                     'conta do google', 'ajuda e comentários', 'filtro de tópicos',
                                     'opções de temas', 'opções do material', 'opções da atividade',
                                     'fechar todas', 'recolher tudo', 'seus trabalhos'];
                    
                    const items = document.querySelectorAll('div[role="button"][aria-expanded="false"]');
                    items.forEach(el => {
                        const hint = el.getAttribute('hint') || '';
                        const ariaLabel = el.getAttribute('aria-label') || '';
                        const text = el.textContent?.trim()?.substring(0, 200) || '';
                        const nome = (hint || ariaLabel || text).toLowerCase();
                        
                        const ehUI = ignorar.some(ig => nome.startsWith(ig));
                        const ehTopico = nome.startsWith('tópico:') || nome.startsWith('topico:');
                        const ehOpcoes = nome.startsWith('opções');
                        
                        if (!ehUI && !ehTopico && !ehOpcoes && nome.length > 3) {
                            el.click();
                        }
                    });
                }
            """)

            await page.wait_for_timeout(3000)

            # 6. Coletar textos dos materiais expandidos
            textos_materiais = await page.evaluate("""
                () => {
                    const textos = [];
                    const contentDivs = document.querySelectorAll('.OHJHx, .z3vRcc, .asQXV');
                    contentDivs.forEach(div => {
                        const text = div.textContent?.trim();
                        if (text && text.length > 10) {
                            textos.push(text.substring(0, 2000));
                        }
                    });
                    if (textos.length === 0) {
                        const allText = document.body.innerText;
                        textos.push(allText.substring(0, 5000));
                    }
                    return textos;
                }
            """)
            dados["textos"] = textos_materiais

            # 7. Coletar TODOS os anexos (links do Drive/Docs/Slides)
            anexos_todos = await page.evaluate("""
                () => {
                    const anexos = [];
                    
                    // Método 1: links com hint "Anexo: ..."
                    document.querySelectorAll('a[hint*="Anexo:"], a[aria-label*="Anexo:"]').forEach(a => {
                        const hint = a.getAttribute('hint') || a.getAttribute('aria-label') || '';
                        const nome = a.textContent?.trim()?.split('\\n')[0] || '';
                        const url = a.href || '';
                        
                        let tipo = 'drive_file';
                        const hintLower = hint.toLowerCase();
                        if (hintLower.includes('google docs') || hintLower.includes('documento')) tipo = 'google_doc';
                        else if (hintLower.includes('google slides') || hintLower.includes('apresentaç')) tipo = 'google_slides';
                        else if (hintLower.includes('google sheets') || hintLower.includes('planilha')) tipo = 'google_sheets';
                        else if (hintLower.includes('imagem') || hintLower.includes('image')) tipo = 'imagem';
                        else if (hintLower.includes('pdf')) tipo = 'pdf';
                        
                        if (url.includes('docs.google.com/document')) tipo = 'google_doc';
                        else if (url.includes('docs.google.com/presentation') || url.includes('slides.google.com')) tipo = 'google_slides';
                        else if (url.includes('docs.google.com/spreadsheets')) tipo = 'google_sheets';
                        
                        const match = url.match(/\\/d\\/([a-zA-Z0-9_-]+)/);
                        const fileId = match ? match[1] : '';
                        
                        if (fileId && !anexos.find(x => x.fileId === fileId)) {
                            anexos.push({ nome, url, fileId, tipo, hint });
                        }
                    });
                    
                    // Método 2: links diretos do Drive/Docs (fallback)
                    if (anexos.length === 0) {
                        const seletores = [
                            'a[href*="drive.google.com/file"]',
                            'a[href*="docs.google.com/document"]',
                            'a[href*="docs.google.com/presentation"]',
                            'a[href*="docs.google.com/spreadsheets"]'
                        ];
                        document.querySelectorAll(seletores.join(', ')).forEach(a => {
                            const nome = a.textContent?.trim()?.split('\\n')[0] || '';
                            const url = a.href || '';
                            if (url && nome && !nome.includes('Pasta da turma')) {
                                let tipo = 'drive_file';
                                if (url.includes('docs.google.com/document')) tipo = 'google_doc';
                                else if (url.includes('docs.google.com/presentation')) tipo = 'google_slides';
                                else if (url.includes('docs.google.com/spreadsheets')) tipo = 'google_sheets';
                                
                                const match = url.match(/\\/d\\/([a-zA-Z0-9_-]+)/);
                                const fileId = match ? match[1] : '';
                                
                                if (fileId && !anexos.find(x => x.fileId === fileId)) {
                                    anexos.push({ nome, url, fileId, tipo, hint: '' });
                                }
                            }
                        });
                    }
                    
                    return anexos;
                }
            """)

            logger.info(f"[ClassroomV3] {len(anexos_todos)} anexos encontrados")
            for a in anexos_todos:
                logger.info(f"[ClassroomV3]   Anexo: {a['nome']} | tipo={a['tipo']} | id={a['fileId']}")

            # Salvar materiais com seus metadados
            for mat in materiais_info:
                dados["materiais"].append({
                    "nome": mat.get("nome", ""),
                    "anexos_count": len(anexos_todos)
                })

            # 8. Para cada anexo: verificar inventário → baixar se novo
            # >>> FIX v3.6.0: Criar UMA ÚNICA aba de download e reutilizar <<<
            download_page = None
            arquivos_para_baixar = []

            for anexo in anexos_todos:
                anexo_nome = anexo.get("nome", "")
                file_id = anexo.get("fileId", "")

                if not file_id:
                    continue

                # Verificar inventário
                if anexo_nome in existentes:
                    logger.info(f"[ClassroomV3] Já existe no Drive: {anexo_nome}")
                    dados["arquivos_existentes"].append(anexo_nome)
                    continue

                arquivos_para_baixar.append(anexo)

            if arquivos_para_baixar:
                logger.info(f"[ClassroomV3] {len(arquivos_para_baixar)} arquivos para baixar")
                
                # Criar UMA aba de download
                download_page = await context.new_page()
                logger.info("[ClassroomV3] Aba de download criada (será reutilizada para todos os arquivos)")

                for i, anexo in enumerate(arquivos_para_baixar):
                    anexo_nome = anexo.get("nome", "")
                    file_id = anexo.get("fileId", "")

                    logger.info(f"[ClassroomV3] Baixando [{i+1}/{len(arquivos_para_baixar)}]: {anexo_nome}")
                    try:
                        # Reutilizar a MESMA aba para cada download
                        result = await download_arquivo(download_page, anexo)

                        if result and not result.get("error") and result.get("size", 0) > 0:
                            dados["arquivos_novos"].append({
                                "nome": anexo_nome,
                                "file_id": file_id,
                                "tipo": anexo.get("tipo", ""),
                                "tamanho": result.get("size", 0),
                                "filename": result.get("filename", anexo_nome),
                                "conteudo_base64": result.get("data", ""),
                                "turma": req.turma_nome
                            })
                            logger.info(f"[ClassroomV3] Download OK: {anexo_nome} | {result.get('size', 0)} bytes | {result.get('filename', '')}")
                        else:
                            error_msg = result.get("error", "Erro desconhecido") if result else "Sem resposta"
                            dados["erros"].append(f"Download falhou: {anexo_nome} - {error_msg}")
                            logger.error(f"[ClassroomV3] Falhou: {anexo_nome} - {error_msg}")

                    except Exception as e:
                        dados["erros"].append(f"Erro download: {anexo_nome} - {str(e)}")
                        logger.error(f"[ClassroomV3] Erro: {anexo_nome} - {e}")

                    # Forçar limpeza de memória após cada download
                    gc.collect()
                    logger.info(f"[ClassroomV3] gc.collect() após download {i+1}")

                # Fechar a aba de download no final
                if download_page:
                    try:
                        await download_page.close()
                        logger.info("[ClassroomV3] Aba de download fechada")
                    except:
                        pass
            else:
                logger.info("[ClassroomV3] Nenhum arquivo novo para baixar")

        except Exception as e:
            logger.error(f"[ClassroomV3] Erro geral turma: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro geral: {str(e)}")
        finally:
            await browser.close()
            gc.collect()

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
    Compara com inventário (arquivos_existentes) e só baixa os novos.
    Retorna arquivos em base64 + textos para o n8n fazer upload no Drive.
    """
    verificar_auth(authorization)
    job_id = create_classroom_job(f"classroom-turma-{req.turma_nome[:20]}")
    background_tasks.add_task(run_classroom_job, job_id, f"classroom-turma", scrape_coletar_turma, req)
    return {"job_id": job_id, "status": "processing", "poll_url": f"/scrape/classroom/turmas/job/{job_id}"}
