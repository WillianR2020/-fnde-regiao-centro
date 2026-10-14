"""Especificação de item trocada por decisão de quem pediu a pesquisa."""

from src.core import tabelas
from src.core.matching import comparar_dimensoes


def test_ajuste_troca_a_descricao_e_mantem_o_nome(tmp_path, monkeypatch):
    itens = tmp_path / "itens.csv"
    itens.write_text(
        "id_item,categoria,grupo_insumo,item_curto,descricao,termos_busca\n"
        "1001,MATERIAIS DE CONSUMO,LIMPEZA E HIGIENE,Álcool etílico 54% líquido,"
        "ÁLCOOL ETÍLICO 54% LÍQUIDO. EMBALAGEM: GALÃO 5 L,alcool etilico 54\n",
        encoding="utf-8")
    ajustes = tmp_path / "ajustes_itens.csv"
    ajustes.write_text(
        "id_item,descricao,item_curto,motivo,decidido_em\n"
        '1001,"ÁLCOOL ETÍLICO 46,2% LÍQUIDO. EMBALAGEM: GALÃO 5 L",,troca aceita,13/10/2026\n',
        encoding="utf-8")
    monkeypatch.setattr(tabelas, "ITENS", itens)
    monkeypatch.setattr(tabelas, "AJUSTES_ITENS", ajustes)
    monkeypatch.setattr(tabelas, "TERMOS_EXTRAS", tmp_path / "nao_existe.csv")
    monkeypatch.setattr(tabelas.ler_ajustes_itens, "__defaults__", (ajustes,))

    item = tabelas.ler_itens(caminho=itens)["1001"]
    assert "46,2%" in item.descricao
    assert item.item_curto == "Álcool etílico 54% líquido"


def test_alcool_46_confere_com_46_2_e_nao_com_54():
    assert comparar_dimensoes("álcool etílico 46,2% galão 5 l",
                              "Álcool Líquido Audax 46° Facilita 5L")[0] == "bate"
    assert comparar_dimensoes("álcool etílico 54% galão 5 l",
                              "Álcool Líquido Audax 46° Facilita 5L")[0] == "diverge"
