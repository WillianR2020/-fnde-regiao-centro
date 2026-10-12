"""Consulta cadastral do CNPJ em API pública, com cache em disco."""

from __future__ import annotations

import json
from pathlib import Path

from src.core.http import Cliente

CACHE = Path("data/raw/cnpj")

# Primária e fallback. Ambas públicas, sem chave.
FONTES = (
    "https://minhareceita.org/{cnpj}",
    "https://brasilapi.com.br/api/cnpj/v1/{cnpj}",
)

# Terceira fonte, para quando as duas acima caem juntas -- aconteceu em
# 08-09/10/2026 (500 e 503 o dia inteiro). Formato próprio, convertido
# por `_de_cnpj_ws` para o das outras antes de ir para o cache, de modo
# que `normalizar` continua sendo o único lugar que lê os campos.
CNPJ_WS = "https://publica.cnpj.ws/cnpj/{cnpj}"
INTERVALO_CNPJ_WS = 21.0  # a API pública aceita 3 consultas por minuto
_ultima_cnpj_ws = 0.0


def consultar(cnpj: str, cliente: Cliente) -> dict | None:
    """Devolve a resposta bruta da Receita. Guarda em data/raw/cnpj/.

    O cache não é otimização, é necessidade: a BrasilAPI limita ~3
    req/min por IP e nós temos ~200 CNPJs para consultar várias vezes
    durante o desenvolvimento.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    arquivo = CACHE / f"{cnpj}.json"
    if arquivo.exists():
        return json.loads(arquivo.read_text(encoding="utf-8"))

    for molde in FONTES:
        try:
            dados = cliente.get_json(molde.format(cnpj=cnpj))
        except Exception:
            continue
        if isinstance(dados, dict) and dados:
            arquivo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
            return dados

    dados = _consultar_cnpj_ws(cnpj, cliente)
    if dados:
        arquivo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    return dados


def _consultar_cnpj_ws(cnpj: str, cliente: Cliente) -> dict | None:
    import time

    global _ultima_cnpj_ws
    espera = INTERVALO_CNPJ_WS - (time.monotonic() - _ultima_cnpj_ws)
    if espera > 0:
        time.sleep(espera)
    _ultima_cnpj_ws = time.monotonic()
    try:
        bruto = cliente.get_json(CNPJ_WS.format(cnpj=cnpj))
    except Exception:
        return None
    return _de_cnpj_ws(bruto)


def _de_cnpj_ws(bruto) -> dict | None:
    """Resposta da publica.cnpj.ws no formato da BrasilAPI (o que `normalizar` lê)."""
    if not isinstance(bruto, dict) or not isinstance(bruto.get("estabelecimento"), dict):
        return None
    est = bruto["estabelecimento"]
    principal = est.get("atividade_principal") or {}
    return {
        "razao_social": bruto.get("razao_social"),
        "uf": (est.get("estado") or {}).get("sigla"),
        "municipio": (est.get("cidade") or {}).get("nome"),
        "cnae_fiscal": principal.get("id") or principal.get("subclasse"),
        "cnaes_secundarios": [
            {"codigo": a.get("id") or a.get("subclasse")}
            for a in (est.get("atividades_secundarias") or [])
        ],
        "descricao_situacao_cadastral": est.get("situacao_cadastral") or "",
        "fonte": "publica.cnpj.ws",
    }


def normalizar(dados: dict) -> dict:
    """Achata os campos que o pipeline usa.

    As duas APIs nomeiam os campos de forma diferente. Esta função é o
    único lugar do projeto que sabe disso.

    TODO: conferir os nomes exatos contra uma resposta real de cada
    fonte antes de confiar (salve uma em tests/fixtures/).
    """
    return {
        "razao_social": dados.get("razao_social") or dados.get("nome_empresarial"),
        "uf": dados.get("uf"),
        "municipio": dados.get("municipio"),
        "cnae_principal": _codigo(dados.get("cnae_fiscal") or dados.get("codigo_cnae_fiscal")),
        "cnaes_secundarios": [
            _codigo(c.get("codigo") or c.get("cnae"))
            for c in (dados.get("cnaes_secundarios") or [])
        ],
        "situacao_cadastral": (dados.get("descricao_situacao_cadastral") or "").upper(),
    }


def _codigo(valor) -> str:
    """Normaliza '4649-4/99', '4649499' e 4649499 para '4649499'."""
    import re

    return re.sub(r"\D", "", str(valor or ""))
