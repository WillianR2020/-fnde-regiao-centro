"""Decisão de quem revisou um match duvidoso. Um lugar só, lido pelo plano e pela coleta.

A varredura marca como "revisar" o par item x produto que o matching não
conseguiu decidir sozinho (medida ausente no título, embalagem que não diz
quantas vêm...). Até 11/10/2026 não havia para onde a decisão voltar: quem
revisava `revisar.csv` não tinha como mandar o par para a coleta, e na
SEAD-GO isso deixava 384 pares parados em 91 itens sem os 5 preços.

`data/raw/revisao_matching.csv` (id_item, dominio, url_produto, decisao,
motivo, decidido_em) segue a regra do `correcoes.csv`: correção é linha
nova num CSV de `data/raw/`, nunca edição à mão de saída do pipeline. A
chave inclui a URL: se a varredura achar outro produto para o mesmo item,
a decisão antiga não vale para ele.

`decisao`: "aceito" manda o par para a coleta; "descartado" tira o par do
plano e da revisão.
"""

from __future__ import annotations

import csv
from pathlib import Path

from src.core.log import obter

log = obter(__name__)

REVISAO = Path("data/raw/revisao_matching.csv")
COLUNAS = ["id_item", "dominio", "url_produto", "decisao", "motivo", "decidido_em"]


def ler_revisao(caminho: Path | None = None) -> dict[tuple[str, str, str], dict]:
    caminho = Path(caminho or REVISAO)
    if not caminho.exists():
        return {}
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        return {
            (r["id_item"].strip(), r["dominio"].strip(), r["url_produto"].strip()): r
            for r in csv.DictReader(f)
            if (r.get("decisao") or "").strip() in ("aceito", "descartado")
        }


def aplicar_revisao(achados: list[dict], caminho: Path | None = None) -> list[dict]:
    """Achados com a decisão humana aplicada. Não grava nada: o arquivo de achados fica como a varredura deixou."""
    decisoes = ler_revisao(caminho)
    if not decisoes:
        return achados
    saida = []
    vistos: set[tuple[str, str, str]] = set()
    por_url: dict[tuple[str, str], dict] = {}
    for a in achados:
        chave = (str(a.get("id_item", "")).strip(), str(a.get("dominio", "")).strip(),
                 str(a.get("url_produto", "")).strip())
        vistos.add(chave)
        por_url.setdefault(chave[1:], a)
        d = decisoes.get(chave)
        if d is None:
            saida.append(a)
        elif d["decisao"].strip() == "aceito":
            saida.append({**a, "classificacao": "aceito",
                          "motivo_match": f"revisão humana: {d.get('motivo', '')}".strip()})
        # "descartado": sai

    # O produto certo às vezes casou com o item vizinho ("fibra branca
    # pct c/ 10" achada na busca da fibra verde). Aceito para um item
    # cujo par não existe, com URL que a varredura achou no mesmo site,
    # vira achado desse item. URL que a varredura nunca viu não entra.
    for chave, d in decisoes.items():
        if chave in vistos or d["decisao"].strip() != "aceito":
            continue
        origem = por_url.get(chave[1:])
        if origem is None:
            continue
        saida.append({**origem, "id_item": chave[0], "classificacao": "aceito",
                      "motivo_match": f"revisão humana: {d.get('motivo', '')}".strip()})
    return saida
