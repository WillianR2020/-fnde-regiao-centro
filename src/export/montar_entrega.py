"""Monta a entrega a partir de data/coletas/. Não raspa nada.

Lê a pasta inteira, junta com itens.csv e fornecedores_master.csv, corta
nos 5 fornecedores por item e UF, e escreve uma planilha por categoria.

Pode rodar com a coleta pela metade — e deve. Rodar isto no primeiro site
coletado, antes de disparar os outros quarenta, é meia hora que economiza
um dia.

Sobre "os 5": cinco registros não são cinco fornecedores. Ordenar e
cortar em cinco entrega, com facilidade, cinco linhas do mesmo domínio,
todas sem preço, enquanto um candidato com preço fica na reserva. A
entrega pede 5 fornecedores DISTINTOS com preço e print — então o corte
primeiro descarta o que não serve, depois garante um registro por
domínio, e só então conta até cinco.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from src.core import tabelas
from src.core.busca import eh_url_de_busca
from src.core.revisao import ler_revisao
from src.core.frete import frete_possivel_de_coletar, houve_recusa, sede_na_uf_da_pesquisa
from src.core.log import obter
from src.export.prints import caminho_miniatura, embutir, gerar_miniatura
from src.models import CEPS_CENTRO, UF_PESQUISA

log = obter(__name__)

# As 15 colunas do template FNDE_output.xlsx, mais PRINT.
COLUNAS = [
    "Categoria", "Código FGV", "Item", "UF", "Data coleta", "Nome Empresa",
    "CNPJ", "FONTE", "PRODUTO PESQUISADO", "Preço PRODUTO", "VALOR DESCONTO",
    "OBS Desconto", "PREÇO FINAL", "VALOR FRETE", "OBS FRETE", "PRINT",
]

ALVO_POR_ITEM = 5
SCORE_MINIMO = 0.6  # abaixo disso o matching nem classifica como "revisar"

RESERVA = Path("data/interim/reserva.csv")
PENDENCIAS = Path("data/interim/pendencias.csv")


# ---------------------------------------------------------------------------
# Seleção
# ---------------------------------------------------------------------------

def problema(candidato: dict) -> str:
    """Por que este registro NÃO pode ir para a entrega. "" = pode.

    São as três coisas que o solicitante confere numa linha: que tem
    preço, que tem prova, e que o produto é mesmo o pedido.
    """
    preco = candidato.get("preco_final") or candidato.get("preco_produto")
    if preco in (None, "") or _numero(preco) <= 0:
        return "sem preço"

    if eh_url_de_busca(candidato.get("url_produto") or ""):
        return "URL é página de busca, não de produto"

    caminho = (candidato.get("caminho_print") or "").strip()
    if not caminho:
        return "sem print"
    if not Path(caminho).exists():
        return "print não está no disco"

    # Regra da SEAD-GO de 09/10/2026: sede em GO entra sem frete; sede
    # fora de GO só entra com a entrega em GO confirmada por uma cotação.
    # "Sob consulta" deixou de bastar -- não prova que a empresa entrega.
    # Exceção: a loja tem calculadora de frete e só o robô não leu o
    # valor. A linha entra marcada FRETE A COLETAR (ver frete_a_coletar).
    if not sede_em_go(candidato):
        obs_frete = (candidato.get("obs_frete") or "").strip()
        if candidato.get("valor_frete") in (None, ""):
            if houve_recusa(obs_frete):
                return f"fornecedor não entrega em {UF_PESQUISA}"
            if not frete_possivel_de_coletar(obs_frete):
                return f"entrega em {UF_PESQUISA} não confirmada (sede fora de {UF_PESQUISA})"

    # Par recusado por quem revisou depois de coletado: a coleta fica no
    # arquivo, mas a linha não entra.
    if candidato.get("descartado_revisao"):
        return "par descartado na revisão humana"

    # Par aprovado por quem revisou (data/raw/revisao_matching.csv): a
    # decisão humana substitui a nota do matching, que foi o que mandou o
    # par para revisão.
    if (_numero(candidato.get("score_match")) < SCORE_MINIMO
            and not candidato.get("revisado")):
        return f"score {_numero(candidato.get('score_match')):.2f} abaixo do mínimo"

    return ""


def frete_a_coletar(candidato: dict) -> bool:
    """Sede fora de GO, sem valor de frete, mas a loja calcula frete: falta coletar à mão."""
    return (not sede_em_go(candidato)
            and candidato.get("valor_frete") in (None, "")
            and frete_possivel_de_coletar(candidato.get("obs_frete")))


def sede_em_go(candidato: dict) -> bool:
    """`sede_go` vem do master (montar() anota); sem ele, a sede gravada na coleta."""
    if candidato.get("sede_go") is not None:
        return bool(candidato["sede_go"])
    return sede_na_uf_da_pesquisa(candidato.get("uf_sede"))


def preco_total(candidato: dict) -> float | None:
    """O PREÇO FINAL da entrega: produto, mais o frete para GO se a sede é de fora."""
    preco = _numero_ou_nada(candidato.get("preco_final") or candidato.get("preco_produto"))
    if preco is None:
        return None
    if sede_em_go(candidato):
        return preco
    frete = _numero_ou_nada(candidato.get("valor_frete"))
    return round(preco + (frete or 0.0), 2)


def _negativo_da_data(valor) -> str:
    """Chave de ordenação que põe a data ISO mais recente primeiro."""
    texto = str(valor or "")
    return "".join(chr(0x10FFFF - ord(ch)) for ch in texto) if texto else chr(0x10FFFF)


def escolher_cinco(candidatos: list[dict]) -> tuple[list[dict], list[dict]]:
    """Os 5 que vão para a entrega e os que sobram para a reserva.

    Critério, nessa ordem: descarta o que não serve; um registro por
    domínio; linha completa antes de linha com FRETE A COLETAR (esta só
    ocupa vaga que sobrou); maior score_match; depois menor preço final.

    A reserva vai para data/interim/reserva.csv e fica só no repositório.
    Ela carrega o motivo de cada exclusão -- sem isso, "por que este
    fornecedor não entrou?" só se responde reprocessando tudo.
    """
    validos: list[dict] = []
    reserva: list[dict] = []

    for c in candidatos:
        motivo = problema(c)
        if motivo:
            reserva.append({**c, "motivo_reserva": motivo})
        else:
            validos.append(c)

    # Dois registros do mesmo fornecedor (coleta refeita): fica o
    # completo, o de maior score e, entre iguais, o MAIS RECENTE -- não o
    # mais barato, que guardaria um preço velho ou mal lido.
    por_dominio: dict[str, dict] = {}
    for c in sorted(validos, key=lambda c: (
            frete_a_coletar(c), -_numero(c.get("score_match")),
            _negativo_da_data(c.get("coletado_em")))):
        dominio = c.get("dominio", "")
        if dominio in por_dominio:
            reserva.append({**c, "motivo_reserva": "já há registro deste fornecedor"})
        else:
            por_dominio[dominio] = c

    ordenados = sorted(
        por_dominio.values(),
        key=lambda c: (frete_a_coletar(c), -_numero(c.get("score_match")), _preco(c)),
    )

    escolhidos: list[dict] = []
    dominios: set[str] = set()
    for c in ordenados:
        dominio = c.get("dominio", "")
        if len(escolhidos) < ALVO_POR_ITEM and dominio not in dominios:
            dominios.add(dominio)
            escolhidos.append(c)
        else:
            razao = ("já há registro deste fornecedor" if dominio in dominios
                     else f"além dos {ALVO_POR_ITEM} primeiros")
            reserva.append({**c, "motivo_reserva": razao})

    return escolhidos, reserva


# ---------------------------------------------------------------------------
# Leitura das coletas
# ---------------------------------------------------------------------------

def ler_coletas(entrada: Path) -> list[dict]:
    """Todos os .jsonl da pasta, tolerando linha truncada."""
    from src.runners.etapa3_coletar import ler_registros

    registros: list[dict] = []
    for arquivo in sorted(Path(entrada).glob("*.jsonl")):
        lidos, descartadas = ler_registros(arquivo)
        if descartadas:
            log.warning("%s: %d linhas ilegiveis ignoradas", arquivo.name, descartadas)
        registros.extend(lidos)
    return registros


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------

def montar(entrada: Path, saida: Path, categorias: list[str] | None = None,
           resultados: Path | None = None) -> dict:
    """Lê os .jsonl, escolhe os 5 e gera um .xlsx por categoria.

    Com `resultados`, monta a pasta de entrega pedida em 11/10/2026:

        RESULTADOS/EXCEL/{CATEGORIA}.xlsx         (no lugar de `saida`)
        RESULTADOS/PRINTS/{CATEGORIA}/{id FGV}__{dominio}__GO.png

    Só os prints que entraram na planilha vão para PRINTS/, copiados do
    data/output/prints/ (o original fica onde está). O nome começa pelo
    ID FGV, então a pasta ordenada por nome mostra os 5 de cada item
    juntos. O hyperlink da coluna PRINT é relativo (`../PRINTS/...`):
    a pasta RESULTADOS pode ser copiada inteira para outra máquina.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    itens = tabelas.ler_itens(categorias)
    fornecedores = tabelas.ler_fornecedores(apenas_aprovados=False)
    coletas = ler_coletas(entrada)
    revisao = ler_revisao()

    if not coletas:
        log.warning("nenhuma coleta em %s -- a etapa 3 ja rodou?", entrada)

    # (categoria, id_item, uf) -> candidatos
    grupos: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for c in coletas:
        item = itens.get(c.get("id_item", ""))
        if item is None:
            continue  # item de outra categoria, ou removido da lista
        if c.get("uf", "") not in CEPS_CENTRO:
            continue  # coleta de UF que saiu da pesquisa (DF, até 09/10/2026)
        forn = fornecedores.get(c.get("dominio", ""))
        # A sede do master (Receita) ganha da gravada na coleta.
        uf_sede = forn.uf if forn and forn.uf else c.get("uf_sede")
        chave = (c.get("id_item", ""), c.get("dominio", ""), (c.get("url_produto") or "").strip())
        decisao = (revisao.get(chave, {}).get("decisao") or "").strip()
        c = {**c, "sede_go": sede_na_uf_da_pesquisa(uf_sede),
             "revisado": decisao == "aceito",
             "descartado_revisao": decisao == "descartado"}
        grupos[(item.categoria, item.id_item, c.get("uf", ""))].append(c)

    reserva_geral: list[dict] = []
    pendencias: list[dict] = []
    linhas_por_categoria: dict[str, list[tuple[dict, dict]]] = defaultdict(list)

    for (categoria, id_item, uf), candidatos in sorted(grupos.items()):
        escolhidos, reserva = escolher_cinco(candidatos)
        reserva_geral.extend(reserva)

        a_coletar = sum(1 for c in escolhidos if frete_a_coletar(c))
        if len(escolhidos) < ALVO_POR_ITEM or a_coletar:
            pendencias.append({
                "categoria": categoria,
                "id_item": id_item,
                "uf": uf,
                "fornecedores": len(escolhidos),
                "faltam": ALVO_POR_ITEM - len(escolhidos),
                # Estão na planilha, mas o VALOR FRETE é preenchido à mão.
                "frete_a_coletar": a_coletar,
                "candidatos_descartados": len(reserva),
            })

        item = itens[id_item]
        for c in escolhidos:
            linhas_por_categoria[categoria].append((c, _linha(c, item, fornecedores)))

    pasta_prints = None
    if resultados is not None:
        saida = Path(resultados) / "EXCEL"
        pasta_prints = Path(resultados) / "PRINTS"
    saida = Path(saida)
    saida.mkdir(parents=True, exist_ok=True)
    gerados: list[str] = []
    copiados: set[Path] = set()

    for categoria, linhas in sorted(linhas_por_categoria.items()):
        wb = Workbook()
        ws = wb.active
        ws.title = categoria[:31] or "ENTREGA"

        ws.append(COLUNAS)
        for celula in ws[1]:
            celula.font = Font(bold=True)
        ws.freeze_panes = "A2"

        coluna_print = COLUNAS.index("PRINT") + 1

        for numero, (coleta, linha) in enumerate(linhas, start=2):
            ws.append([linha.get(c) for c in COLUNAS])
            # Formato da entrega do grupo: texto quebrado e alinhado no
            # topo, porque a linha tem a altura do print (300 pontos).
            for celula in ws[numero]:
                celula.alignment = Alignment(wrap_text=True, vertical="top")
            if frete_a_coletar(coleta):
                _marcar_frete_a_coletar(ws, numero, coleta)
            if pasta_prints is not None:
                coleta = _copiar_print(coleta, pasta_prints / categoria)
                copiados.add(Path(coleta.get("caminho_print") or "").resolve())
            _embutir_print(ws, numero, coluna_print, coleta, saida)

        _ajustar_larguras(ws)
        caminho = saida / f"{_arquivo(categoria)}.xlsx"
        wb.save(caminho)
        gerados.append(str(caminho))
        log.info("%s: %d linhas", caminho, len(linhas))

    if pasta_prints is not None:
        _limpar_prints_antigos(pasta_prints, linhas_por_categoria, copiados)

    _gravar_csv(RESERVA, reserva_geral)
    _gravar_csv(PENDENCIAS, pendencias)

    resumo = {
        "linhas": sum(len(v) for v in linhas_por_categoria.values()),
        "planilhas": gerados,
        "reserva": len(reserva_geral),
        "pendencias": sum(1 for p in pendencias if p["faltam"] > 0),
        "frete_a_coletar": sum(p["frete_a_coletar"] for p in pendencias),
    }
    print(
        f"[montar] {resumo['linhas']} linhas em {len(gerados)} planilha(s); "
        f"{resumo['reserva']} na reserva; "
        f"{resumo['pendencias']} combinações item/UF ainda sem 5 fornecedores; "
        f"{resumo['frete_a_coletar']} linha(s) com FRETE A COLETAR à mão"
    )
    return resumo


