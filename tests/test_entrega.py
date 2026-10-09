"""Entrega em Goiás como critério -- regra da SEAD-GO desde 09/10/2026.

5 preços por item, todos para GO; o DF saiu da pesquisa. A sede decide o
frete: sede em GO entra sem frete; sede fora de GO só entra com entrega
em GO confirmada, e o frete é somado ao preço.

A regra que continua: recusa reprova, silêncio não. Um site fora do ar
não pode virar "não entrega em GO".
"""

from __future__ import annotations

import pytest

from src.core.frete import (FRETE_DISPENSADO, SEM_ENTREGA, houve_recusa,
                             sede_na_uf_da_pesquisa)
from src.export.montar_entrega import preco_total, problema
from src.models import CEPS_CENTRO, UF_PESQUISA, Fornecedor, Status
from src.runners.etapa1_validar import UFS_CENTRO, decidir_entrega


def fornecedor(uf: str = "AM", **extra) -> Fornecedor:
    base = dict(
        nome="Atacado Exemplo", dominio="exemplo.com.br",
        url_base="https://exemplo.com.br", uf=uf,
        cnpj="61340901000117", cnae_principal="4649499",
        situacao_cadastral="ATIVA", status=Status.APROVADO,
        motivo="CNAE principal 4649499 (atacado)",
    )
    base.update(extra)
    return Fornecedor(**base)


# ---------------------------------------------------------------------------
# A UF da pesquisa
# ---------------------------------------------------------------------------

def test_a_pesquisa_cobre_so_go():
    """O DF saiu da pesquisa em 09/10/2026."""
    assert set(CEPS_CENTRO) == {"GO"}
    assert UF_PESQUISA == "GO"
    assert UFS_CENTRO == tuple(CEPS_CENTRO)
    assert all(len(cep) == 8 and cep.isdigit() for cep in CEPS_CENTRO.values())


# ---------------------------------------------------------------------------
# A sede decide o frete
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uf", ["GO", "go", " GO "])
def test_sede_em_go_atende_go_sem_teste_de_frete(uf):
    forn = decidir_entrega(fornecedor(uf, entrega_centro={}))
    assert forn.status is Status.APROVADO
    assert forn.entrega_centro == {"GO": True}
    assert "sede em GO" in forn.motivo


@pytest.mark.parametrize("uf", ["AM", "SP", "PR", "DF", ""])
def test_sede_fora_de_go_entra_se_entrega_em_go(uf):
    forn = decidir_entrega(fornecedor(uf, entrega_centro={"GO": True}))
    assert forn.status is Status.APROVADO
    assert "entrega em GO" in forn.motivo


def test_sede_em_branco_conta_como_fora_de_go():
    """Sem saber onde a empresa fica, a entrega tem que ser provada."""
    assert not sede_na_uf_da_pesquisa("")
    assert not sede_na_uf_da_pesquisa(None)


# ---------------------------------------------------------------------------
# Recusa reprova, silêncio não
# ---------------------------------------------------------------------------

def test_recusa_em_go_reprova_quem_e_de_fora():
    forn = decidir_entrega(fornecedor("SP", entrega_centro={"GO": False}))
    assert forn.status is Status.REPROVADO
    assert forn.motivo == "não entrega em GO"


def test_sem_resposta_fica_pendente_e_nao_reprovado():
    """Site fora do ar não pode sumir da lista."""
    forn = decidir_entrega(fornecedor("SP", entrega_centro={}))
    assert forn.status is Status.PENDENTE
    assert "não confirmada" in forn.motivo


# ---------------------------------------------------------------------------
# O marcador de recusa
# ---------------------------------------------------------------------------

def test_reconhece_a_recusa_com_e_sem_acento():
    assert houve_recusa(f"{SEM_ENTREGA} para este CEP")
    assert houve_recusa("Sem Opção de Entrega para este CEP")


def test_nao_confunde_sob_consulta_com_recusa():
    assert not houve_recusa("sob consulta: cotação só por telefone")
    assert not houve_recusa("cotacao indisponivel (FalhaDeRede)")


# ---------------------------------------------------------------------------
# A linha não entra na planilha
# ---------------------------------------------------------------------------

@pytest.fixture
def registro(tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"png falso")
    return {
        "id_item": "1000", "dominio": "exemplo.com.br", "uf": "GO",
        "preco_produto": 24.9, "preco_final": 24.9,
        "valor_frete": None, "obs_frete": "",
        "caminho_print": str(png), "score_match": 0.92,
        "uf_sede": "SP",
    }


