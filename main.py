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
            locale="en-US"
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
            if "Melissa" in page_text or "My Apps" in page_text or "Home" in page_text or "Meus apps" in page_text or "Início" in page_text:
                logger.info("Login SuperApp OK!")
            else:
                logger.warning(f"Login pode ter falhado. Texto: {page_text[:300]}")
                dados["erros"].append("Login SuperApp pode ter falhado")

            # ========================================
            # 2. NOTAS ACADÊMICAS (GRADEBOOKS) - via clique no menu + iframe
            # ========================================
            logger.info("Acessando Notas Acadêmicas...")
            try:
                # Clicar em "Gradebooks" no menu lateral
                gradebooks_link = page.locator('a:has-text("Gradebooks"), a:has-text("Notas Acadêmicas")')
                if await gradebooks_link.count() > 0:
                    await gradebooks_link.first.click()
                    logger.info("Clicou Gradebooks no menu")
                    await page.wait_for_timeout(8000)
                else:
                    logger.warning("Link Gradebooks não encontrado no menu")

                # Clicar em "Ver notas" dentro do iframe (Playwright click)
                notas_fl = page.frame_locator('iframe[src*="layers-notas-academicas"]')
                try:
                    ver_notas_btn = notas_fl.locator('button:has-text("Ver notas"), button:has-text("See grades")')
                    if await ver_notas_btn.count() > 0:
                        await ver_notas_btn.first.click()
                        logger.info("Clicou Ver notas")
                        await page.wait_for_timeout(8000)
                except Exception as e:
                    logger.warning(f"Erro ao clicar Ver notas: {e}")

                # Encontrar o frame real para evaluate
                notas_frame = None
                for f in page.frames:
                    if "layers-notas-academicas.web.app" in f.url:
                        notas_frame = f
                        break

                if notas_frame:
                    materias_conhecidas = [
                        'LEM - Espanhol', 'Arte', 'Educação Física', 'Redação',
                        'Geografia', 'História', 'Língua Portuguesa', 'Matemática',
                        'Ciências', 'LEM - Inglês', 'MAT - Geometria', 'LP - Gramática',
                        'MAT - Álgebra', 'LP - Leitura'
                    ]

                    skip_lines = {'Melissa Majado Marinho', '(1) Anexo', '1º Bimestre', '2º Bimestre',
                                  '3º Bimestre', '4º Bimestre', '8 E', 'Atual', '', '/'}

                    def parse_materia_text(texto, materia_nome):
                        """Parseia o texto expandido de uma matéria individual"""
                        data = {"materia": materia_nome, "resultado_final": "-", "faltas": "0", "avaliacoes": []}
                        lines = texto.split('\n')
                        avaliacoes = []
                        found_materia = False
                        i = 0
                        while i < len(lines):
                            line = lines[i].strip()
                            i += 1
                            if line in skip_lines or (line.startswith('(') and line.endswith(')')):
                                continue
                            # Pular outras matérias (colapsadas)
                            if line in materias_conhecidas and line != materia_nome:
                                if found_materia:
                                    break  # Chegou na próxima matéria, parar
                                continue
                            if line == materia_nome:
                                found_materia = True
                                # Próxima linha é nota resumo
                                if i < len(lines):
                                    nr = lines[i].strip()
                                    if nr == '-' or nr.replace(',', '').replace('.', '').isdigit():
                                        i += 1
                                continue
                            if not found_materia:
                                continue
                            # Categorias de avaliação (headers)
                            if line in ['Avaliação Dissertativa', 'Avaliação Objetiva', 'Outros Instrumentos avaliativos']:
                                continue
                            if line == 'Resultado Final':
                                if i < len(lines):
                                    val = lines[i].strip()
                                    if val == '-' or val.replace(',', '').replace('.', '').isdigit():
                                        data["resultado_final"] = val
                                        i += 1
                                continue
                            if line == 'Faltas':
                                if i < len(lines):
                                    val = lines[i].strip()
                                    if val.isdigit():
                                        data["faltas"] = val
                                        i += 1
                                continue
                            # Avaliação: nome seguido de nota / max
                            if line and line not in skip_lines:
                                if not line.replace(',', '').replace('.', '').isdigit() and line != '-' and line != '/':
                                    aval = {"nome": line, "nota": "-", "max": ""}
                                    if i < len(lines):
                                        nv = lines[i].strip()
                                        if nv == '-' or nv.replace(',', '').replace('.', '').isdigit():
                                            aval["nota"] = nv
                                            i += 1
                                    if i < len(lines) and lines[i].strip() == '/':
                                        i += 1
                                    if i < len(lines):
                                        mv = lines[i].strip()
                                        if mv.replace(',', '').replace('.', '').isdigit():
                                            aval["max"] = mv
                                            i += 1
                                    if aval["max"]:
                                        avaliacoes.append(aval)
                        data["avaliacoes"] = avaliacoes
                        return data

                    # Expandir UMA matéria por vez, capturar texto, colapsar
                    notas_detalhadas = []
                    for materia in materias_conhecidas:
                        try:
                            mat_el = notas_fl.locator(f'text="{materia}"').first
                            if await mat_el.count() > 0:
                                # Expandir
                                await mat_el.click()
                                await page.wait_for_timeout(2000)
                                # Capturar texto com esta matéria expandida
                                texto = await notas_frame.evaluate("document.body.innerText")
                                parsed = parse_materia_text(texto, materia)
                                notas_detalhadas.append(parsed)
                                logger.info(f"Notas {materia}: {len(parsed['avaliacoes'])} avaliacoes")
                                # Colapsar (clicar de novo)
                                await mat_el.click()
                                await page.wait_for_timeout(500)
                        except Exception as e:
                            logger.warning(f"Erro ao coletar notas de {materia}: {e}")
                            notas_detalhadas.append({"materia": materia, "erro": str(e)})

                    dados["notas"] = notas_detalhadas
                    logger.info(f"Notas detalhadas: {len(notas_detalhadas)} matérias")
                else:
                    dados["notas"] = [{"erro": "Frame de notas não encontrado"}]
                    dados["erros"].append("Frame layers-notas-academicas.web.app não encontrado")

            except Exception as e:
                logger.error(f"Erro em Notas Acadêmicas: {e}")
                dados["erros"].append(f"Notas: {str(e)}")

            # ========================================
            # 3. REGISTROS ACADÊMICOS - via clique no menu + iframe
            # ========================================
            logger.info("Acessando Registros Acadêmicos...")
            try:
                # Clicar em "Academic Records" no menu lateral
                records_link = page.locator('a:has-text("Academic Records"), a:has-text("Registros Acadêmicos")')
                if await records_link.count() > 0:
                    await records_link.first.click()
                    logger.info("Clicou Academic Records no menu")
                    await page.wait_for_timeout(5000)

                # Os registros estão no iframe layers-registros-academicos
                reg_frame = page.frame_locator('iframe[src*="layers-registros-academicos"]')

                # Clicar em "See all" dentro do iframe
                try:
                    see_all_btn = reg_frame.locator('button:has-text("See all"), a:has-text("See all"), button:has-text("Ver tudo"), a:has-text("Ver tudo")')
                    sa_count = await see_all_btn.count()
                    logger.info(f"Botões See all no iframe: {sa_count}")
                    if sa_count > 0:
                        await see_all_btn.first.click()
                        logger.info("Clicou See all dentro do iframe")
                        await page.wait_for_timeout(5000)
                except Exception as e:
                    logger.warning(f"Erro ao clicar See all: {e}")

                # Ler conteúdo do iframe de registros
                try:
                    reg_text = await reg_frame.locator('body').inner_text(timeout=15000)
                    logger.info(f"Texto do iframe de registros: {len(reg_text)} chars")
                    logger.info(f"Primeiros 500 chars registros: {reg_text[:500]}")

                    # Parsear registros do texto
                    registros = []
                    lines = reg_text.split('\n')
                    materias_conhecidas = [
                        'LEM - Espanhol', 'Arte', 'Educação Física', 'Redação',
                        'Geografia', 'História', 'Língua Portuguesa', 'Matemática',
                        'Ciências', 'LEM - Inglês', 'MAT - Geometria', 'LP - Gramática',
                        'MAT - Álgebra', 'LP - Leitura'
                    ]
                    i = 0
                    while i < len(lines):
                        line = lines[i].strip()
                        # Detectar linhas com status (New/Read)
                        if line in ['New', 'Read', 'Novo', 'Nova', 'Lido', 'Lida']:
                            registro = {"status": "Novo" if line in ['New', 'Novo', 'Nova'] else "Lido"}
                            # Próxima linha deve ter o tempo
                            if i + 1 < len(lines):
                                tempo_line = lines[i + 1].strip()
                                if '•' in tempo_line:
                                    parts = tempo_line.split('•', 1)
                                    registro["tempo"] = parts[0].strip()
                                    registro["materia"] = parts[1].strip()
                                else:
                                    registro["tempo"] = tempo_line
                            # Próxima linha deve ter a descrição
                            if i + 2 < len(lines):
                                desc = lines[i + 2].strip()
                                if desc and desc not in ['New', 'Read', 'Novo', 'Nova', 'Lido', 'Lida', 'Mark all as read']:
                                    registro["descricao"] = desc
                            registros.append(registro)
                            i += 3
                        else:
                            i += 1

                    if registros:
                        dados["registros"] = registros
                    else:
                        dados["registros"] = [{"texto_raw": reg_text[:10000]}]
                    logger.info(f"Registros coletados: {len(registros)}")

                except Exception as e:
                    logger.warning(f"Erro ao ler iframe de registros: {e}")
                    try:
                        frame = page.frame(url=lambda u: "layers-registros-academicos" in u)
                        if frame:
                            reg_text = await frame.evaluate("document.body.innerText")
                            dados["registros"] = [{"texto_raw": reg_text[:10000]}]
                        else:
                            dados["registros"] = [{"erro": "Frame de registros não encontrado"}]
                    except Exception as e2:
                        dados["registros"] = [{"erro": str(e2)}]

            except Exception as e:
                logger.error(f"Erro em Registros Acadêmicos: {e}")
                dados["erros"].append(f"Registros: {str(e)}")

            # Conteúdo de Aula agora é endpoint separado: /scrape/superapp/conteudo
            dados["conteudos"] = [{"info": "Use endpoint /scrape/superapp/conteudo?materia=NomeDaMateria para coletar conteúdo de aula por matéria"}]

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
# SCRAPING - SUPERAPP CONTEÚDO DE AULA (POR MATÉRIA)
# ============================================================
class ConteudoRequest(BaseModel):
    email: str = ""
    password: str = ""
    materia: str = ""  # Nome da matéria (ex: "Arte", "Ciências"). Vazio = retorna lista de matérias


