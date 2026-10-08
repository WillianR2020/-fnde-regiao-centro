"""
Coleta de precos FNDE - Categoria UNIFORMES
============================================

Por que este script substitui o coleta_precos_fnde_sul.py original
--------------------------------------------------------------------
O script original navegava sozinho com Selenium + Chrome. O ambiente onde esta
coleta foi feita nao tem Chrome nem chromedriver instalados, entao o Selenium
nunca chegou a rodar aqui. Os 22 precos desta rodada (categoria UNIFORMES) foram
encontrados por pesquisa manual (busca + navegacao pagina a pagina), seguindo as
mesmas regras de validacao do projeto (SKILL.md): CNAE atacadista Divisao 46,
sem marketplace, sem fornecedor so-varejo, preco nunca inventado.

Este script cobre as partes do fluxo que SAO automatizaveis e reproduziveis:

  1) VALIDACAO DE CNAE: para cada CNPJ usado, consulta a BrasilAPI ao vivo e
     confirma que o fornecedor tem CNAE de comercio atacadista (Divisao 46),
     principal ou secundario -- a mesma regra usada na pesquisa manual.
  2) CAPTURA DE PRINT: abre a URL de cada produto com Playwright (Chromium
     baixado pelo proprio Playwright, sem depender de instalacao previa de
     Chrome no sistema) e salva um print em
     output/prints/<ID_FGV>/print_NN_<fornecedor>.png
  3) GERACAO DA PLANILHA: escreve/recria output/FNDE_output.xlsx a partir dos
     registros abaixo (REGISTROS_UNIFORMES), no layout ja usado pelo projeto.

O que este script NAO faz (por decisao, nao por limitacao tecnica)
--------------------------------------------------------------------
- Nao descobre fornecedores novos sozinho. A descoberta de fornecedores e
  precos continua sendo feita por pesquisa (manual ou por um agente),
  registrada aqui em REGISTROS_UNIFORMES, e so entao processada pelo script.
- Nao preenche frete. VALOR FRETE / OBS FRETE ficam vazios nesta etapa,
  por decisao do usuario ("agora nao vamos nos preocupar com isso").

Como adicionar um preco novo
------------------------------
1. Adicione um dict a lista REGISTROS_UNIFORMES, com todos os campos
   preenchidos (nunca invente CNPJ, preco ou CNAE).
2. Rode `python coleta_precos_fnde_uniformes.py`.
3. O script valida o CNAE ao vivo, tira o print da pagina e regrava a
   planilha inteira a partir da lista (a lista e a fonte da verdade).

Como rodar
-----------
1. pip install -r requirements.txt
2. python -m playwright install chromium   (uma unica vez, baixa o Chromium)
3. python coleta_precos_fnde_uniformes.py
"""

from __future__ import annotations

import re
import json
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

try:
    # Em maquinas com um certificado raiz corporativo (proxy/antivirus) que nao
    # esta no bundle do certifi, o requests falha com SSLCertVerificationError
    # mesmo que o sistema operacional confie nesse certificado. Este pacote
    # redireciona a verificacao de certificados do requests para o repositorio
    # de certificados do proprio sistema operacional. Se nao estiver instalado,
    # seguimos com o comportamento padrao do requests/certifi.
    import pip_system_certs.wrapt_requests  # noqa: F401
except ImportError:
    pass

import requests
from openpyxl import Workbook
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "coleta_fnde.log"
OUTPUT_DIR = BASE_DIR / "output"
PRINTS_DIR = OUTPUT_DIR / "prints"
PLANILHA_SAIDA = OUTPUT_DIR / "FNDE_output.xlsx"
CNAE_CACHE_FILE = LOG_DIR / "cnae_cache.json"


# ---------------------------------------------------------------------------
# 1) LOGGING (mesmo padrao documentado em LOGS.md: terminal + arquivo rotativo)
# ---------------------------------------------------------------------------

def configurar_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("coleta_fnde")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formato_terminal = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
    )
    formato_arquivo = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler_terminal = logging.StreamHandler()
    handler_terminal.setFormatter(formato_terminal)

    handler_arquivo = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
    )
    handler_arquivo.setFormatter(formato_arquivo)

    logger.addHandler(handler_terminal)
    logger.addHandler(handler_arquivo)
    return logger


log = configurar_logging()


# ---------------------------------------------------------------------------
# 2) VALIDACAO DE CNAE ATACADISTA (Divisao 46) -- mesma regra do SKILL.md
# ---------------------------------------------------------------------------

CNAE_DIVISAO_ATACADISTA = "46"

MARKETPLACE_DOMAINS = [
    "mercadolivre.com", "mercadolibre.com", "amazon.com", "shopee.com",
    "americanas.com", "magazineluiza.com.br", "magalu.com", "aliexpress.com",
    "olx.com.br", "submarino.com.br", "shoptime.com.br", "casasbahia.com.br",
    "extra.com.br", "carrefour.com.br", "netshoes.com.br", "kabum.com.br",
]


