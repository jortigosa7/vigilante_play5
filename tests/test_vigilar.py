import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vigilar as v  # noqa: E402

PROD = {"tienda": "MediaMarkt", "nombre": "PS5 Digital", "url": "https://x/ps5", "precio_max": 599.99,
        "vendedor": "MediaMarkt"}


def ld(obj):
    return f'<html><head><script type="application/ld+json">{json.dumps(obj)}</script></head><body></body></html>'


def oferta(disp, precio, vendedor="MediaMarkt", cond=None):
    o = {"@type": "Offer", "availability": f"https://schema.org/{disp}", "price": precio,
         "priceCurrency": "EUR", "seller": {"@type": "Organization", "name": vendedor}}
    if cond:
        o["itemCondition"] = f"https://schema.org/{cond}"
    return o


# ---------- precios ----------
@pytest.mark.parametrize("entrada,esperado", [
    ("599,00", 599.0), ("599.00", 599.0), ("1.336,32", 1336.32), ("1,336.32", 1336.32),
    (599, 599.0), ("599 €", 599.0), ("1.336", 1336.0), ("", None), (None, None), ("gratis", None),
])
def test_a_precio(entrada, esperado):
    assert v.a_precio(entrada) == esperado


def test_euros():
    assert v.euros(1336.32) == "1.336,32 €"
    assert v.euros(None) == "precio no detectado"


# ---------- JSON-LD ----------
def test_en_stock_simple():
    l = v.analizar(200, ld({"@context": "https://schema.org", "@type": "Product", "name": "PS5",
                            "offers": oferta("InStock", "599.00")}), "https://x", PROD)
    assert (l.estado, l.precio, l.vendedor) == (v.EN_STOCK, 599.0, "MediaMarkt")


def test_agotado():
    l = v.analizar(200, ld({"@type": "Product", "offers": oferta("OutOfStock", "599.00")}), "", PROD)
    assert l.estado == v.AGOTADO and l.precio == 599.0


def test_graph_y_aggregate_offer():
    doc = {"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": []},
        {"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": "599,00",
                                        "availability": "http://schema.org/InStock"}}]}
    l = v.analizar(200, ld(doc), "", {**PROD, "vendedor": ""})
    assert l.estado == v.EN_STOCK and l.precio == 599.0


def test_product_group_con_variantes():
    doc = {"@type": "ProductGroup", "hasVariant": [
        {"@type": "Product", "offers": oferta("OutOfStock", 649)},
        {"@type": "Product", "offers": oferta("InStock", 599)}]}
    l = v.analizar(200, ld(doc), "", PROD)
    assert l.estado == v.EN_STOCK and l.precio == 599.0


def test_ignora_productos_relacionados():
    html = (ld({"@type": "Product", "offers": oferta("OutOfStock", 599)}) +
            ld({"@type": "ItemList", "itemListElement": [
                {"@type": "ListItem", "item": {"@type": "Product", "offers": oferta("InStock", 59.99)}}]}))
    assert v.analizar(200, html, "", PROD).estado == v.AGOTADO


def test_filtra_marketplace_y_reacondicionados():
    doc = {"@type": "Product", "offers": [
        oferta("InStock", 550, vendedor="asgoodasnew electronics GmbH"),
        oferta("InStock", 499, cond="RefurbishedCondition"),
        oferta("OutOfStock", 599, vendedor="MediaMarkt")]}
    l = v.analizar(200, ld(doc), "", PROD)
    assert l.estado == v.AGOTADO


def test_solo_marketplace_cuenta_como_agotado():
    doc = {"@type": "Product", "offers": [oferta("InStock", 1336.32, vendedor="Office-Partner ES")]}
    l = v.analizar(200, ld(doc), "", PROD)
    assert l.estado == v.AGOTADO and "externos" in l.detalle


