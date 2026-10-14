"""Busca da Store API do WooCommerce."""

from src.adapters.woocommerce import palavra_de_recuo


def test_recuo_e_a_palavra_mais_longa():
    """WordPress compara acento: 'lavadora pressao' não acha 'Pressão'."""
    assert palavra_de_recuo("lavadora pressao") == "lavadora"
    assert palavra_de_recuo("aspirador po 1.600") == "aspirador"


def test_sem_recuo_para_uma_palavra_ou_palavra_curta():
    assert palavra_de_recuo("lavadora") == ""
    assert palavra_de_recuo("pa cabo") == ""
