#!/usr/bin/env python3
"""
Melissa Scraper API v3.0 — Playwright Edition
Usa Playwright (headless Chromium) para navegação real nos portais escolares.
Deploy no Render.com via Docker.

Endpoints:
  POST /scrape/classroom  - Coleta turmas, atividades e materiais do Google Classroom
  POST /scrape/superapp   - Coleta dados do SuperApp Layers (notas, conteúdos, registros)
  POST /scrape/roteiro    - Coleta dados do Roteiro de Estudos (provas AO/AD)
  POST /scrape/all        - Executa todos de uma vez
  GET  /health            - Health check
"""

import os
import json
import re
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel

# ============================================================
# CONFIGURAÇÃO
# ============================================================
API_SECRET = os.environ.get("MELISSA_API_SECRET", "") or os.environ.get("MELISSA_API_KEY", "trocar-por-uma-chave-segura")

# Credenciais da Melissa (conta escolar)
MELISSA_EMAIL = os.environ.get("MELISSA_EMAIL", "melissa.marinho@liceujardim.g12.br")
MELISSA_PASSWORD = os.environ.get("MELISSA_PASSWORD", "elvis!!1")

# Credenciais do SuperApp (pode ser diferente)
SUPERAPP_EMAIL = os.environ.get("SUPERAPP_EMAIL", MELISSA_EMAIL)
SUPERAPP_PASSWORD = os.environ.get("SUPERAPP_PASSWORD", MELISSA_PASSWORD)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
    title="Melissa Scraper API",
    description="API de scraping com Playwright para a Agente Melissa",
    version="3.0.0"
)


def verificar_auth(authorization: str = Header(None)):
    if not API_SECRET:
        return  # Se não configurou secret, aceita tudo
    if not authorization or authorization.replace("Bearer ", "") != API_SECRET:
        raise HTTPException(status_code=401, detail="Chave de API inválida")