async def scrape_superapp_conteudo_async(req: ConteudoRequest) -> dict:
    """
    Coleta conteúdo de aula de UMA matéria específica do SuperApp (Sophia).
    Se materia estiver vazio, retorna a lista de matérias disponíveis.
    Fluxo: Login Layers -> Conteúdo de Aula -> Pular tutorial -> Ver conteúdo -> Por disciplina -> Materia
    """
    from playwright.async_api import async_playwright

    dados = {
        "materia_solicitada": req.materia,
        "conteudo": "",
        "materias_disponiveis": [],
        "erros": []
    }

    email = req.email or SUPERAPP_EMAIL
    password = req.password or SUPERAPP_PASSWORD

    async with async_playwright() as p:
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
            locale="en-US"
        )

        page = await context.new_page()

        try:
            # 1. LOGIN NO LAYERS
            logger.info("[Conteudo] Acessando SuperApp Layers...")
            await page.goto("https://liceu-jardim.layers.education/@liceu-jardim/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            current_url = page.url
            if "id.layers.digital" in current_url or "login" in current_url.lower():
                logger.info("[Conteudo] Fazendo login...")
                # Email
                email_input = page.locator('input[type="email"]')
                await email_input.wait_for(state="visible", timeout=10000)
                await email_input.fill(email)
                await page.wait_for_timeout(500)
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Continuar")')
                if await continue_btn.count() > 0:
                    await continue_btn.first.click()
                    await page.wait_for_timeout(3000)
                # Senha
                password_input = page.locator('input[type="password"]')
                await password_input.wait_for(state="visible", timeout=10000)
                await password_input.fill(password)
                await page.wait_for_timeout(500)
                enter_btn = page.locator('button:has-text("Enter"), button:has-text("Entrar")')
                if await enter_btn.count() > 0:
                    await enter_btn.first.click()
                    await page.wait_for_timeout(8000)

            # 2. NAVEGAR PARA CONTEÚDO DE AULA
            logger.info("[Conteudo] Clicando Conteúdo de aula no menu...")
            conteudo_link = page.locator('a:has-text("Conteúdo de aula"), a:has-text("Class content")')
            if await conteudo_link.count() > 0:
                await conteudo_link.first.click()
                await page.wait_for_timeout(10000)
            else:
                await page.goto(
                    "https://liceu-jardim.layers.education/@liceu-jardim/portal/@sophiabylayers:conteudo-de-aula",
                    wait_until="domcontentloaded", timeout=30000
                )
                await page.wait_for_timeout(10000)

            # 3. ENCONTRAR FRAME SOPHIA
            sophia = None
            for f in page.frames:
                if "appconteudoaula" in f.url:
                    sophia = f
                    break

            if not sophia:
                dados["erros"].append("Frame Sophia não encontrado")
                await browser.close()
                return dados

            # 4. PULAR TUTORIAL
            try:
                await sophia.evaluate("""
                    () => {
                        const els = document.querySelectorAll('*');
                        for (const el of els) {
                            if (el.textContent?.trim() === 'Pular' || el.textContent?.trim() === 'Skip') {
                                el.click(); return;
                            }
                        }
                    }
                """)
                await page.wait_for_timeout(3000)
            except:
                pass

            # 5. CLICAR "VER CONTEÚDO"
            fl = page.frame_locator('iframe[src*="appconteudoaula"]')
            try:
                ver = fl.locator('h6:has-text("Ver conteúdo"), h6:has-text("See content")')
                if await ver.count() > 0:
                    await ver.first.click()
                    logger.info("[Conteudo] Clicou Ver conteúdo")
                    await page.wait_for_timeout(5000)
            except Exception as e:
                logger.warning(f"[Conteudo] Erro Ver conteúdo: {e}")

            # 6. CLICAR "POR DISCIPLINA" (segundo card)
            try:
                cards = fl.locator('div.s-card.s-button-menu')
                card_count = await cards.count()
                if card_count >= 2:
                    await cards.nth(1).click()
                    logger.info("[Conteudo] Clicou Por disciplina")
                    await page.wait_for_timeout(5000)
                elif card_count == 1:
                    await cards.first.click()
                    await page.wait_for_timeout(5000)
            except Exception as e:
                logger.warning(f"[Conteudo] Erro Por disciplina: {e}")

            # 7. COLETAR LISTA DE MATÉRIAS
            materias_els = await sophia.evaluate("""
                () => {
                    const els = document.querySelectorAll('div.s-card.s-card-container');
                    return Array.from(els).map(el => el.textContent?.trim()).filter(t => t && t.length > 2);
                }
            """)
            dados["materias_disponiveis"] = materias_els
            logger.info(f"[Conteudo] Matérias: {materias_els}")

            # Se nenhuma matéria solicitada, retorna só a lista
            if not req.materia:
                logger.info("[Conteudo] Nenhuma matéria solicitada, retornando lista")
                await browser.close()
                return dados

            # 8. CLICAR NA MATÉRIA SOLICITADA
            materia_encontrada = False
            for m in materias_els:
                if req.materia.lower() in m.lower() or m.lower() in req.materia.lower():
                    materia_encontrada = True
                    logger.info(f"[Conteudo] Clicando em: {m}")
                    await sophia.evaluate(f"""
                        () => {{
                            const els = document.querySelectorAll('div.s-card.s-card-container');
                            for (const el of els) {{
                                if (el.textContent?.trim() === '{m}') {{
                                    el.click();
                                    return;
                                }}
                            }}
                        }}
                    """)
                    await page.wait_for_timeout(5000)

                    # Coletar texto da matéria
                    materia_text = await sophia.evaluate("document.body.innerText")
                    logger.info(f"[Conteudo] {m}: {len(materia_text)} chars")
                    dados["materia_solicitada"] = m
                    dados["conteudo"] = materia_text[:10000]
                    break

            if not materia_encontrada:
                dados["erros"].append(f"Matéria '{req.materia}' não encontrada. Disponíveis: {materias_els}")

        except Exception as e:
            logger.error(f"[Conteudo] Erro: {e}\n{traceback.format_exc()}")
            dados["erros"].append(f"Erro: {str(e)}")

        finally:
            await browser.close()

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
    Estratégia: Navega direto para o Roteiro, clica "Continuar com Google",
    preenche email/senha no popup do Google, e coleta os dados.
    """
    from playwright.async_api import async_playwright
    import asyncio as _asyncio

    dados = {
        "provas_ao": [],
        "provas_ad": [],
        "provas_ingles": [],
        "erros": []
    }

    email = req.email or MELISSA_EMAIL
    password = req.password or MELISSA_PASSWORD

    async with async_playwright() as p:
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

        # Bloquear imagens, fontes e analytics para economizar memória
        await page.route("**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,eot,mp4,mp3,avi}", lambda route: route.abort())
        await page.route("**/fonts.googleapis.com/**", lambda route: route.abort())
        await page.route("**/fonts.gstatic.com/**", lambda route: route.abort())
        await page.route("**/www.google-analytics.com/**", lambda route: route.abort())
        await page.route("**/www.googletagmanager.com/**", lambda route: route.abort())

        try:
            # 1. NAVEGAR DIRETO PARA O ROTEIRO
            logger.info("[Roteiro] Navegando para o Roteiro de Estudos...")
            await page.goto("https://roteiro.jardim.li/dl/d0a5f4", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            page_text = await page.evaluate("document.body.innerText")
            logger.info(f"[Roteiro] Texto inicial: {page_text[:200]}")

            # 2. CLICAR EM "CONTINUAR COM GOOGLE" E FAZER LOGIN NO POPUP
            google_btn = page.locator('#sign-in-with-google-button')
            if await google_btn.count() > 0:
                logger.info("[Roteiro] Clicando 'Continuar com Google' e aguardando popup...")

                # Capturar o popup que vai abrir
                async with context.expect_page() as popup_info:
                    await google_btn.first.click()

                popup = await popup_info.value
                await popup.wait_for_load_state("domcontentloaded")
                logger.info(f"[Roteiro] Popup aberto: {popup.url[:100]}")
                await popup.wait_for_timeout(3000)

                # 3. PREENCHER EMAIL NO POPUP
                logger.info("[Roteiro] Preenchendo email no popup...")
                email_input = popup.locator('input[type="email"]')
                await email_input.wait_for(state="visible", timeout=10000)
                await email_input.fill(email)
                await popup.wait_for_timeout(500)

                # Clicar Avançar
                next_btn = popup.locator('#identifierNext button')
                if await next_btn.count() > 0:
                    await next_btn.first.click()
                else:
                    next_btn2 = popup.locator('button:has-text("Avançar"), button:has-text("Next")')
                    await next_btn2.first.click()
                logger.info("[Roteiro] Email enviado, aguardando senha...")
                await popup.wait_for_timeout(5000)

                # 4. PREENCHER SENHA NO POPUP
                logger.info("[Roteiro] Preenchendo senha...")
                password_input = popup.locator('input[type="password"]')
                await password_input.first.wait_for(state="visible", timeout=15000)
                await password_input.first.fill(password)
                await popup.wait_for_timeout(500)

                pwd_next = popup.locator('#passwordNext button')
                if await pwd_next.count() > 0:
                    await pwd_next.first.click()
                else:
                    pwd_next2 = popup.locator('button:has-text("Avançar"), button:has-text("Next")')
                    await pwd_next2.first.click()
                logger.info("[Roteiro] Senha enviada, aguardando popup fechar...")

                # Aguardar popup fechar (redireciona e fecha automaticamente)
                for i in range(15):
                    await _asyncio.sleep(1)
                    if popup.is_closed():
                        logger.info(f"[Roteiro] Popup fechou após {i+1}s")
                        break

                # 5. AGUARDAR ROTEIRO CARREGAR APÓS LOGIN
                logger.info("[Roteiro] Aguardando Roteiro carregar após login...")
                await page.wait_for_timeout(10000)
            else:
                # Já está logado (sem botão Google)
                logger.info("[Roteiro] Sem botão Google - já está logado")
                await page.wait_for_timeout(5000)

            # 6. VERIFICAR SE CARREGOU
            page_text = await page.evaluate("document.body.innerText")
            logger.info(f"[Roteiro] Texto após login ({len(page_text)} chars): {page_text[:300]}")

            # 7. COLETAR DADOS DE CADA ABA: AD, AO, Inglês
            abas_config = [
                {"nome": "AD", "chave": "provas_ad"},
                {"nome": "AO", "chave": "provas_ao"},
                {"nome": "Inglês", "chave": "provas_ingles"},
            ]

            for aba_cfg in abas_config:
                aba_nome = aba_cfg["nome"]
                aba_chave = aba_cfg["chave"]
                try:
                    aba_btn = page.locator(f'button:has-text("{aba_nome}")')
                    aba_count = await aba_btn.count()
                    logger.info(f"[Roteiro] Procurando aba {aba_nome}: {aba_count} botões")
                    if aba_count > 0:
                        await aba_btn.first.click()
                        await page.wait_for_timeout(3000)

                        items_data = await page.evaluate("""
                            () => {
                                const items = document.querySelectorAll('div[role="button"]');
                                return Array.from(items).map(item => item.innerText.trim());
                            }
                        """)
                        logger.info(f"[Roteiro] Aba {aba_nome}: {len(items_data)} itens")

                        provas_aba = []
                        for i, item_text in enumerate(items_data):
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
                        logger.info(f"[Roteiro] Aba {aba_nome}: {len(provas_aba)} provas coletadas")
                    else:
                        logger.warning(f"[Roteiro] Aba {aba_nome} não encontrada")
                        dados["erros"].append(f"Aba {aba_nome} não encontrada")

                except Exception as e:
                    logger.error(f"[Roteiro] Erro na aba {aba_nome}: {e}")
                    dados["erros"].append(f"Aba {aba_nome}: {str(e)}")

            # Fallback
            if not dados["provas_ao"] and not dados["provas_ad"] and not dados["provas_ingles"]:
                page_text = await page.evaluate("document.body.innerText")
                dados["texto_completo"] = page_text[:15000]
                logger.warning(f"[Roteiro] Nenhuma prova coletada. Texto ({len(page_text)} chars) salvo como fallback")

        except Exception as e:
            logger.error(f"[Roteiro] Erro geral: {e}\n{traceback.format_exc()}")
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
        "endpoints": ["/scrape/classroom", "/scrape/superapp", "/scrape/superapp/conteudo", "/scrape/roteiro", "/scrape/all"]
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


@app.post("/scrape/superapp/conteudo")
async def endpoint_superapp_conteudo(req: ConteudoRequest, authorization: str = Header(None)):
    verificar_auth(authorization)
    logger.info(f"Scraping Conteúdo de Aula - Matéria: {req.materia or 'LISTA'}")
    dados = await scrape_superapp_conteudo_async(req)
    return {
        "status": "success" if not dados.get("erros") else "partial",
        "fonte": "superapp-conteudo",
        "data_coleta": datetime.now().isoformat(),
        "dados": dados,
        "erros": dados.get("erros", [])
    }


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