def _linha(coleta: dict, item, fornecedores: dict) -> dict:
    forn = fornecedores.get(coleta.get("dominio", ""))
    return {
        "Categoria": item.categoria,
        "Código FGV": item.id_item,
        "Item": item.item_curto or item.descricao[:90],
        "UF": coleta.get("uf", ""),
        "Data coleta": _data(coleta.get("coletado_em")),
        "Nome Empresa": forn.nome if forn else coleta.get("dominio", ""),
        "CNPJ": _cnpj(forn.cnpj if forn else None),
        "FONTE": coleta.get("url_produto", ""),
        "PRODUTO PESQUISADO": coleta.get("titulo_encontrado", ""),
        "Preço PRODUTO": _numero_ou_nada(coleta.get("preco_produto")),
        "VALOR DESCONTO": _numero_ou_nada(coleta.get("valor_desconto")),
        "OBS Desconto": coleta.get("obs_desconto", ""),
        # Produto (já com desconto) + frete para GO quando a sede é de
        # fora. Sede em GO: só o produto, e VALOR FRETE fica vazio.
        "PREÇO FINAL": preco_total(coleta),
        "VALOR FRETE": (None if sede_em_go(coleta)
                        else _numero_ou_nada(coleta.get("valor_frete"))),
        "OBS FRETE": coleta.get("obs_frete", ""),
        "PRINT": "",  # a imagem entra ancorada; o texto ficaria atrás dela
    }