# ============================================================
# PLAYWRIGHT - LOGIN GOOGLE
# ============================================================
async def google_login(page, email: str, password: str, max_retries: int = 3):
    """
    Faz login no Google com email e senha.
    Funciona para Classroom, Drive, e qualquer serviço Google.
    IMPORTANTE: Requer Xvfb (display virtual) para evitar CAPTCHA.
    A URL /challenge/pwd é o fluxo NORMAL de senha, NÃO é CAPTCHA.
    """
    for attempt in range(max_retries):
        try:
            logger.info(f"Tentativa de login Google {attempt + 1}/{max_retries}...")

            # Navegar para o login do Google
            await page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Verificar se já está logado
            current_url = page.url
            if "myaccount.google.com" in current_url or "classroom.google.com" in current_url:
                logger.info("Já está logado!")
                return True

            # Inserir email
            email_input = page.locator('input[type="email"]')
            await email_input.wait_for(state="visible", timeout=10000)
            await email_input.fill(email)
            await page.wait_for_timeout(500)
            await page.locator('#identifierNext button').click()
            await page.wait_for_timeout(4000)

            # Aguardar tela de senha (/challenge/pwd é NORMAL, não é CAPTCHA)
            password_input = page.locator('input[type="password"]')
            await password_input.wait_for(state="visible", timeout=15000)
            await password_input.fill(password)
            await page.wait_for_timeout(500)
            await page.locator('#passwordNext button').click()
            await page.wait_for_timeout(5000)

            # Verificar se login foi bem-sucedido
            current_url = page.url
            if "accounts.google.com" not in current_url:
                logger.info(f"Login Google bem-sucedido! URL: {current_url}")
                return True

            # Verificar CAPTCHA real (não confundir com /challenge/pwd)
            page_content = await page.content()
            has_visible_captcha = await page.locator('iframe[title*="recaptcha"]:visible').count() > 0
            if has_visible_captcha:
                logger.error("CAPTCHA visível detectado! Verifique se Xvfb está ativo.")
                return False

            # Pode ter redirecionamento extra, esperar mais
            await page.wait_for_timeout(5000)
            current_url = page.url
            if "accounts.google.com" not in current_url:
                logger.info(f"Login Google bem-sucedido (após redirect)! URL: {current_url}")
                return True

            logger.warning(f"Login pode ter falhado. URL: {current_url}")

        except Exception as e:
            logger.error(f"Erro no login (tentativa {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await page.wait_for_timeout(3000)

    return False


# ============================================================
# SCRAPING - GOOGLE CLASSROOM
# ============================================================
async def scrape_classroom_async(req: ScrapeRequest) -> dict:
    """
    Navega no Google Classroom via Playwright e coleta:
    - Lista de turmas do 8E
    - Atividades de cada turma (título, data, descrição, status)
    - Materiais e arquivos anexados (com links do Drive)
    """
    from playwright.async_api import async_playwright

    dados = {
        "turmas": [],
        "atividades": [],
        "materiais": [],
        "arquivos_para_download": [],
        "erros": []
    }

    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD

    async with async_playwright() as p:
        # headless=False + Xvfb = browser real com display virtual
        # Isso evita detecção de bot e CAPTCHA do Google
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

        try:
            # 1. Login no Google
            logger.info("Iniciando login no Google...")
            login_ok = await google_login(page, email, password)

            if not login_ok:
                dados["erros"].append("Falha no login Google. Verifique credenciais ou desafio de segurança.")
                await browser.close()
                return dados

            # 2. Navegar para o Classroom
            logger.info("Navegando para o Google Classroom...")
            await page.goto("https://classroom.google.com/", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(5000)

            # 3. Verificar se estamos no Classroom
            current_url = page.url
            if "classroom.google.com" not in current_url:
                dados["erros"].append(f"Não conseguiu acessar o Classroom. URL atual: {current_url}")
                await browser.close()
                return dados

            # 4. Coletar lista de turmas
            logger.info("Coletando lista de turmas...")
            await page.wait_for_timeout(3000)

            # Rolar para carregar todas as turmas
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

            # Extrair turmas via DOM
            turmas_raw = await page.evaluate("""
                () => {
                    const turmas = [];
                    // Cards de turma no Classroom
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

                    // Fallback: pegar todos os links de turmas
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

            # Filtrar turmas do 8E
            for turma in turmas_raw:
                dados["turmas"].append(turma)
                logger.info(f"Turma encontrada: {turma['nome']}")

            if not dados["turmas"]:
                # Fallback: extrair texto da página
                page_text = await page.evaluate("document.body.innerText")
                dados["erros"].append(f"Nenhuma turma encontrada via DOM. Texto da página (primeiros 2000 chars): {page_text[:2000]}")
                await browser.close()
                return dados

            # 5. Para cada turma, acessar atividades
            for turma in dados["turmas"]:
                turma_nome = turma["nome"]
                turma_link = turma.get("link", "")

                if not turma_link:
                    dados["erros"].append(f"Turma '{turma_nome}': sem link de acesso")
                    continue

                try:
                    logger.info(f"Acessando turma: {turma_nome}...")

                    # Navegar para a turma
                    await page.goto(turma_link, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(3000)

                    # Clicar na aba "Atividades" (Classwork)
                    try:
                        atividades_tab = page.locator('a:has-text("Atividades"), a:has-text("Classwork"), a[href*="/w/"]')
                        if await atividades_tab.count() > 0:
                            await atividades_tab.first.click()
                            await page.wait_for_timeout(3000)
                    except Exception as e:
                        logger.warning(f"Não encontrou aba Atividades: {e}")
                        # Tentar navegar direto
                        course_id_match = re.search(r'/c/(\w+)', turma_link)
                        if course_id_match:
                            await page.goto(f"https://classroom.google.com/w/{course_id_match.group(1)}/t/all", wait_until="networkidle", timeout=30000)
                            await page.wait_for_timeout(3000)

                    # Rolar para carregar todas as atividades
                    for _ in range(5):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(1000)

                    # Extrair atividades
                    atividades_raw = await page.evaluate("""
                        () => {
                            const items = [];
                            // Seletores do Classroom para atividades
                            const posts = document.querySelectorAll('.asQXV, [data-coursework-id], [data-stream-item-id]');
                            posts.forEach(post => {
                                const titulo = post.querySelector('.YVvGBb, .tL9Q4c, .onkcGd')?.textContent?.trim() || '';
                                const descricao = post.querySelector('.oKOVhd, .CkGfWc')?.textContent?.trim() || '';
                                const data = post.querySelector('.EhRlC, .lYVkk')?.textContent?.trim() || '';
                                const link = post.querySelector('a[href*="/c/"]')?.href || '';
                                const tipo = post.querySelector('.YVvGBb')?.closest('[data-item-type]')?.getAttribute('data-item-type') || '';

                                if (titulo) {
                                    items.push({ titulo, descricao, data, link, tipo, turma: '' });
                                }
                            });

                            // Fallback: pegar texto geral da página
                            if (items.length === 0) {
                                const allText = document.body.innerText;
                                items.push({ titulo: 'FALLBACK_TEXT', descricao: allText.substring(0, 5000), data: '', link: '', tipo: 'fallback', turma: '' });
                            }

                            return items;
                        }
                    """)

                    for ativ in atividades_raw:
                        ativ["turma"] = turma_nome
                        dados["atividades"].append(ativ)

                    logger.info(f"Turma '{turma_nome}': {len(atividades_raw)} atividades encontradas")

                    # 6. Clicar em cada atividade para ver detalhes e arquivos
                    atividade_links = await page.evaluate("""
                        () => {
                            const links = [];
                            document.querySelectorAll('a[href*="/c/"][href*="/a/"], a[href*="/c/"][href*="/m/"]').forEach(a => {
                                const href = a.href;
                                if (href && !links.includes(href)) {
                                    links.push(href);
                                }
                            });
                            return links;
                        }
                    """)

                    for ativ_link in atividade_links[:20]:  # Limitar a 20 por turma
                        try:
                            await page.goto(ativ_link, wait_until="networkidle", timeout=20000)
                            await page.wait_for_timeout(2000)

                            # Extrair detalhes da atividade
                            detalhes = await page.evaluate("""
                                () => {
                                    const titulo = document.querySelector('.YVvGBb, h1, .onkcGd')?.textContent?.trim() || '';
                                    const descricao = document.querySelector('.oKOVhd, .CkGfWc, [data-region="instructions"]')?.textContent?.trim() || '';
                                    const dataEntrega = document.querySelector('.EhRlC, .lYVkk, [data-region="due-date"]')?.textContent?.trim() || '';

                                    // Extrair arquivos/materiais
                                    const arquivos = [];
                                    document.querySelectorAll('a[href*="drive.google.com"], a[href*="docs.google.com"], a[href*="slides.google.com"]').forEach(a => {
                                        const nome = a.textContent?.trim() || '';
                                        const url = a.href || '';
                                        if (url) {
                                            // Extrair file ID do Google Drive
                                            const match = url.match(/\/d\/([a-zA-Z0-9_-]+)/);
                                            const fileId = match ? match[1] : '';
                                            arquivos.push({ nome, url, fileId });
                                        }
                                    });

                                    // Também pegar imagens e outros anexos
                                    document.querySelectorAll('img[src*="googleusercontent"], img[src*="drive.google"]').forEach(img => {
                                        arquivos.push({ nome: img.alt || 'imagem', url: img.src, fileId: '' });
                                    });

                                    return { titulo, descricao, dataEntrega, arquivos };
                                }
                            """)

                            if detalhes["arquivos"]:
                                for arq in detalhes["arquivos"]:
                                    arq["turma"] = turma_nome
                                    arq["atividade"] = detalhes["titulo"]
                                    dados["arquivos_para_download"].append(arq)

                            # Atualizar a atividade com detalhes
                            dados["materiais"].append({
                                "turma": turma_nome,
                                "titulo": detalhes["titulo"],
                                "descricao": detalhes["descricao"],
                                "dataEntrega": detalhes["dataEntrega"],
                                "arquivos": detalhes["arquivos"],
                                "link": ativ_link
                            })

                        except Exception as e:
                            logger.warning(f"Erro ao acessar atividade {ativ_link}: {e}")
                            dados["erros"].append(f"Atividade {ativ_link}: {str(e)}")

                except Exception as e:
                    logger.error(f"Erro na turma '{turma_nome}': {e}")
                    dados["erros"].append(f"Turma '{turma_nome}': {str(e)}")

        except Exception as e:
            logger.error(f"Erro geral no Classroom: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro geral: {str(e)}")

        finally:
            await browser.close()

    # Resumo
    dados["resumo"] = {
        "total_turmas": len(dados["turmas"]),
        "total_atividades": len(dados["atividades"]),
        "total_materiais": len(dados["materiais"]),
        "total_arquivos": len(dados["arquivos_para_download"]),
        "total_erros": len(dados["erros"])
    }

    return dados


# ============================================================
# SCRAPING - SUPERAPP LAYERS
# ============================================================
async def scrape_superapp_async(req: ScrapeRequest) -> dict:
    """
    Navega no SuperApp Layers via Playwright e coleta:
    - Notas Acadêmicas (Gradebooks) - via iframe layers-notas-academicas.web.app
    - Registros Acadêmicos (Academic Records) - via iframe layers-registros-academicos.web.app
    - Conteúdo de Aula (via Sophia iframe) - pesquisa por matéria, últimos 60 dias

    Login: direto no Layers (id.layers.digital) com email/senha.
    Usa mesma config do Classroom (headless=False + Xvfb).
    """
    from playwright.async_api import async_playwright

    dados = {
        "notas": [],
        "registros": [],
        "conteudos": [],
        "erros": []
    }

    email = req.email or SUPERAPP_EMAIL
    password = req.password or SUPERAPP_PASSWORD

    async with async_playwright() as p:
        # Mesma config do Classroom que funciona
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

        try:
            # ========================================
            # 1. LOGIN NO LAYERS
            # ========================================
            logger.info("Acessando SuperApp Layers...")
            await page.goto("https://liceu-jardim.layers.education/@liceu-jardim/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            # Verificar se redirecionou para login (id.layers.digital)
            current_url = page.url
            logger.info(f"URL atual: {current_url}")

            if "id.layers.digital" in current_url or "login" in current_url.lower():
                logger.info("Fazendo login no Layers...")

                # Inserir email
                email_input = page.locator('input[type="email"]')
                await email_input.wait_for(state="visible", timeout=10000)
                await email_input.fill(email)
                await page.wait_for_timeout(500)

                # Clicar Continue
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Continuar")')
                if await continue_btn.count() > 0:
                    await continue_btn.first.click()
                    logger.info("Clicou Continue, aguardando tela de senha...")
                    await page.wait_for_timeout(3000)

                # Inserir senha
                password_input = page.locator('input[type="password"]')
                await password_input.wait_for(state="visible", timeout=10000)
                await password_input.fill(password)
                await page.wait_for_timeout(500)

                # Clicar Enter/Entrar
                enter_btn = page.locator('button:has-text("Enter"), button:has-text("Entrar")')
                if await enter_btn.count() > 0:
                    await enter_btn.first.click()
                    logger.info("Clicou Enter, aguardando login completar...")
                    await page.wait_for_timeout(8000)

                current_url = page.url
                logger.info(f"URL após login: {current_url}")

            # Verificar se logou com sucesso
            page_text = await page.evaluate("document.body.innerText")
            if "Melissa" in page_text or "My Apps" in page_text or "Home" in page_text:
                logger.info("Login SuperApp OK!")
            else:
                logger.warning(f"Login pode ter falhado. Texto: {page_text[:300]}")
                dados["erros"].append("Login SuperApp pode ter falhado")

            # ========================================
            # 2. NOTAS ACADÊMICAS (GRADEBOOKS) - IFRAME cross-origin
            # ========================================
            logger.info("Acessando Notas Acadêmicas...")
            try:
                # Navegar direto para Gradebooks overview
                await page.goto(
                    "https://liceu-jardim.layers.education/@liceu-jardim/portal/@admin:layers-notas-academicas",
                    wait_until="domcontentloaded", timeout=30000
                )
                await page.wait_for_timeout(8000)  # Esperar mais para o SPA carregar

                # Clicar em "See grades" (botão na página principal, NÃO no iframe)
                see_grades = page.locator('button:has-text("See grades"), button:has-text("Ver notas")')
                sg_count = await see_grades.count()
                logger.info(f"Botões See grades encontrados: {sg_count}")
                if sg_count > 0:
                    await see_grades.first.click()
                    logger.info("Clicou See grades")
                    await page.wait_for_timeout(8000)  # Esperar iframe carregar

                # O conteúdo está dentro de um iframe cross-origin
                # iframe src: https://layers-notas-academicas.web.app/
                # Tentar via frame_locator primeiro
                notas_frame = page.frame_locator('iframe[src*="layers-notas-academicas"]')

                try:
                    iframe_text = await notas_frame.locator('body').inner_text(timeout=15000)
                    logger.info(f"Texto do iframe de notas: {len(iframe_text)} chars")
                    logger.info(f"Primeiros 500 chars: {iframe_text[:500]}")

                    # Parsear matérias e notas do texto
                    materias_notas = []
                    lines = iframe_text.split('\n')
                    materias_conhecidas = [
                        'LEM - Espanhol', 'Arte', 'Educação Física', 'Redação',
                        'Geografia', 'História', 'Língua Portuguesa', 'Matemática',
                        'Ciências', 'LEM - Inglês', 'MAT - Geometria', 'LP - Gramática',
                        'MAT - Álgebra', 'LP - Leitura'
                    ]
                    current_materia = None
                    for line in lines:
                        line = line.strip()
                        if not line or line in ['1º Bimestre', '2º Bimestre', '3º Bimestre', '4º Bimestre', 'Atual', '8 E', 'Melissa Majado Marinho']:
                            continue
                        if line in materias_conhecidas:
                            current_materia = line
                            materias_notas.append({"materia": current_materia, "nota": "-"})
                        elif current_materia and (line.replace('.', '').replace(',', '').isdigit() or line == '-'):
                            if materias_notas:
                                materias_notas[-1]["nota"] = line

                    if materias_notas:
                        dados["notas"] = materias_notas
                    else:
                        # Se não conseguiu parsear, salvar texto raw
                        dados["notas"] = [{"texto_raw": iframe_text[:10000]}]
                    logger.info(f"Notas coletadas: {len(materias_notas)} matérias")

                except Exception as e:
                    logger.warning(f"Erro ao acessar iframe de notas via frame_locator: {e}")
                    # Fallback: tentar via page.frame()
                    try:
                        frame = page.frame(url=lambda u: "layers-notas-academicas" in u)
                        if frame:
                            iframe_text = await frame.evaluate("document.body.innerText")
                            logger.info(f"Texto via page.frame(): {len(iframe_text)} chars")
                            dados["notas"] = [{"texto_raw": iframe_text[:10000]}]
                        else:
                            logger.warning("Frame de notas não encontrado")
                            page_text = await page.evaluate("document.body.innerText")
                            dados["notas"] = [{"texto_raw": page_text[:10000]}]
                    except Exception as e2:
                        logger.warning(f"Fallback page.frame() também falhou: {e2}")
                        page_text = await page.evaluate("document.body.innerText")
                        dados["notas"] = [{"texto_raw": page_text[:10000]}]

                # Verificar Attachment (boletim) - botão na página principal
                try:
                    attachment_btn = page.locator('button:has-text("Attachment")')
                    if await attachment_btn.count() > 0:
                        dados["notas_attachment"] = True
                        logger.info("Botão de Attachment (boletim) encontrado")
                except:
                    pass

            except Exception as e:
                logger.error(f"Erro em Notas Acadêmicas: {e}")
                dados["erros"].append(f"Notas: {str(e)}")

            # ========================================
            # 3. REGISTROS ACADÊMICOS (SEM IFRAME - conteúdo direto na página)
            # ========================================
            logger.info("Acessando Registros Acadêmicos...")
            try:
                # Navegar direto para a página com todos os registros
                await page.goto(
                    "https://liceu-jardim.layers.education/@liceu-jardim/portal/@admin:layers-registros-academicos/group/19656",
                    wait_until="domcontentloaded", timeout=30000
                )
                await page.wait_for_timeout(5000)

                # Registros NÃO estão em iframe - conteúdo direto na página
                # Cada registro é um <a> com texto no formato:
                # "New/Read X days ago • Matéria Descrição"
                registros = []
                registro_links = page.locator('a:has-text("ago"), a:has-text("month")')
                count = await registro_links.count()
                logger.info(f"Links de registros encontrados: {count}")

                for i in range(count):
                    try:
                        texto = await registro_links.nth(i).inner_text()
                        texto = texto.strip()
                        if not texto:
                            continue
                        registro = {"raw": texto}

                        # Parsear: "New 2 days ago • Arte Sem material"
                        # ou "Read 9 days ago • LEM - Inglês Sem Lição de Casa"
                        if texto.startswith("New"):
                            registro["status"] = "New"
                            texto_rest = texto[3:].strip()
                        elif texto.startswith("Read"):
                            registro["status"] = "Read"
                            texto_rest = texto[4:].strip()
                        else:
                            texto_rest = texto

                        # Separar por bullet •
                        if '•' in texto_rest:
                            parts = texto_rest.split('•', 1)
                            registro["tempo"] = parts[0].strip()
                            materia_desc = parts[1].strip()

                            # Lista de matérias conhecidas para separar matéria da descrição
                            materias = [
                                'LEM - Espanhol', 'Arte', 'Educação Física', 'Redação',
                                'Geografia', 'História', 'Língua Portuguesa', 'Matemática',
                                'Ciências', 'LEM - Inglês', 'MAT - Geometria', 'LP - Gramática',
                                'MAT - Álgebra', 'LP - Leitura'
                            ]
                            found_materia = False
                            for mat in materias:
                                if materia_desc.startswith(mat):
                                    registro["materia"] = mat
                                    registro["descricao"] = materia_desc[len(mat):].strip()
                                    found_materia = True
                                    break
                            if not found_materia:
                                registro["descricao"] = materia_desc

                        registros.append(registro)
                    except Exception as e:
                        logger.warning(f"Erro ao parsear registro {i}: {e}")

                if not registros:
                    # Fallback: extrair texto completo da página
                    page_text = await page.evaluate("document.body.innerText")
                    registros = [{"texto_raw": page_text[:10000]}]

                dados["registros"] = registros
                logger.info(f"Registros coletados: {len(registros)}")

            except Exception as e:
                logger.error(f"Erro em Registros Acadêmicos: {e}")
                dados["erros"].append(f"Registros: {str(e)}")

            # ========================================
            # 4. CONTEÚDO DE AULA (SOPHIA)
            # ========================================
            logger.info("Acessando Conteúdo de Aula...")
            try:
                await page.goto(
                    "https://liceu-jardim.layers.education/@liceu-jardim/portal/@sophiabylayers:conteudo-de-aula",
                    wait_until="domcontentloaded", timeout=30000
                )
                await page.wait_for_timeout(5000)

                # O conteúdo está dentro de iframe do Sophia
                sophia_frame = page.frame_locator('iframe[src*="sophia"]')
                try:
                    # Clicar em "Ver conteúdo"
                    ver_conteudo = sophia_frame.locator('button:has-text("Ver conteúdo"), a:has-text("Ver conteúdo")')
                    if await ver_conteudo.count() > 0:
                        await ver_conteudo.first.click()
                        logger.info("Clicou Ver conteúdo")
                        await page.wait_for_timeout(5000)

                    # Pular tutorial se aparecer
                    try:
                        pular_btn = sophia_frame.locator('button:has-text("Pular"), a:has-text("Pular")')
                        if await pular_btn.count() > 0:
                            await pular_btn.first.click()
                            await page.wait_for_timeout(2000)
                    except:
                        pass

                    # Tentar navegar para visualização por disciplina
                    try:
                        disciplina_btn = sophia_frame.locator('button:has-text("Disciplina"), a:has-text("Disciplina")')
                        if await disciplina_btn.count() > 0:
                            await disciplina_btn.first.click()
                            logger.info("Selecionou visualização por Disciplina")
                            await page.wait_for_timeout(3000)
                    except:
                        pass

                    # Coletar texto do iframe
                    conteudo_text = await sophia_frame.locator('body').inner_text(timeout=10000)
                    logger.info(f"Conteúdo de aula: {len(conteudo_text)} chars")
                    dados["conteudos"] = [{"texto_raw": conteudo_text[:15000]}]

                except Exception as e:
                    logger.warning(f"Erro no iframe Sophia: {e}")
                    page_text = await page.evaluate("document.body.innerText")
                    dados["conteudos"] = [{"texto_raw": page_text[:10000]}]

            except Exception as e:
                logger.error(f"Erro em Conteúdo de Aula: {e}")
                dados["erros"].append(f"Conteúdo de Aula: {str(e)}")

        except Exception as e:
            logger.error(f"Erro geral SuperApp: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro geral: {str(e)}")

        finally:
            await browser.close()

    dados["resumo"] = {
        "total_notas": len(dados["notas"]),
        "total_registros": len(dados["registros"]),
        "total_conteudos": len(dados["conteudos"]),
        "total_erros": len(dados["erros"])
    }

    return dados


# ============================================================
# SCRAPING - ROTEIRO DE ESTUDOS
# ============================================================
async def scrape_roteiro_async(req: ScrapeRequest) -> dict:
    """
    Navega no Roteiro de Estudos (Glide app) via Playwright e coleta:
    - Provas AO (Avaliação Objetiva)
    - Provas AD (Avaliação Dissertativa)
    - Provas de Inglês
    Navega DIRETO para o Roteiro e faz login pelo botão "Continuar com Google"
    que aparece na própria página do Glide App.
    """
    from playwright.async_api import async_playwright

    dados = {
        "provas_ao": [],
        "provas_ad": [],
        "provas_ingles": [],
        "erros": []
    }

    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD

    async with async_playwright() as p:
        # headless=False + Xvfb (mesmo padrão do Classroom que funciona)
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

        # Bloquear imagens, fontes, CSS e media para economizar memória
        await page.route("**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,eot,mp4,mp3,avi}", lambda route: route.abort())
        await page.route("**/fonts.googleapis.com/**", lambda route: route.abort())
        await page.route("**/fonts.gstatic.com/**", lambda route: route.abort())
        await page.route("**/www.google-analytics.com/**", lambda route: route.abort())
        await page.route("**/www.googletagmanager.com/**", lambda route: route.abort())

        try:
            # 1. Navegar DIRETO para o Roteiro de Estudos
            logger.info("Navegando direto para o Roteiro de Estudos...")
            await page.goto("https://roteiro.jardim.li/dl/d0a5f4", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            # 2. Clicar em "Continue with Google" na página do Roteiro
            logger.info("Procurando botão 'Continue with Google'...")
            google_btn = page.locator('#sign-in-with-google-button')
            btn_count = await google_btn.count()
            logger.info(f"Botões Google encontrados: {btn_count}")

            if btn_count > 0:
                await google_btn.first.click()
                logger.info("Clicou em 'Continuar com o Google'")
                await page.wait_for_timeout(5000)

                # 3. Preencher email no formulário do Google
                current_url = page.url
                logger.info(f"URL após clicar Google: {current_url}")

                # Verificar se abriu popup ou redirecionou
                pages = context.pages
                login_page = pages[-1] if len(pages) > 1 else page
                logger.info(f"Páginas abertas: {len(pages)}")

                # Inserir email
                email_input = login_page.locator('input[type="email"]')
                await email_input.wait_for(state="visible", timeout=10000)
                await email_input.fill(email)
                await login_page.wait_for_timeout(500)
                await login_page.locator('#identifierNext button').click()
                logger.info("Email inserido, aguardando tela de senha...")
                await login_page.wait_for_timeout(4000)

                # Inserir senha
                password_input = login_page.locator('input[type="password"]')
                await password_input.wait_for(state="visible", timeout=15000)
                await password_input.fill(password)
                await login_page.wait_for_timeout(500)
                await login_page.locator('#passwordNext button').click()
                logger.info("Senha inserida, aguardando login completar...")
                await login_page.wait_for_timeout(5000)

                # Verificar se voltou para o Roteiro
                current_url = page.url
                logger.info(f"URL após login: {current_url}")
            else:
                # Talvez já esteja logado, verificar conteúdo
                page_text = await page.evaluate("document.body.innerText")
                logger.info(f"Sem botão Google. Texto da página: {page_text[:200]}")

            # 4. Aguardar Glide App carregar após login
            logger.info("Aguardando Glide App renderizar após login...")
            await page.wait_for_timeout(10000)

            # Verificar se carregou
            page_text = await page.evaluate("document.body.innerText")
            logger.info(f"Roteiro: {len(page_text)} chars na página")

            # 5. Coletar dados de cada aba: AD, AO, Inglês
            abas_config = [
                {"nome": "AD", "chave": "provas_ad"},
                {"nome": "AO", "chave": "provas_ao"},
                {"nome": "Inglês", "chave": "provas_ingles"},
            ]

            for aba_cfg in abas_config:
                aba_nome = aba_cfg["nome"]
                aba_chave = aba_cfg["chave"]
                try:
                    # Clicar na aba
                    aba_btn = page.locator(f'button:has-text("{aba_nome}")')
                    aba_count = await aba_btn.count()
                    logger.info(f"Procurando aba {aba_nome}: {aba_count} botões encontrados")
                    if aba_count > 0:
                        await aba_btn.first.click()
                        logger.info(f"Clicou na aba {aba_nome}")
                        await page.wait_for_timeout(3000)

                        # Coletar todos os itens da lista via JavaScript
                        items_data = await page.evaluate("""
                            () => {
                                const items = document.querySelectorAll('div[role="button"]');
                                return Array.from(items).map(item => item.innerText.trim());
                            }
                        """)
                        logger.info(f"Aba {aba_nome}: {len(items_data)} itens encontrados via JS")

                        provas_aba = []
                        for i, item_text in enumerate(items_data):
                            logger.info(f"  Item {i}: {item_text[:80]}")
                            prova = {
                                "tipo": aba_nome,
                                "item_lista": item_text,
                                "indice": i
                            }
                            partes = item_text.split("\n")
                            if len(partes) >= 2:
                                prova["materia"] = partes[1].strip() if len(partes) > 1 else ""
                                prova["data"] = partes[2].strip() if len(partes) > 2 else ""
                                prova["serie"] = partes[0].strip() if len(partes) > 0 else ""
                            provas_aba.append(prova)

                        dados[aba_chave] = provas_aba
                        logger.info(f"Aba {aba_nome}: {len(provas_aba)} provas coletadas")

                    else:
                        logger.warning(f"Aba {aba_nome} não encontrada")
                        dados["erros"].append(f"Aba {aba_nome} não encontrada")

                except Exception as e:
                    logger.error(f"Erro na aba {aba_nome}: {e}")
                    dados["erros"].append(f"Aba {aba_nome}: {str(e)}")

            # Fallback: se nenhuma prova coletada, extrair texto completo
            if not dados["provas_ao"] and not dados["provas_ad"] and not dados["provas_ingles"]:
                page_text = await page.evaluate("document.body.innerText")
                dados["texto_completo"] = page_text[:15000]
                logger.warning(f"Nenhuma prova coletada. Texto da página ({len(page_text)} chars) salvo como fallback")

        except Exception as e:
            logger.error(f"Erro geral Roteiro: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro geral: {str(e)}")

        finally:
            await browser.close()

    dados["resumo"] = {
        "total_ao": len(dados["provas_ao"]),
        "total_ad": len(dados["provas_ad"]),
        "total_ingles": len(dados["provas_ingles"]),
        "total_erros": len(dados["erros"])
    }

    return dados


# ============================================================
# ENDPOINTS
# ============================================================
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "melissa-scraper-playwright",
        "version": "3.0.0",
        "timestamp": datetime.now().isoformat(),
        "playwright": True
    }


@app.get("/")
def root():
    return {
        "service": "Melissa Scraper API v3 (Playwright)",
        "version": "3.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": ["/scrape/classroom", "/scrape/superapp", "/scrape/roteiro", "/scrape/all"]
    }


@app.post("/scrape/classroom", response_model=ScrapeResponse)
async def endpoint_classroom(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping Classroom para {req.aluna}")
    dados = await scrape_classroom_async(req)
    return ScrapeResponse(
        status="success" if not dados.get("erros") else "partial",
        fonte="classroom",
        data_coleta=datetime.now().isoformat(),
        dados=dados,
        erros=dados.get("erros", [])
    )


@app.post("/scrape/superapp", response_model=ScrapeResponse)
async def endpoint_superapp(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping SuperApp para {req.aluna}")
    dados = await scrape_superapp_async(req)
    return ScrapeResponse(
        status="success" if not dados.get("erros") else "partial",
        fonte="superapp",
        data_coleta=datetime.now().isoformat(),
        dados=dados,
        erros=dados.get("erros", [])
    )


@app.post("/scrape/roteiro", response_model=ScrapeResponse)
async def endpoint_roteiro(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info("Scraping Roteiro de Estudos")
    dados = await scrape_roteiro_async(req)
    return ScrapeResponse(
        status="success" if not dados.get("erros") else "partial",
        fonte="roteiro",
        data_coleta=datetime.now().isoformat(),
        dados=dados,
        erros=dados.get("erros", [])
    )


@app.post("/scrape/all")
async def endpoint_all(req: ScrapeRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping completo para {req.aluna}")

    # Executar em paralelo
    classroom_task = scrape_classroom_async(req)
    superapp_task = scrape_superapp_async(req)
    roteiro_task = scrape_roteiro_async(req)

    classroom, superapp, roteiro = await asyncio.gather(
        classroom_task, superapp_task, roteiro_task,
        return_exceptions=True
    )

    return {
        "status": "success",
        "data_coleta": datetime.now().isoformat(),
        "classroom": classroom if not isinstance(classroom, Exception) else {"erros": [str(classroom)]},
        "superapp": superapp if not isinstance(superapp, Exception) else {"erros": [str(superapp)]},
        "roteiro": roteiro if not isinstance(roteiro, Exception) else {"erros": [str(roteiro)]}
    }


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