def test_solo_tienda_y_reserva_cuentan_como_stock():
    l = v.analizar(200, ld({"@type": "Product", "offers": oferta("InStoreOnly", 499)}), "", PROD)
    assert l.estado == v.EN_STOCK and "tienda" in l.detalle
    l = v.analizar(200, ld({"@type": "Product", "offers": oferta("PreOrder", 599)}), "", PROD)
    assert l.estado == v.EN_STOCK and "reserva" in l.detalle


def test_mainentity_y_json_con_entidades_html():
    crudo = json.dumps({"@type": "WebPage", "mainEntity": {"@type": "Product",
                                                            "offers": oferta("InStock", "599.00")}})
    html = f'<script type="application/ld+json">{crudo.replace(chr(34), "&quot;")}</script>'
    assert v.analizar(200, html, "", PROD).estado == v.EN_STOCK


# ---------- meta ----------
def test_meta_itemprop():
    html = ('<meta content="599.00" itemprop="price"><link itemprop="availability" '
            'href="https://schema.org/InStock">')
    l = v.analizar(200, html, "", PROD)
    assert l.estado == v.EN_STOCK and l.precio == 599.0


def test_meta_og():
    html = '<meta property="product:availability" content="out of stock">'
    assert v.analizar(200, html, "", PROD).estado == v.AGOTADO


# ---------- texto visible (navegador) ----------
SONY = {"tienda": "Sony", "nombre": "PS5 Digital", "url": "https://sony/ps5", "precio_max": 599.99,
        "modo": "navegador", "fuente": "texto"}


def test_texto_boton_compra():
    l = v.analizar(200, "<html></html>", "https://sony", SONY, "PS5 Digital 599,99 € Añadir al carrito", "navegador")
    assert l.estado == v.EN_STOCK


def test_texto_agotado():
    l = v.analizar(200, "<html></html>", "https://sony", SONY, "PS5 Digital No disponible actualmente", "navegador")
    assert l.estado == v.AGOTADO


def test_texto_contradictorio_no_avisa():
    l = v.analizar(200, "<html></html>", "", SONY, "Añadir al carrito ... Agotado", "navegador")
    assert l.estado == v.DESCONOCIDO


def test_texto_personalizado():
    prod = {**SONY, "texto_en_stock": "Comprar ya", "texto_agotado": ["Sin unidades"]}
    assert v.analizar(200, "", "", prod, "Comprar ya", "navegador").estado == v.EN_STOCK
    assert v.analizar(200, "", "", prod, "Sin unidades", "navegador").estado == v.AGOTADO


def test_entrega_no_disponible_no_es_agotado():
    # MediaMarkt pone "La entrega a domicilio no está disponible" en productos de recogida en tienda
    l = v.leer_texto("La entrega a domicilio no está disponible. Añadir a la cesta", SONY)
    assert l.estado == v.EN_STOCK


def test_precio_de_jsonld_se_usa_con_texto():
    html = ld({"@type": "Product", "offers": {"@type": "Offer", "price": "599.99"}})
    l = v.analizar(200, html, "", {**SONY, "fuente": "auto"}, "Añadir al carrito", "navegador")
    assert l.estado == v.EN_STOCK and l.precio == 599.99


# ---------- cola y bloqueos ----------
def test_cola_queue_it():
    l = v.analizar(200, "<html>x</html>", "https://sony.queue-it.net/?c=sony", SONY, "", "navegador")
    assert l.estado == v.COLA


def test_script_queue_it_sin_cola_no_es_cola():
    html = '<script src="https://static.queue-it.net/script/queueclient.min.js"></script>'
    assert v.analizar(200, html, "https://sony", SONY, "Añadir al carrito", "navegador").estado == v.EN_STOCK


def test_bloqueo_cloudflare():
    l = v.analizar(403, "<title>Just a moment...</title>", "", PROD)
    assert l.estado == v.ERROR and "bloquea" in l.detalle


def test_bloqueo_no_pisa_datos_validos():
    html = ld({"@type": "Product", "offers": oferta("InStock", 599)}) + "<script src='/cdn-cgi/challenge-platform/x'></script>"
    assert v.analizar(200, html, "", PROD).estado == v.EN_STOCK


