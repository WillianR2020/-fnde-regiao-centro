"""Termos de busca conferidos à mão (data/raw/termos_extras.csv)."""

from src.core.tabelas import ler_itens


def test_termo_extra_vem_depois_dos_gerados(tmp_path):
    itens = tmp_path / "itens.csv"
    itens.write_text(
        "id_item,categoria,grupo_insumo,item_curto,descricao,termos_busca\n"
        "1107,EPIS E UNIFORMES,EPI / UNIFORME,Calçado,CALÇADO DE SEGURANÇA,calcado seguranca\n"
        "1106,EPIS E UNIFORMES,EPI / UNIFORME,Meia,MEIA,meia\n",
        encoding="utf-8",
    )
    extras = tmp_path / "termos_extras.csv"
    extras.write_text("id_item,termo,motivo\n1107,botina seguranca,loja chama de botina\n",
                      encoding="utf-8")
    lidos = ler_itens(caminho=itens, extras=extras)
    assert lidos["1107"].termos_busca == ["calcado seguranca", "botina seguranca"]
    assert lidos["1106"].termos_busca == ["meia"]


def test_outra_lista_nao_recebe_extras_sem_pedir(tmp_path):
    itens = tmp_path / "itens.csv"
    itens.write_text(
        "id_item,categoria,grupo_insumo,item_curto,descricao,termos_busca\n"
        "1107,EPIS E UNIFORMES,EPI / UNIFORME,Calçado,CALÇADO DE SEGURANÇA,calcado seguranca\n",
        encoding="utf-8",
    )
    assert ler_itens(caminho=itens)["1107"].termos_busca == ["calcado seguranca"]
