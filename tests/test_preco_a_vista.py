"""Preço de PIX, boleto ou à vista não é o preço do produto: vale o do cartão.

Os textos são os das fichas reais, como o navegador devolve.
"""

import pytest

from src.core.pagina_produto import preco_sem_desconto_de_pagamento, produto_da_ficha

URL = "https://loja.com.br/produto"


@pytest.mark.parametrize("preco, texto, esperado", [
    # Dutra: "Por" já com o desconto do PIX; o cartão vem embaixo.
    (177.56, "De R$ 219,88\n15% OFF\neconomize R$ 9,34\nPor R$ 177,56\n"
             "DESCONTO DE 5% no Boleto ou PIX (já aplicado)\nR$ 186,90\n"
             "Em até 3x de R$ 62,30 sem juros no cartão de crédito", 186.90),
    # Multiseg: só a parcela, sem "R$" e com ponto decimal.
    (80.61, "R$ 80,61\nÀ VISTA\n3% DE DESCONTO\n2x de 41.55 s/juros no cartão", 83.10),
    # Amaster EPI
    (17.10, "R$ 17,10 à vista\n1 X de R$ 18,00 no cartão de crédito", 18.00),
    # PreveOeste
    (12.25, "R$ 12,25\nno PIX\nou R$ 12,90 em 2 x R$ 6,45 sem juros", 12.90),
    # Casa dos Químicos
    (64.90, "R$ 64,90\nà vista no Pix ou Boleto\nou R$ 66,91 sem juros no cartão", 66.91),
    # Plásticos Ipiranga: o JSON-LD traz o à vista.
    (23.28, "por R$ 24,50\naté 5x de R$ 5,54\nMais formas de pagamento\n"
            "R$ 23,28 à vista\nEconomize: R$ 1,22", 24.50),
    # Casa dos Parafusos
    (827.23, "R$ 827,23\nà vista com 10% de desconto\nou R$ 919,20 em 12x de R$ 76,60 sem juros",
     919.20),
    # NC Bella: o preço escrito ganha da parcela vezes 6 (162,96).
    (154.85, "R$ 163,00\naté 6x de R$ 27,16 sem juros\nR$ 154,85 no pix com 5% de desconto\n"
             "1x de R$ 163,00 sem juros\n6x de R$ 27,16 sem juros", 163.00),
    # Supriflex: idem (3 x 8,22 = 24,66).
    (22.21, "R$ 24,68\naté 3x de R$ 8,22 sem juros\nou R$ 22,21 via Pix", 24.68),
])
def test_preco_a_vista_vira_preco_do_cartao(preco, texto, esperado):
    assert preco_sem_desconto_de_pagamento(preco, texto) == esperado


@pytest.mark.parametrize("preco, texto", [
    # Gimba: o único preço da ficha; é o mesmo do card da busca.
    (19.99, "Por apenas\nR$ 19,99\nno PIX (3% desconto)\nLeve Mais Pague Menos\n"
            "2 ou +\nR$ 17,57 cada\nAdicionar\nFormas de Pagamento"),
    # TR EPIs: o lido já é o normal; o à vista vem depois.
    (18.38, "R$ 18,38\nR$ 17,46 à vista com desconto\n% produto com desconto progressivo"),
    # Loja que parcela com juros: a parcela não é preço.
    (101.99, "R$ 101,99\nà vista no PIX\n2x de R$ 51,99 com juros*"),
    # Preço normal seguido do parcelamento.
    (35.44, "R$ 31,90\nJá com 10% OFF de desconto à vista no Pix\nR$ 35,44\n"
            "Em até 3x de R$ 11,81 sem juros no cartão"),
])
def test_preco_normal_fica(preco, texto):
    assert preco_sem_desconto_de_pagamento(preco, texto) == preco


def test_ficha_com_jsonld_a_vista_sai_com_o_preco_do_cartao():
    ficha = {
        "jsonld": [{"@type": "Product", "name": "Balde",
                    "offers": {"price": "23.28", "availability": "InStock"}}],
        "titulo": "Balde com Tampa 5 Litros",
        "texto_area": "Balde com Tampa 5 Litros\npor R$ 24,50\naté 5x de R$ 5,54\n"
                      "R$ 23,28 à vista\nEconomize: R$ 1,22\nComprar",
    }
    assert produto_da_ficha(ficha, URL).preco == 24.50


def test_dutra_preco_do_cartao_e_riscado_continua():
    ficha = {
        "jsonld": [],
        "titulo": "Protetor solar 1 litro",
        "texto_area": "Protetor solar\nDe R$ 219,88\n15% OFF\neconomize R$ 9,34\n"
                      "Por R$ 177,56\nDESCONTO DE 5% no Boleto ou PIX (já aplicado)\n"
                      "R$ 186,90\nEm até 3x de R$ 62,30 sem juros no cartão de crédito\nCOMPRAR",
    }
    p = produto_da_ficha(ficha, URL)
    assert p.preco == 186.90
    assert p.preco_lista == 219.88


def test_gimba_jsonld_com_preco_cheio_e_tela_de_por():
    """JSON-LD traz 19,90; a tela mostra "de R$ 19,90 / Por apenas R$ 17,59"."""
    ficha = {
        "jsonld": [{"@type": "Product", "name": "Álcool em Gel Edumax 800ml",
                    "offers": {"price": "19.90", "availability": "https://schema.org/InStock"}}],
        "titulo": "Álcool em Gel Edumax 70% 800ml 1 UN",
        "preco_atual": "de\nR$ 19,90\nPor apenas\nR$ 17,59\nno PIX (3% desconto)",
        "texto_area": "Disponível\nde\nR$ 19,90\nPor apenas\nR$ 17,59\nno PIX (3% desconto)\n"
                      "Formas de Pagamento\nAdicionar à Caixa",
    }
    p = produto_da_ficha(ficha, URL)
    assert p.preco == 17.59
    assert p.preco_lista == 19.90


def test_variacao_escolhida_vale_o_preco_da_tela():
    """Casa do Saco de Lixo: o JSON-LD é do produto pai; o print é da variação."""
    ficha = {
        "jsonld": [{"@type": "Product", "name": "Dispenser",
                    "offers": {"price": "60.90", "availability": "InStock"}}],
        "titulo": "Dispenser Para Papel Higiênico Rolão 300m",
        "preco_atual": "R$ 37,90",
        "texto_area": "Dispenser\nPapel Higiênico Cai Cai\nR$ 37,90\nComprar",
        "variacao_escolhida": True,
    }
    assert produto_da_ficha(ficha, URL).preco == 37.90
    ficha["variacao_escolhida"] = False
    assert produto_da_ficha(ficha, URL).preco == 60.90