def test_404():
    l = v.analizar(404, "not found", "", PROD)
    assert l.estado == v.ERROR and "404" in l.detalle


def test_pagina_sin_datos():
    assert v.analizar(200, "<html><body>hola</body></html>", "", PROD).estado == v.DESCONOCIDO


# ---------- avisos y estado ----------
class Buzon:
    def __init__(self):
        self.mensajes = []

    def __call__(self, texto, titulo="", url=None, urgente=False):
        self.mensajes.append((titulo, texto, urgente))
        return True


def test_transiciones():
    estado, buzon = {}, Buzon()
    v.procesar_lectura(PROD, v.Lectura(v.AGOTADO, 599), estado, buzon)
    assert buzon.mensajes == []
    v.procesar_lectura(PROD, v.Lectura(v.EN_STOCK, 599), estado, buzon)
    assert len(buzon.mensajes) == 1 and buzon.mensajes[0][2] is True
    assert "599,00 €" in buzon.mensajes[0][1] and PROD["url"] in buzon.mensajes[0][1]
    v.procesar_lectura(PROD, v.Lectura(v.EN_STOCK, 599), estado, buzon)      # sigue en stock: no repite
    v.procesar_lectura(PROD, v.Lectura(v.ERROR, detalle="x"), estado, buzon)  # un fallo no cambia nada
    v.procesar_lectura(PROD, v.Lectura(v.EN_STOCK, 599), estado, buzon)
    assert len(buzon.mensajes) == 1
    v.procesar_lectura(PROD, v.Lectura(v.AGOTADO), estado, buzon)            # se acaba: te lo dice
    assert len(buzon.mensajes) == 2 and "Se ha vuelto a agotar" in buzon.mensajes[1][1]
    assert buzon.mensajes[1][2] is False and "min)" in buzon.mensajes[1][1]
    v.procesar_lectura(PROD, v.Lectura(v.AGOTADO), estado, buzon)            # sigue agotada: nada
    v.procesar_lectura(PROD, v.Lectura(v.EN_STOCK, 589), estado, buzon)      # vuelve a entrar: avisa
    assert len(buzon.mensajes) == 3 and "recarga" in buzon.mensajes[2][1]


def test_revendedor_por_encima_del_maximo_no_avisa():
    estado, buzon = {}, Buzon()
    v.procesar_lectura(PROD, v.Lectura(v.EN_STOCK, 710.53), estado, buzon)
    assert buzon.mensajes == []
    v.procesar_lectura(PROD, v.Lectura(v.EN_STOCK, 599.99), estado, buzon)
    assert len(buzon.mensajes) == 1


def test_stock_sin_precio_avisa_con_advertencia():
    estado, buzon = {}, Buzon()
    v.procesar_lectura(SONY, v.Lectura(v.EN_STOCK, None), estado, buzon)
    assert len(buzon.mensajes) == 1 and "compruébalo" in buzon.mensajes[0][1]


def test_aviso_de_fallos_una_sola_vez():
    estado, buzon = {}, Buzon()
    for _ in range(v.UMBRAL_FALLOS + 5):
        v.procesar_lectura(PROD, v.Lectura(v.ERROR, detalle="la tienda bloquea"), estado, buzon)
    assert len(buzon.mensajes) == 1 and "NO está vigilada" in buzon.mensajes[0][1]
    v.procesar_lectura(PROD, v.Lectura(v.AGOTADO), estado, buzon)  # se recupera
    assert estado[PROD["url"]]["fallos"] == 0


def test_cola_avisa():
    estado, buzon = {}, Buzon()
    v.procesar_lectura(SONY, v.Lectura(v.COLA), estado, buzon)
    v.procesar_lectura(SONY, v.Lectura(v.COLA), estado, buzon)
    assert len(buzon.mensajes) == 1 and "cola virtual" in buzon.mensajes[0][1]