def _marcar_frete_a_coletar(ws, numero: int, coleta: dict) -> None:
    """Linha de fora de GO cujo frete o robô não leu, mas a loja calcula.

    VALOR FRETE fica em branco e amarelo para alguém preencher com o
    CEP de GO; PREÇO FINAL vira fórmula, então soma o frete sozinho
    assim que ele for digitado.
    """
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    amarelo = PatternFill("solid", fgColor="FFEB9C")
    col = {nome: n for n, nome in enumerate(COLUNAS, start=1)}
    frete = get_column_letter(col["VALOR FRETE"])
    preco = _numero_ou_nada(coleta.get("preco_final") or coleta.get("preco_produto")) or 0

    ws.cell(numero, col["PREÇO FINAL"], f"={preco}+N({frete}{numero})").fill = amarelo
    ws.cell(numero, col["VALOR FRETE"]).fill = amarelo
    obs = ws.cell(numero, col["OBS FRETE"],
                  f"FRETE A COLETAR: o site calcula frete, mas o robô não leu o valor. "
                  f"Preencher VALOR FRETE com o CEP {CEPS_CENTRO[UF_PESQUISA]}.")
    obs.fill = amarelo
    obs.font = Font(bold=True)


def _limpar_prints_antigos(pasta_prints: Path, linhas_por_categoria: dict,
                           copiados: set[Path]) -> None:
    """Tira de PRINTS/ a cópia de quem saiu da planilha nesta montagem.

    Sem isso, um fornecedor trocado por outro deixava o print velho na
    pasta, e PRINTS/ deixava de ser "os 5 de cada item". Só mexe nas
    categorias montadas agora; o original em data/output/prints/ fica.
    """
    removidos = 0
    for categoria in linhas_por_categoria:
        for png in (pasta_prints / categoria).glob("*.png"):
            if png.resolve() not in copiados:
                png.unlink()
                removidos += 1
    if removidos:
        log.info("%s: %d prints de linhas que sairam da entrega removidos", pasta_prints, removidos)


