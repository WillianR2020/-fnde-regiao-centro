"""Leitura de preço e frete na página do produto. Nenhum teste sobe navegador."""

import pytest

from src.core.frete import houve_recusa
from src.core.http import RespostaInvalida
from src.core.pagina_produto import frete_do_texto, produto_da_ficha

URL = "https://loja.com.br/product/agua-sanitaria-5l/"


def test_jsonld_ganha_do_texto():
    ficha = {
        "jsonld": [{"@graph": [
            {"@type": "WebPage"},
            {"@type": "Product", "name": "Água Sanitária 5L",
             "offers": [{"@type": "Offer", "price": "10.67",
                         "availability": "https://schema.org/InStock"}]},
        ]}],
        "titulo": "Água Sanitária – 5L",
        "texto_area": "R$ 99,90",
    }
    p = produto_da_ficha(ficha, URL)
    assert p.preco == 10.67
    assert p.disponivel is True
    assert p.titulo == "Água Sanitária – 5L"


def test_fora_de_estoque_pelo_jsonld():
    ficha = {"jsonld": [{"@type": "Product", "offers": {
        "price": 10.67, "availability": "http://schema.org/OutOfStock"}}]}
    assert produto_da_ficha(ficha, URL).disponivel is False


def test_sem_jsonld_le_o_texto_e_o_riscado():
    ficha = {
        "jsonld": [],
        "titulo": "Desinfetante 5 L",
        "preco_riscado": "R$ 1.234,56",
        "preco_atual": "R$ 1.100,00",
        "texto_area": "Desinfetante 5 L\nFora de estoque",
    }
    p = produto_da_ficha(ficha, URL)
    assert p.preco == 1100.0
    assert p.preco_lista == 1234.56
    assert p.disponivel is False


def test_de_por_sem_del_nao_vira_preco():
    """PreveOeste: promoção sem <del>, só "De R$ 56,80" antes do preço."""
    ficha = {
        "jsonld": [{"@type": "Product", "name": "Rodo"}],  # sem offers
        "titulo": "Rodo de espuma",
        "preco_atual": "De R$ 56,80\nR$ 44,55\nou R$ 46,90 em 6 x R$ 7,82 sem juros",
        "texto_area": "Rodo de espuma\nDe R$ 56,80\nR$ 44,55\nComprar",
    }
    p = produto_da_ficha(ficha, URL)
    assert p.preco == 44.55
    assert p.preco_lista == 56.80


def test_de_economize_por_dutra():
    """Dutra: o bloco de preço cola o valor cheio e o do PIX num texto só."""
    ficha = {
        "jsonld": [],
        "titulo": "Rodo plástico 40 cm",
        "preco_atual": "De R$ 23,41\n15% OFF\neconomize R$ 1,99\nPor R$ 17.9117,91",
        "texto_area": "Rodo plástico 40 cm\nDe R$ 23,41\n15%\nOFF\neconomize R$ 1,99\n"
                      "Por\xa0\xa0\xa0R$ 17,91\xa0\nDESCONTO DE 10%\nCOMPRAR",
    }
    p = produto_da_ficha(ficha, URL)
    assert p.preco == 17.91
    assert p.preco_lista == 23.41


def test_a_partir_de_continua_sendo_preco():
    ficha = {"jsonld": [], "titulo": "Balde", "preco_atual": "A partir de R$ 12,90",
             "texto_area": "Balde\nComprar"}
    assert produto_da_ficha(ficha, URL).preco == 12.90


def test_pagina_sem_preco_levanta():
    with pytest.raises(RespostaInvalida):
        produto_da_ficha({"jsonld": [], "titulo": "x", "texto_area": "Consulte"}, URL)


def test_frete_conta_so_o_que_apareceu_depois_do_cep():
    antes = "Água Sanitária\nR$ 10,67\nFrete grátis acima de R$ 299,00"
    depois = antes + "\nSEDEX\nR$ 45,20\nPAC\nR$ 32,10"
    valor, obs = frete_do_texto(antes, depois)
    assert valor == 32.10
    assert "PAC" in obs


