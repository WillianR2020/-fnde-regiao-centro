"""Decisão humana sobre match duvidoso (data/raw/revisao_matching.csv)."""

from src.core.revisao import aplicar_revisao


def _csv(tmp_path, linhas):
    caminho = tmp_path / "revisao.csv"
    caminho.write_text(
        "id_item,dominio,url_produto,decisao,motivo,decidido_em\n" + "\n".join(linhas) + "\n",
        encoding="utf-8",
    )
    return caminho


ACHADOS = [
    {"id_item": "1042", "dominio": "a.com.br", "url_produto": "https://a.com.br/saco-20l", "classificacao": "revisar"},
    {"id_item": "1043", "dominio": "a.com.br", "url_produto": "https://a.com.br/saco-30l", "classificacao": "revisar"},
    {"id_item": "1044", "dominio": "a.com.br", "url_produto": "https://a.com.br/saco-40l", "classificacao": "revisar"},
]


def test_aceito_vai_para_a_coleta_e_descartado_sai(tmp_path):
    caminho = _csv(tmp_path, [
        "1042,a.com.br,https://a.com.br/saco-20l,aceito,20 L pacote 100,11/10/2026",
        "1043,a.com.br,https://a.com.br/saco-30l,descartado,é de 50 L,11/10/2026",
    ])
    saida = {a["id_item"]: a for a in aplicar_revisao(ACHADOS, caminho)}
    assert saida["1042"]["classificacao"] == "aceito"
    assert "revisão humana" in saida["1042"]["motivo_match"]
    assert "1043" not in saida
    assert saida["1044"]["classificacao"] == "revisar"


def test_decisao_nao_vale_para_outra_url(tmp_path):
    """A varredura achou outro produto: a decisão antiga não é sobre ele."""
    caminho = _csv(tmp_path, ["1042,a.com.br,https://a.com.br/outro,aceito,,11/10/2026"])
    assert aplicar_revisao(ACHADOS, caminho)[0]["classificacao"] == "revisar"


def test_aceito_leva_produto_achado_em_outro_item(tmp_path):
    """O saco de 30 L casou com o item 1043; quem revisou o põe no 1045."""
    caminho = _csv(tmp_path, ["1045,a.com.br,https://a.com.br/saco-30l,aceito,é o 30 L,11/10/2026"])
    saida = aplicar_revisao(ACHADOS, caminho)
    novo = [a for a in saida if a["id_item"] == "1045"]
    assert len(novo) == 1
    assert novo[0]["url_produto"] == "https://a.com.br/saco-30l"
    assert novo[0]["classificacao"] == "aceito"
    # o par original continua como estava
    assert any(a["id_item"] == "1043" and a["classificacao"] == "revisar" for a in saida)


def test_aceito_de_url_que_a_varredura_nunca_viu_nao_entra(tmp_path):
    caminho = _csv(tmp_path, ["1045,a.com.br,https://a.com.br/inventado,aceito,,11/10/2026"])
    assert [a["id_item"] for a in aplicar_revisao(ACHADOS, caminho)] == ["1042", "1043", "1044"]


def test_sem_arquivo_nao_muda_nada(tmp_path):
    assert aplicar_revisao(ACHADOS, tmp_path / "nao_existe.csv") == ACHADOS