def _copiar_print(coleta: dict, destino: Path) -> dict:
    """Copia o PNG da linha para a pasta de entrega e devolve a coleta apontando para a cópia."""
    import shutil

    original = Path((coleta.get("caminho_print") or "").strip())
    if not original.name or not original.exists():
        return coleta
    destino.mkdir(parents=True, exist_ok=True)
    copia = destino / original.name
    if not copia.exists() or copia.stat().st_mtime < original.stat().st_mtime:
        shutil.copy2(original, copia)
    return {**coleta, "caminho_print": str(copia)}


def _embutir_print(ws, linha: int, coluna: int, coleta: dict,
                   pasta_planilha: Path | None = None) -> None:
    original = Path((coleta.get("caminho_print") or "").strip())
    if not original.name or not original.exists():
        return
    try:
        miniatura = gerar_miniatura(original, caminho_miniatura(original))
        embutir(ws, linha, coluna, miniatura, original, relativo_a=pasta_planilha)
    except Exception as e:  # uma miniatura ruim não pode derrubar a entrega
        log.warning("print %s nao entrou na planilha (%s: %s)",
                    original, type(e).__name__, e)


def _ajustar_larguras(ws) -> None:
    from openpyxl.utils import get_column_letter

    larguras = {
        "Categoria": 14, "Código FGV": 12, "Item": 45, "UF": 6,
        "Data coleta": 12, "Nome Empresa": 28, "CNPJ": 20, "FONTE": 40,
        "PRODUTO PESQUISADO": 45, "Preço PRODUTO": 14, "VALOR DESCONTO": 14,
        "OBS Desconto": 24, "PREÇO FINAL": 14, "VALOR FRETE": 12,
        "OBS FRETE": 28,
    }
    for numero, nome in enumerate(COLUNAS, start=1):
        if nome in larguras:
            ws.column_dimensions[get_column_letter(numero)].width = larguras[nome]