def test_linha_de_fora_que_recusa_go_nao_entra(registro):
    registro["obs_frete"] = f"{SEM_ENTREGA} para este CEP"
    assert problema(registro) == "fornecedor não entrega em GO"


def test_sob_consulta_nao_basta_para_quem_e_de_fora(registro):
    """Regra de 09/10/2026: de fora de GO, só com entrega confirmada."""
    registro["obs_frete"] = "sob consulta: cotação só por telefone"
    assert "não confirmada" in problema(registro)


def test_frete_em_branco_de_fora_nao_entra(registro):
    assert "não confirmada" in problema(registro)


def test_de_fora_com_frete_entra_e_soma_o_frete(registro):
    registro["valor_frete"] = 10.1
    registro["obs_frete"] = "PAC 7 dias"
    assert problema(registro) == ""
    assert preco_total(registro) == 35.0


def test_sede_em_go_entra_sem_frete_e_sem_somar(registro):
    registro["uf_sede"] = "GO"
    registro["obs_frete"] = FRETE_DISPENSADO
    assert problema(registro) == ""
    assert preco_total(registro) == 24.9


def test_sede_do_master_ganha_da_gravada_na_coleta(registro):
    """montar() anota sede_go a partir do master; a coleta é o reserva."""
    registro["sede_go"] = True  # master diz GO, coleta gravou SP
    assert problema(registro) == ""
    assert preco_total(registro) == 24.9


# ---------------------------------------------------------------------------
# Frete possível de coletar à mão (09/10/2026)
# ---------------------------------------------------------------------------

def test_calculadora_sem_valor_lido_entra_como_frete_a_coletar(registro):
    """A loja calcula frete, só o robô não leu: salva e mostra, não descarta."""
    from src.core.frete import FRETE_A_COLETAR
    from src.export.montar_entrega import frete_a_coletar

    registro["obs_frete"] = f"{FRETE_A_COLETAR}: a pagina tem calculadora de frete"
    assert problema(registro) == ""
    assert frete_a_coletar(registro)


def test_linha_completa_ganha_a_vaga_da_linha_com_frete_a_coletar(registro):
    from src.core.frete import FRETE_A_COLETAR
    from src.export.montar_entrega import escolher_cinco, frete_a_coletar

    a_coletar = {**registro, "dominio": "a.com.br", "score_match": 0.99,
                 "obs_frete": f"{FRETE_A_COLETAR}: x"}
    completas = [{**registro, "dominio": f"{n}.com.br", "valor_frete": 9.0,
                  "obs_frete": "PAC"} for n in "bcdef"]
    escolhidos, reserva = escolher_cinco([a_coletar] + completas)
    assert len(escolhidos) == 5
    assert not any(frete_a_coletar(c) for c in escolhidos)
    assert reserva[0]["dominio"] == "a.com.br"


def test_frete_a_coletar_ocupa_vaga_que_sobrou(registro):
    from src.core.frete import FRETE_A_COLETAR
    from src.export.montar_entrega import escolher_cinco

    a_coletar = {**registro, "dominio": "a.com.br", "obs_frete": f"{FRETE_A_COLETAR}: x"}
    completa = {**registro, "dominio": "b.com.br", "valor_frete": 9.0, "obs_frete": "PAC"}
    escolhidos, _ = escolher_cinco([a_coletar, completa])
    assert [c["dominio"] for c in escolhidos] == ["b.com.br", "a.com.br"]


def test_correcoes_csv_ganha_da_validacao(tmp_path):
    """Decisão humana (site que exige cadastro) sobrevive à revalidação."""
    from src.runners.etapa1_validar import aplicar_correcoes

    correcoes = tmp_path / "correcoes.csv"
    correcoes.write_text(
        "dominio,status,motivo,decidido_em\n"
        "https://www.exemplo.com.br/,REPROVADO,preço só com login,09/10/2026\n"
        "outro.com.br,REPROVADO,não está no master,09/10/2026\n",
        encoding="utf-8",
    )
    forn = fornecedor("GO")
    assert aplicar_correcoes([forn], correcoes) == 1
    assert forn.status is Status.REPROVADO
    assert forn.motivo == "preço só com login (decisão manual 09/10/2026)"