def eh_marketplace(url: str) -> bool:
    from urllib.parse import urlparse
    dominio = urlparse(url).netloc.lower()
    return any(mp in dominio for mp in MARKETPLACE_DOMAINS)


def cnae_eh_atacadista(codigo_cnae: str) -> bool:
    digitos = re.sub(r"\D", "", str(codigo_cnae))
    return digitos[:2] == CNAE_DIVISAO_ATACADISTA


def _carregar_cache_cnae() -> dict:
    if not CNAE_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CNAE_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Cache de CNAE corrompido, ignorando ({CNAE_CACHE_FILE}): {e}")
        return {}


def _salvar_cache_cnae(cache: dict) -> None:
    CNAE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CNAE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def consultar_cnae_por_cnpj(cnpj: str, tentativas: int = 4, usar_cache: bool = True) -> tuple[list[str], str, str]:
    """Consulta CNAEs e UF de um CNPJ na BrasilAPI. Nunca inventa: se a API
    nao responder (mesmo apos retentativas), devolve listas vazias.

    A BrasilAPI aplica rate limit (HTTP 429) em rajadas de requisicoes; por
    isso as tentativas usam backoff exponencial (2s, 4s, 8s...), e o
    resultado de uma consulta bem-sucedida fica em cache local
    (logs/cnae_cache.json) para nao martelar a API a cada nova execucao do
    script com os mesmos CNPJs.
    """
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    if len(cnpj_limpo) != 14:
        return [], "", ""

    cache = _carregar_cache_cnae() if usar_cache else {}
    if usar_cache and cnpj_limpo in cache:
        entrada = cache[cnpj_limpo]
        log.info(f"  (cache de {entrada['consultado_em']}) usando resultado ja validado para {cnpj}")
        return entrada["cnaes"], entrada["uf"], f"BrasilAPI (cache de {entrada['consultado_em']})"

    espera = 2.0
    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(
                f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}", timeout=12
            )
            if resp.status_code == 200:
                dados = resp.json()
                cnaes = [str(dados.get("cnae_fiscal", ""))]
                cnaes += [str(c.get("codigo", "")) for c in dados.get("cnaes_secundarios", [])]
                cnaes = [c for c in cnaes if c]
                uf = str(dados.get("uf", "") or "")
                if usar_cache and cnaes:
                    cache[cnpj_limpo] = {
                        "cnaes": cnaes, "uf": uf,
                        "consultado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    _salvar_cache_cnae(cache)
                return cnaes, uf, "BrasilAPI"
            if resp.status_code == 429 and tentativa < tentativas:
                log.warning(f"  Rate limit (429) da BrasilAPI para {cnpj}; aguardando {espera:.0f}s (tentativa {tentativa}/{tentativas})")
                time.sleep(espera)
                espera *= 2
                continue
            log.warning(f"Falha ao consultar BrasilAPI para {cnpj}: HTTP {resp.status_code}")
            return [], "", ""
        except Exception as e:
            if tentativa < tentativas:
                log.warning(f"  Erro de conexao para {cnpj} (tentativa {tentativa}/{tentativas}): {e}; aguardando {espera:.0f}s")
                time.sleep(espera)
                espera *= 2
                continue
            log.warning(f"Falha ao consultar BrasilAPI para {cnpj}: {e}")
    return [], "", ""


def validar_fornecedor(nome: str, cnpj: str, url: str) -> bool:
    """Revalida ao vivo: nao e marketplace + tem CNAE atacadista (Divisao 46,
    principal ou secundario). Loga o resultado. Retorna True/False."""
    if eh_marketplace(url):
        log.warning(f"  REPROVADO (marketplace): {nome} -> {url}")
        return False

    cnaes, uf_cnpj, fonte = consultar_cnae_por_cnpj(cnpj)
    if not cnaes:
        log.warning(f"  PENDENTE (API nao respondeu): {nome} ({cnpj})")
        return False

    if not any(cnae_eh_atacadista(c) for c in cnaes):
        log.warning(f"  REPROVADO (sem CNAE atacadista): {nome} ({cnpj}) -> CNAEs: {', '.join(cnaes)}")
        return False

    log.info(f"  APROVADO: {nome} ({cnpj}) -- CNAE atacadista confirmado via {fonte}, UF cadastral {uf_cnpj}")
    return True


# ---------------------------------------------------------------------------
# 3) REGISTROS COLETADOS -- fonte da verdade (preencher manualmente por rodada)
# ---------------------------------------------------------------------------

@dataclass
class RegistroPreco:
    categoria: str
    codigo_fgv: int
    item: str
    uf: str
    data_coleta: str
    empresa: str
    cnpj: str
    fonte: str
    produto_pesquisado: str
    preco_produto: float
    valor_final: float
    obs_desconto: str
    valor_desconto: float | None = None
    valor_frete: float | None = None
    obs_frete: str = ""
    scroll_y: int = 0  # pixels para rolar antes do print, se o preco nao estiver visivel no topo da pagina


# Os registros da rodada da Região Sul (UNIFORMES FNDE, UFs PR/SC/SP) foram
# retirados nesta versão Região Centro: eram preços coletados para outra
# lista e outras UFs. A ferramenta continua a mesma; registre aqui os
# preços da pesquisa SEAD-GO (UFs GO e DF) quando houver coleta manual.
REGISTROS_UNIFORMES: list[RegistroPreco] = [
]

COLUNAS_FNDE_OUTPUT = [
    "Categoria", "Código FGV", "Item", "UF", "Data coleta", "Nome Empresa",
    "CNPJ", "FONTE", "PRODUTO PESQUISADO", "Preço PRODUTO", "VALOR DESCONTO",
    "OBS Desconto", "PREÇO FINAL", "VALOR FRETE", "OBS FRETE",
]


# ---------------------------------------------------------------------------
# 4) GERACAO DA PLANILHA output/FNDE_output.xlsx
# ---------------------------------------------------------------------------

def gerar_planilha(registros: list[RegistroPreco]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan1"
    ws.append(COLUNAS_FNDE_OUTPUT)

    for r in registros:
        ws.append([
            r.categoria, r.codigo_fgv, r.item, r.uf, r.data_coleta, r.empresa,
            r.cnpj, r.fonte, r.produto_pesquisado, r.preco_produto,
            r.valor_desconto, r.obs_desconto, r.valor_final, r.valor_frete,
            r.obs_frete,
        ])

    wb.save(PLANILHA_SAIDA)
    log.info(f"Planilha gerada: {PLANILHA_SAIDA} ({len(registros)} linhas)")


# ---------------------------------------------------------------------------
# 5) CAPTURA DE PRINTS (Playwright + Chromium baixado pelo proprio Playwright)
# ---------------------------------------------------------------------------

def slug_fornecedor(nome: str) -> str:
    texto = re.sub(r"[^a-zA-Z0-9]+", "_", nome).strip("_").lower()
    return texto[:40]


def capturar_prints(registros: list[RegistroPreco]) -> None:
    PRINTS_DIR.mkdir(parents=True, exist_ok=True)

    por_item: dict[int, list[RegistroPreco]] = {}
    for r in registros:
        por_item.setdefault(r.codigo_fgv, []).append(r)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        for codigo_fgv, itens in sorted(por_item.items()):
            pasta_item = PRINTS_DIR / str(codigo_fgv)
            pasta_item.mkdir(parents=True, exist_ok=True)
            log.info(f"\n### Capturando prints do item {codigo_fgv} ({len(itens)} precos) -> {pasta_item}")

            for idx, r in enumerate(itens, start=1):
                nome_arquivo = f"print_{idx:02d}_{slug_fornecedor(r.empresa)}.png"
                caminho = pasta_item / nome_arquivo
                try:
                    page.goto(r.fonte, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    if r.scroll_y:
                        page.mouse.wheel(0, r.scroll_y)
                        page.wait_for_timeout(1000)
                    page.screenshot(path=str(caminho), full_page=False)
                    log.info(f"  OK: {r.empresa} -> {nome_arquivo}")
                except PlaywrightTimeoutError:
                    log.error(f"  TIMEOUT ao abrir {r.fonte} ({r.empresa}) -- print nao capturado")
                except Exception as e:
                    log.exception(f"  ERRO ao capturar print de {r.empresa} ({r.fonte}): {e}")

        browser.close()


# ---------------------------------------------------------------------------
# 6) EXECUCAO
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Inicio da execucao -- coleta_precos_fnde_uniformes.py")

    log.info(f"\n=== 1) Validando CNAE de {len(REGISTROS_UNIFORMES)} registros (fornecedores unicos) ===")
    cnpjs_ja_validados: set[str] = set()
    for r in REGISTROS_UNIFORMES:
        if r.cnpj in cnpjs_ja_validados:
            continue
        cnpjs_ja_validados.add(r.cnpj)
        validar_fornecedor(r.empresa, r.cnpj, r.fonte)
        time.sleep(1.0)  # evita rate limit (429) da BrasilAPI em rajada

    log.info("\n=== 2) Gerando planilha de saida ===")
    gerar_planilha(REGISTROS_UNIFORMES)

    log.info("\n=== 3) Capturando prints de cada produto/preco ===")
    capturar_prints(REGISTROS_UNIFORMES)

    log.info("\nExecucao finalizada. Verifique output/FNDE_output.xlsx e output/prints/.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Execucao encerrada por erro nao tratado")
        raise
    else:
        log.info("Execucao da coleta FNDE (UNIFORMES) finalizada com sucesso")
