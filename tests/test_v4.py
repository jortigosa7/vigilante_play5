import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vigilar as v  # noqa: E402


def test_410_es_pagina_que_ya_no_existe_y_no_bloquea_el_navegador(monkeypatch):
    productos = [
        {"tienda": "MediaMarkt", "nombre": "Fortnite", "url": "https://mm.es/fortnite", "precio_max": 600},
        {"tienda": "MediaMarkt", "nombre": "Digital", "url": "https://mm.es/digital", "precio_max": 600},
    ]
    paginas = {"https://mm.es/fortnite": (410, "gone"), "https://mm.es/digital": (200, "<html>sin datos</html>")}
    monkeypatch.setattr(v, "descargar_http", lambda url, timeout=25: (*paginas[url], url))

    class NavFalso:
        def __init__(self):
            self.pedidas = []

        def arrancar(self):
            return True

        def descargar(self, url, timeout=45):
            self.pedidas.append(url)
            return 200, "<html></html>", "PS5 Digital Añadir a la cesta", url

    nav = NavFalso()
    res = v.revisar_todos(productos, nav, None)
    assert nav.pedidas == ["https://mm.es/digital"]            # a la que no existe ni se le intenta
    assert "ya no existe (HTTP 410)" in res[0][1].detalle
    assert res[1][1].estado == v.EN_STOCK
    assert "botón de compra" in res[1][1].detalle               # el aviso dirá que se compruebe


@pytest.mark.parametrize("sucio,limpio", [
    ("- 6 % Top Black Friday Pack Sony PlayStation 5 Slim Digital Chasis E + Samsung 990 PRO 1 TB 869,98€",
     "Pack Sony PlayStation 5 Slim Digital Chasis E + Samsung 990 PRO 1 TB"),
    ("PACK PlayStation 5 + Marvel Lobezno PS5 PACK PlayStation 5 + Marvel Lobezno PS5",
     "PACK PlayStation 5 + Marvel Lobezno PS5"),
    ("Consola PlayStation 5 Digital Consola PlayStation 5 Digital 599,99 € Agotado",
     "Consola PlayStation 5 Digital"),
    ("Recomendado Sony PlayStation 5 Slim Chasis E", "Sony PlayStation 5 Slim Chasis E"),
])
def test_limpiar_titulo_v4(sucio, limpio):
    assert v.limpiar_titulo(sucio) == limpio


def test_pack_con_ssd_sigue_siendo_digital_y_no_pro():
    t = v.limpiar_titulo("- 7 % Top Black Friday Pack Sony PlayStation 5 Slim Digital Chasis E + Samsung 990 PRO 1 TB")
    assert v.es_consola_ps5(t)
    precio, regla = v.precio_max_pack(t, v.REGLAS_POR_DEFECTO)
    assert precio == pytest.approx(869.99) and regla == "PS5 Digital 599,99 € + SSD 1 TB 270,00 €"


def test_unir_textos():
    assert v.unir_textos(["PS5 Digital", "", "Consola PS5 Digital 825GB", "ps5 digital"]) == "Consola PS5 Digital 825GB"
    assert v.unir_textos(["Ver", "Sony PlayStation 5 Slim"]) == "Ver Sony PlayStation 5 Slim"


def test_error_telegram_explicado():
    cuerpo = io.BytesIO(json.dumps({"ok": False, "description": "Forbidden: bot can't initiate conversation with a user"}).encode())
    e = urllib.error.HTTPError("https://api.telegram.org", 403, "Forbidden", {}, cuerpo)
    txt = v.explicar_error_telegram(e)
    assert "403" in txt and "Iniciar" in txt and "can't initiate" in txt


def test_listado_con_pocos_resultados_se_completa_con_navegador(monkeypatch):
    url = "https://www.tienda.es/consolas-ps5"
    html_http = '<a href="/ps5-a">Consola PlayStation 5 Digital 825GB</a>'
    monkeypatch.setattr(v, "descargar_http", lambda u, timeout=25: (200, html_http, u))

    class NavFalso:
        def arrancar(self):
            return True

        def descargar_enlaces(self, u, timeout=45):
            enlaces = [("https://www.tienda.es/ps5-a", "Consola PlayStation 5 Digital 825GB"),
                       ("https://www.tienda.es/ps5-b", "Consola PlayStation 5 Estándar 1TB + Astro Bot"),
                       ("https://www.tienda.es/mando", "Mando DualSense")]
            return 200, "<html></html>", u, enlaces

    res = v.descubrir_listados([{"tienda": "T", "url": url}], NavFalso())
    items, detalle = res[url]
    assert [u for u, _ in items] == ["https://www.tienda.es/ps5-a", "https://www.tienda.es/ps5-b"]
    assert detalle == ""


def test_listado_con_suficientes_no_abre_navegador(monkeypatch):
    url = "https://www.tienda.es/consolas-ps5"
    html_http = "".join(f'<a href="/ps5-{i}">Consola PlayStation 5 Digital {i}</a>' for i in range(6))
    monkeypatch.setattr(v, "descargar_http", lambda u, timeout=25: (200, html_http, u))

    class NavQueNoDebeUsarse:
        def arrancar(self):
            raise AssertionError("no debería abrir el navegador")

    items, _ = v.descubrir_listados([{"tienda": "T", "url": url}], NavQueNoDebeUsarse())[url]
    assert len(items) == 6