def _gravar_csv(caminho: Path, linhas: list[dict]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    if not linhas:
        caminho.write_text("", encoding="utf-8")
        return
    colunas = list({chave: None for linha in linhas for chave in linha})
    with caminho.open("w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=colunas, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(linhas)


# ---------------------------------------------------------------------------

def _numero(valor) -> float:
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _numero_ou_nada(valor):
    if valor in (None, ""):
        return None
    try:
        return round(float(str(valor).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def _preco(candidato: dict) -> float:
    """Para desempate: o que a SEAD-GO paga de fato, frete incluído."""
    valor = preco_total(candidato) or 0.0
    return valor if valor > 0 else float("inf")


def _data(valor) -> str:
    texto = str(valor or "")
    return texto[:10]


def _cnpj(digitos: str | None) -> str:
    d = "".join(c for c in str(digitos or "") if c.isdigit())
    if len(d) != 14:
        return d
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def _arquivo(texto: str) -> str:
    from unidecode import unidecode

    limpo = unidecode(str(texto)).upper()
    return "".join(c if c.isalnum() else "_" for c in limpo).strip("_") or "ENTREGA"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--entrada", type=Path, default=Path("data/coletas"))
    p.add_argument("--saida", type=Path, default=Path("data/output"))
    p.add_argument("--categoria", action="append", dest="categorias")
    p.add_argument("--resultados", type=Path, default=None,
                   help="pasta de entrega: grava EXCEL/ e PRINTS/ dentro dela")
    a = p.parse_args()
    montar(a.entrada, a.saida, a.categorias, a.resultados)
