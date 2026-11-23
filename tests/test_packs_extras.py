import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vigilar as v  # noqa: E402

R = v.REGLAS_POR_DEFECTO


@pytest.mark.parametrize("titulo,esperado,regla", [
    # los dos packs reales de PcComponentes (noviembre 2026)
    ("Sony PlayStation 5 Slim Digital Chasis E + Samsung 990 PRO 1 TB SSD PCIe 4.0 NVMe Disipador Térmico",
     869.99, "PS5 Digital 599,99 € + SSD 1 TB 270,00 €"),
    ("Sony PlayStation 5 Slim Digital Chasis E + Samsung 990 PRO 1 TB SSD + GRAND THEFT AUTO VI STANDARD ED. "
     "(GTA VI) CIAB", 949.99, "PS5 Digital 599,99 € + SSD 1 TB 270,00 € + juego 80,00 €"),
    # título cortado en la tarjeta del listado
    ("Pack Sony PlayStation 5 Slim Digital Chasis E + Samsung 990 PRO 1 TB", 869.99, None),
    ("Consola PS5 Slim + WD_BLACK SN850P 2TB", 1189.99, "PS5 con lector 649,99 € + SSD 2 TB 540,00 €"),
    ("Consola PS5 Digital + SSD 500GB", 734.99, None),
    ("Sony PlayStation 5 Slim Digital + PlayStation Portal", 849.99, "PS5 Digital 599,99 € + PS Portal 250,00 €"),
    ("Sony PlayStation 5 Slim Chasis E + Auriculares Sony Pulse Elite", 749.99, None),
    ("Consola PS5 Digital + DualSense Edge", 839.99, None),
    ("Consola PS5 Digital + PS VR2", 999.99, None),
])
def test_packs_con_otras_cosas(titulo, esperado, regla):
    assert v.es_consola_ps5(titulo)
    precio, texto = v.precio_max_pack(titulo, R)
    assert precio == pytest.approx(esperado), texto
    if regla:
        assert texto == regla


@pytest.mark.parametrize("titulo", [
    "Sony PlayStation 5 Slim Digital Chasis E + TV Philips OLED 55OLED801/12 55\" 4K UltraHD 120Hz Smart TV",
    "Sony PlayStation 5 Slim Digital Chasis E + Philips QLED 75PUS7810 75\" Smart TV Titan OS",
])
def test_packs_con_tele_se_ignoran(titulo):
    assert v.precio_max_pack(titulo, R) is None


def test_valores_configurables():
    reglas = {**R, "valor_extras": {"ssd_por_tb": 200}}
    precio, texto = v.precio_max_pack("Consola PS5 Digital + Samsung 990 PRO 1 TB + GTA VI", reglas)
    assert precio == pytest.approx(599.99 + 200 + 80)  # lo que no cambias se queda por defecto
    assert "SSD 1 TB 200,00 €" in texto


def test_portal_o_vr_solos_no_son_consola():
    assert not v.es_consola_ps5("Sony PlayStation Portal Midnight Black para PS5")
    assert not v.es_consola_ps5("Pack PlayStation VR2 + Horizon para PS5")


# ---------- PcComponentes: grupo de variantes en el JSON-LD ----------
BASE = "https://www.pccomponentes.com/"
SSD = BASE + "sony-playstation-5-slim-digital-chasis-e-plus-samsung-990-pro-1-tb-ssd-pcie-4-0-nvme-disipador-termico"
GTA = BASE + "sony-playstation-5-slim-digital-chasis-e-plus-samsung-990-pro-1-tb-ssd-plus-grand-theft-auto-vi"


def grupo(variantes):
    return '<script type="application/ld+json">' + json.dumps({
        "@context": "https://schema.org", "@type": "ProductGroup", "name": "Sony PlayStation 5 Digital",
        "hasVariant": [{"@type": "Product", "name": n, "sku": s,
                        "offers": {"@type": "Offer", "url": u, "price": p,
                                   "availability": f"https://schema.org/{d}"}}
                       for n, s, u, p, d in variantes]}) + "</script>"


def test_solo_cuenta_la_variante_de_la_ficha():
    # tal cual lo sirve PcComponentes: la ficha está agotada pero la OTRA variante dice InStock
    html = grupo([("PS5 + SSD", "11218815", SSD, "869.98", "OutOfStock"),
                  ("PS5 + SSD + GTA VI", "11218818", GTA, "1017.65", "InStock")])
    prod = {"tienda": "PcComponentes", "nombre": "PS5 + SSD", "url": SSD, "precio_max": 869.99,
            "vendedor": "PcComponentes"}
    l = v.analizar(200, html, SSD, prod)
    assert l.estado == v.AGOTADO and l.precio == pytest.approx(869.98)


def test_variante_propia_en_stock():
    html = grupo([("PS5 + SSD", "11218815", SSD, "869.98", "InStock"),
                  ("PS5 + SSD + GTA VI", "11218818", GTA, "949.99", "OutOfStock")])
    prod = {"tienda": "PcComponentes", "nombre": "PS5 + SSD", "url": SSD + "?ref=x", "precio_max": 869.99}
    l = v.analizar(200, html, SSD, prod)
    assert l.estado == v.EN_STOCK and l.precio == pytest.approx(869.98)


def test_sin_url_en_variantes_se_usan_todas():
    html = grupo([("A", "1", None, "599.99", "OutOfStock"), ("B", "2", None, "649.99", "OutOfStock")])
    prod = {"tienda": "X", "nombre": "PS5", "url": "https://tienda.es/ps5", "precio_max": 599.99}
    assert v.analizar(200, html, "https://tienda.es/ps5", prod).estado == v.AGOTADO
