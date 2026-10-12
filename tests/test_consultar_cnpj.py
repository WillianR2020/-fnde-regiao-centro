"""Terceira fonte de CNPJ (publica.cnpj.ws) convertida para o formato das outras."""

from src.validacao.classificar_cnae import decidir
from src.validacao.consultar_cnpj import _de_cnpj_ws, normalizar

# Recorte de uma resposta real (CNPJ da Beka, 09/10/2026).
RESPOSTA = {
    "razao_social": "BEKA DISTRIBUIDORA DE PRODUTOS DE LIMPEZA E HIGIENE LTDA",
    "estabelecimento": {
        "situacao_cadastral": "Ativa",
        "atividade_principal": {"id": "4649408", "descricao": "Comércio atacadista de ..."},
        "atividades_secundarias": [{"id": "4789005"}, {"id": "4686902"}],
        "estado": {"sigla": "GO"},
        "cidade": {"nome": "Goiânia"},
    },
}


def test_cnpj_ws_vira_o_formato_que_normalizar_le():
    campos = normalizar(_de_cnpj_ws(RESPOSTA))
    assert campos["razao_social"].startswith("BEKA")
    assert campos["uf"] == "GO"
    assert campos["cnae_principal"] == "4649408"
    assert campos["cnaes_secundarios"] == ["4789005", "4686902"]
    assert campos["situacao_cadastral"] == "ATIVA"
    status, _, _ = decidir(campos["cnae_principal"], campos["cnaes_secundarios"],
                           campos["situacao_cadastral"])
    assert status.value == "APROVADO"


def test_resposta_sem_estabelecimento_e_descartada():
    assert _de_cnpj_ws({"status": 429, "titulo": "Muitas requisições"}) is None
    assert _de_cnpj_ws(None) is None