def test_frete_gratis():
    valor, obs = frete_do_texto("Produto", "Produto\nEntrega expressa: Grátis")
    assert valor == 0.0
    assert obs.startswith("frete gratis")


def test_recusa_vira_marcador_de_recusa():
    valor, obs = frete_do_texto("Produto", "Produto\nNão entregamos neste CEP")
    assert valor is None
    assert houve_recusa(obs)


def test_nada_novo_nao_e_recusa():
    valor, obs = frete_do_texto("Produto\nR$ 10,67", "Produto\nR$ 10,67")
    assert (valor, obs) == (None, "")


def test_preco_zero_e_ausencia_de_preco():
    """Carrinho vazio do cabeçalho ("R$ 0,00") não é preço do produto."""
    with pytest.raises(RespostaInvalida):
        produto_da_ficha({"jsonld": [], "preco_atual": "R$ 0,00",
                          "texto_area": "Carrinho R$ 0,00"}, URL)


def test_filtro_de_preco_nao_e_frete():
    """Filtro lateral carregado depois do CEP virava "frete grátis"."""
    valor, obs = frete_do_texto(
        "Produto", "Produto\nFILTRAR POR PREÇO de R$ 0,00 até R$ 24,99 (462)")
    assert (valor, obs) == (None, "")


def test_so_retirada_e_recusa():
    """Retirar no balcão não é entrega em GO/DF."""
    valor, obs = frete_do_texto(
        "Produto", "Produto\nR$ 0,00 1 dia útil Retirar pessoalmente")
    assert valor is None
    assert houve_recusa(obs)


def test_retirada_nao_ganha_da_entrega():
    valor, _ = frete_do_texto(
        "Produto", "Produto\nRetirar na loja R$ 0,00\nPAC R$ 41,30 7 dias úteis")
    assert valor == 41.30


def test_frase_de_recusa_da_loja_integrada():
    _, obs = frete_do_texto(
        "Produto", "Produto\nNão foram encontradas formas de envio para o CEP informado.")
    assert houve_recusa(obs)


def test_preco_com_login_ou_cadastro_e_recusado():
    """Regra de 09/10/2026: site que exige cadastro para mostrar preço é ignorado."""
    ficha = {"jsonld": [], "titulo": "ÁGUA SANITÁRIA YPÊ 5LT",
              "texto_area": "ÁGUA SANITÁRIA YPÊ 5LT\nCódigo: 40082\nFaça seu login ou\n"
                            "cadastre-se para ver preços e comprar"}
    with pytest.raises(RespostaInvalida, match="login"):
        produto_da_ficha(ficha, URL)


@pytest.mark.parametrize("texto, esperado", [
    ("Faça seu login ou\ncadastre-se para ver preços e comprar", True),
    ("Entre para ver o preço", True),
    ("Cadastre-se e veja o preço", True),
    ("Acesse sua conta para consultar preço", True),
    ("Entrar | Cadastrar | Frete grátis", False),
    ("Água Sanitária 5L R$ 12,90 Comprar", False),
])
def test_reconhece_preco_escondido_atras_de_cadastro(texto, esperado):
    from src.core.pagina_produto import preco_exige_cadastro

    assert preco_exige_cadastro(texto) is esperado


def test_valor_em_linha_separada_do_nome_do_envio():
    """Gimba: "Envio:" / "Transportadora" / "Valor:" / "R$ 77,36"."""
    depois = ("Produto\nFrete para CEP\n74933-130\nEnvio:\nTransportadora\nValor:\nR$ 77,36\n"
              "Prazo:\nEntrega em até 7 dia(s) úteis após liberação do pedido!")
    valor, obs = frete_do_texto("Produto", depois)
    assert valor == 77.36
    assert "Transportadora" in obs


def test_valor_puro_nao_herda_rotulo_de_outra_opcao():
    valor, _ = frete_do_texto(
        "Produto", "Produto\nRetirar na loja\nR$ 0,00\nPAC\nR$ 41,30")
    assert valor == 41.30