# ---------- pasada completa con descargas simuladas ----------
def test_pasada_completa(tmp_path, monkeypatch):
    productos = {"productos": [
        {"tienda": "PcComponentes", "nombre": "PS5 Digital", "url": "https://pcc/1", "precio_max": 599.99,
         "vendedor": "PcComponentes"},
        {"tienda": "MediaMarkt", "nombre": "PS5 Digital", "url": "https://mm/1", "precio_max": 599.99,
         "vendedor": "MediaMarkt"},
        {"tienda": "Carrefour", "nombre": "PS5", "url": "PEGA AQUÍ", "activo": False},
        {"tienda": "Carrefour", "nombre": "PS5 sin url", "url": "PEGA AQUÍ", "activo": True},
    ]}
    (tmp_path / "productos.json").write_text(json.dumps(productos), encoding="utf-8")
    monkeypatch.setattr(v, "RUTA_PRODUCTOS", tmp_path / "productos.json")
    monkeypatch.setattr(v, "RUTA_ESTADO", tmp_path / "estado.json")
    resumen_md = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(resumen_md))

    paginas = {
        "https://pcc/1": (200, ld({"@type": "Product", "offers": oferta("OutOfStock", 599, "PcComponentes")}), "https://pcc/1"),
        "https://mm/1": (403, "<title>Access Denied</title>", "https://mm/1"),
    }
    monkeypatch.setattr(v, "descargar_http", lambda url, timeout=25: paginas[url])
    buzon = Buzon()
    args = SimpleNamespace(probar=False, resumen=False)

    res = v.pasada(args, None, avisar=buzon, pausa=None)
    assert [l.estado for _, l in res] == [v.AGOTADO, v.ERROR]
    assert len(buzon.mensajes) == 1 and "resumen" in buzon.mensajes[0][0]  # primera vez: resumen
    assert "Leyendo bien 1 de 2" in buzon.mensajes[0][1]
    assert (tmp_path / "estado.json").exists() and "MediaMarkt" in resumen_md.read_text(encoding="utf-8")

    # reposición en PcComponentes
    paginas["https://pcc/1"] = (200, ld({"@type": "Product", "offers": oferta("InStock", "599,00", "PcComponentes")}), "https://pcc/1")
    v.pasada(args, None, avisar=buzon, pausa=None)
    assert len(buzon.mensajes) == 2 and "EN STOCK en PcComponentes" in buzon.mensajes[1][1]

    # modo prueba: no avisa ni toca el estado
    antes = (tmp_path / "estado.json").read_text(encoding="utf-8")
    v.pasada(SimpleNamespace(probar=True, resumen=False), None, avisar=buzon, pausa=None)
    assert len(buzon.mensajes) == 2 and (tmp_path / "estado.json").read_text(encoding="utf-8") == antes


def test_ntfy_y_telegram(monkeypatch):
    llamadas = []

    def falso_urlopen(peticion, data=None, timeout=None):
        llamadas.append(peticion if isinstance(peticion, str) else (peticion.full_url, peticion.data))
        return None

    monkeypatch.setattr(v.urllib.request, "urlopen", falso_urlopen)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("NTFY_TOPIC", "ps5-prueba")
    assert v.enviar_aviso("🟢 PS5 en stock ñ", titulo="Título con tildes ñ", url="https://x", urgente=True)
    assert llamadas[0] == "https://api.telegram.org/bot123:ABC/sendMessage"
    url, cuerpo = llamadas[1]
    datos = json.loads(cuerpo)
    assert url == "https://ntfy.sh" and datos["topic"] == "ps5-prueba" and datos["priority"] == 5
    assert datos["click"] == "https://x" and "ñ" in datos["title"]


def test_productos_json_real_es_valido():
    datos = json.loads((Path(__file__).resolve().parents[1] / "productos.json").read_text(encoding="utf-8"))
    activos = [p for p in datos["productos"] if p.get("activo", True)]
    assert len(activos) >= 10
    for p in activos:
        assert p["url"].startswith("https://") and p["precio_max"] > 500
