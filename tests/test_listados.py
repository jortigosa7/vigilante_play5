import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vigilar as v  # noqa: E402

REGLAS = dict(v.REGLAS_POR_DEFECTO)


# ---------- qué cuenta como consola ----------
@pytest.mark.parametrize("titulo", [
    "Sony PlayStation 5 Slim Digital Chasis E",
    "Sony PlayStation 5 Slim Chasis E + Marvels: Lobezno + Marvels: Spider-Man 2",
    "Consola - Sony PS5 Slim Digital Edition, 825 GB SSD, 4K, 2 mandos DualSense, Chasis E, Blanco",
    "Consola PlayStation 5 Digital 825GB + 2º Mando DualSense Blanco",
    "Consola PlayStation®5 edición digital",
    "PACK PlayStation 5 + Marvel Lobezno PS5",
    "PlayStation 5 digital Edición Limitada Marvel: Lobezno Amarillo de combate",
    "Sony PlayStation 5 Slim Digital Chasis E + Sony Tarjeta Prepago 20€ para PlayStation Store",
    "Consola PS5 Slim Digital 1TB Chassis D + 2 Dualsense",
    "Consola PS5 Edición Digital (825 GB) + Juego PS5 God Of War Ragnarök (Formato Digital)",
    "Consola PlayStation 5 Pro V2 2TB",
])
def test_si_es_consola(titulo):
    assert v.es_consola_ps5(titulo), titulo


@pytest.mark.parametrize("titulo", [
    "Mando PS5 Sony DualSense V2 Blanco inalámbrico",
    "Sony DualSense Edge Mando Inalámbrico para PS5 Midnight Black",
    "Marvel's Spider-Man 2 PlayStation 5",
    "EA Sports FC27 PlayStation 5",
    "GRAND THEFT AUTO VI STANDARD ED. (GTA VI) CIAB PS5",
    "Soporte vertical para consola PS5 Slim",
    "Lector de discos para consolas PS5 Digital Edition",
    "Cubiertas para consola PS5 Slim Cobalt Blue",
    "WD_BLACK SN850P 1TB SSD para consolas PS5",
    "Disco SSD Samsung 990 Pro para PlayStation 5",
    "Dispositivo de uso a distancia Playstation Portal Remote para PS5 - Blanco",
    "Gafas de Realidad Virtual PlayStation VR2",
    "REACONDICIONADO Seminuevo Muy bueno Consola - Sony PS5 Slim Digital Edition",
    "Consolas PS5",
    "Juegos PS5",
    "Auriculares Sony Pulse Elite para PS5",
])
def test_no_es_consola(titulo):
    assert not v.es_consola_ps5(titulo), titulo


# ---------- precio máximo de packs ----------
@pytest.mark.parametrize("titulo,esperado", [
    ("Sony PlayStation 5 Slim Digital Chasis E", 599.99),
    ("Sony PlayStation 5 Slim Chasis E", 649.99),
    ("Sony PlayStation 5 Slim Chasis E + Marvels: Lobezno + Marvels: Spider-Man 2", 809.99),
    ("Sony PlayStation 5 Slim Digital Chasis E + 2x Mandos DualSense V3", 674.99),
    ("Consola - Sony PS5 Slim Digital Edition, 825 GB SSD, 4K, 2 mandos DualSense, Chasis E, Blanco", 674.99),
    ("Consola - Sony PS5 Slim Digital Edition, 825 GB SSD, 4K, 1 mando, Chasis E, Blanco + Pack Fortnite "
     "Flowering Chaos (código de descarga)", 599.99),
    ("Sony PlayStation 5 Slim Digital Chasis E + Sony Tarjeta Prepago 20€ para PlayStation Store", 619.99),
    ("Consola PlayStation 5 Digital 825GB con EA Sports FC 26, Blanca", 679.99),
    ("Consola PlayStation 5 Digital 825GB con Fortnite Flowering Chaos", 599.99),
    ("Sony PlayStation 5 Slim Digital Bundle Battle Yellow Marvel's Wolverine Edición Limitada", 679.99),
    ("PACK PlayStation 5 + Marvel Lobezno PS5", 729.99),
    ("Consola PS5 Slim Digital 1TB Chassis D + 2 Dualsense", 674.99),
    ("Consola PlayStation 5 Estándar 1TB E + Marvel's Spider Man 2 + Astro Bot", 809.99),
    ("Consola PlayStation 5 Digital 825GB + 2º Mando DualSense Blanco", 674.99),
    ("Consola - Sony PS5 Slim Digital Edition, 1 TB SSD, 4K, 1 mando, Chasis D, Blanco + Código de descarga "
     "de juego Astro Bot", 679.99),
])
def test_precio_max_pack(titulo, esperado):
    precio, regla = v.precio_max_pack(titulo, REGLAS)
    assert precio == pytest.approx(esperado), (titulo, regla)


def test_pro_se_ignora_salvo_que_lo_pidas():
    assert v.precio_max_pack("Consola PlayStation 5 Pro V2 2TB", REGLAS) is None
    precio, _ = v.precio_max_pack("Consola PlayStation 5 Pro V2 2TB", {**REGLAS, "incluir_pro": True})
    assert precio == 899.99
    # un accesorio "Pro" dentro del pack no convierte la consola en Pro
    precio, _ = v.precio_max_pack("Sony PlayStation 5 Slim Chasis E + Razer Kaira Pro", REGLAS)
    assert precio == pytest.approx(749.99)


def test_regla_legible():
    _, regla = v.precio_max_pack("Sony PlayStation 5 Slim Chasis E + GTA V + Mafia", REGLAS)
    assert regla == "PS5 con lector 649,99 € + 2 juegos × 80,00 €"


# ---------- leer listados ----------
LISTADO = """
<html><body>
<nav><a href="/videoconsolas-ps5">Consolas PS5</a><a href="/juegos-ps5">Juegos PS5</a>
<a href="https://www.pccomponentes.com/sony-playstation-5-slim">PS5 Slim</a></nav>
<div class="card">
  <a href="/sony-playstation-5-slim-digital-chasis-e?ref=listado#top"><img alt="Sony PlayStation 5 Slim Digital Chasis E" src="x.jpg"></a>
  <a href="/sony-playstation-5-slim-digital-chasis-e"><h3>Sony PlayStation 5 Slim Digital Chasis E</h3>
     <span>599€</span> <span>PVPR 599,99€</span> 4,7/5 2458 opiniones Vendido y enviado por PcComponentes</a>
  <a href="/opiniones/sony-playstation-5-slim-digital-chasis-e">2458 opiniones Sony PlayStation 5 Slim Digital</a>
</div>
<div class="card"><a href="/sony-playstation-5-slim-chasis-e-marvels-lobezno-marvels-spider-man-2" title="Sony PlayStation 5 Slim Chasis E + Marvels: Lobezno + Marvels: Spider-Man 2">ver</a></div>
<div class="card"><a href="/sony-playstation-5-pro">Sony PlayStation 5 Pro 2TB</a></div>
<div class="card"><a href="/mando-ps5-sony-dualsense-v2-blanco">Mando PS5 Sony DualSense V2 Blanco 74,99€</a></div>
<div class="card"><a href="/grand-theft-auto-vi-standard-ed-ciab-ps5">GRAND THEFT AUTO VI STANDARD ED. (GTA VI) CIAB PS5</a></div>
<a href="https://www.otra-tienda.es/consola-ps5-slim">Consola PS5 Slim barata</a>
<script type="application/ld+json">{"@type":"ItemList","itemListElement":[
  {"@type":"ListItem","position":1,"url":"https://www.pccomponentes.com/sony-playstation-5-slim-digital-chasis-e-plus-2x-mandos-dualsense-v3",
   "name":"Sony PlayStation 5 Slim Digital Chasis E + 2x Mandos DualSense V3"}]}</script>
</body></html>
"""
URL_LISTADO = "https://www.pccomponentes.com/videoconsolas-ps5"


def test_descubre_consolas_y_packs_del_listado():
    pares = v.extraer_enlaces(LISTADO, URL_LISTADO)
    items = v.consolas_del_listado(pares, URL_LISTADO, 25)
    urls = [u for u, _ in items]
    assert urls == [
        "https://www.pccomponentes.com/sony-playstation-5-slim",
        "https://www.pccomponentes.com/sony-playstation-5-slim-digital-chasis-e",
        "https://www.pccomponentes.com/sony-playstation-5-slim-chasis-e-marvels-lobezno-marvels-spider-man-2",
        "https://www.pccomponentes.com/sony-playstation-5-pro",
        "https://www.pccomponentes.com/sony-playstation-5-slim-digital-chasis-e-plus-2x-mandos-dualsense-v3",
    ]
    titulos = dict(items)
    assert titulos["https://www.pccomponentes.com/sony-playstation-5-slim-digital-chasis-e"] == \
        "Sony PlayStation 5 Slim Digital Chasis E"
    # el Pro se descubre pero no se vigila (no es "barato")
    prods = v.productos_descubiertos({"tienda": "PcComponentes", "url": URL_LISTADO, "vendedor": "PcComponentes"},
                                     items, REGLAS)
    assert "https://www.pccomponentes.com/sony-playstation-5-pro" not in [p["url"] for p in prods]
    lobezno = [p for p in prods if "lobezno" in p["url"]][0]
    assert lobezno["precio_max"] == pytest.approx(809.99) and lobezno["descubierto"]


def test_maximo_de_productos_por_listado():
    pares = v.extraer_enlaces(LISTADO, URL_LISTADO)
    assert len(v.consolas_del_listado(pares, URL_LISTADO, 2)) == 2


def test_limpiar_titulo():
    assert v.limpiar_titulo("Sony PlayStation 5 Slim Chasis E 645,99€ PVPR 649,99€ 4,7/5") == \
        "Sony PlayStation 5 Slim Chasis E"
    t = "Sony PlayStation 5 Slim Digital Chasis E + Sony Tarjeta Prepago 20€ para PlayStation Store"
    assert v.limpiar_titulo(t) == t
    assert v.limpiar_titulo("Consola PS5 Pro 1.336,32 €") == "Consola PS5 Pro"


# ---------- estado de los listados ----------
class Buzon:
    def __init__(self):
        self.mensajes = []

    def __call__(self, texto, titulo="", url=None, urgente=False):
        self.mensajes.append((titulo, texto))
        return True


def test_listado_que_falla_reutiliza_lo_conocido_y_avisa_una_vez():
    lst = {"tienda": "Carrefour", "url": "https://carrefour/c"}
    estado, buzon = {}, Buzon()
    items = [("https://carrefour/ps5/p", "Consola PlayStation 5 Digital 825GB")]
    assert v.procesar_listado(lst, items, "", estado, buzon) == items
    for _ in range(v.UMBRAL_FALLOS + 3):
        assert v.procesar_listado(lst, [], "HTTP 403", estado, buzon) == items
    assert len(buzon.mensajes) == 1 and "Sigo vigilando las 1" in buzon.mensajes[0][1]
    v.procesar_listado(lst, items, "", estado, buzon)
    assert estado["_listados"][lst["url"]]["fallos"] == 0


def test_listado_experimental_no_avisa():
    lst = {"tienda": "Fnac", "url": "https://fnac/buscar", "experimental": True}
    estado, buzon = {}, Buzon()
    for _ in range(v.UMBRAL_FALLOS + 3):
        v.procesar_listado(lst, [], "HTTP 403", estado, buzon)
    assert buzon.mensajes == []


def test_ficha_descubierta_que_falla_no_avisa():
    prod = {"tienda": "X", "nombre": "PS5", "url": "https://x/1", "precio_max": 599.99, "descubierto": True}
    estado, buzon = {}, Buzon()
    for _ in range(v.UMBRAL_FALLOS + 3):
        v.procesar_lectura(prod, v.Lectura(v.DESCONOCIDO), estado, buzon)
    assert buzon.mensajes == []


def test_trocear_mensajes_largos():
    texto = "\n".join(f"línea {i} " + "x" * 80 for i in range(200))
    trozos = v.trocear(texto, 3900)
    assert len(trozos) > 1 and all(len(t) <= 3900 for t in trozos)
    assert "\n".join(trozos) == texto


# ---------- pasada completa con listados simulados ----------
def ld_producto(disp, precio, vendedor):
    return ('<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "offers": {"@type": "Offer", "availability": f"https://schema.org/{disp}",
                                       "price": precio, "seller": {"@type": "Organization", "name": vendedor}}})
            + "</script>")


def test_pasada_con_listados(tmp_path, monkeypatch):
    config = {
        "reglas_packs": {"extra_por_juego_o_mando": 60},
        "productos": [{"tienda": "Sony", "nombre": "PS5 Digital", "url": "https://sony/ps5", "precio_max": 599.99,
                       "modo": "navegador", "fuente": "texto"}],
        "listados": [
            {"tienda": "PcComponentes", "url": URL_LISTADO, "vendedor": "PcComponentes"},
            {"tienda": "Fnac", "url": "https://www.fnac.es/buscar", "vendedor": "Fnac", "experimental": True},
        ],
    }
    (tmp_path / "productos.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(v, "RUTA_PRODUCTOS", tmp_path / "productos.json")
    monkeypatch.setattr(v, "RUTA_ESTADO", tmp_path / "estado.json")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    base = "https://www.pccomponentes.com/"
    paginas = {
        URL_LISTADO: (200, LISTADO),
        "https://www.fnac.es/buscar": (403, "<title>Access Denied</title>"),
        base + "sony-playstation-5-slim": (200, "<html>categoría</html>"),
        base + "sony-playstation-5-slim-digital-chasis-e": (200, ld_producto("OutOfStock", "599.00", "PcComponentes")),
        # pack en stock por debajo de su máximo calculado (809,99): avisa
        base + "sony-playstation-5-slim-chasis-e-marvels-lobezno-marvels-spider-man-2":
            (200, ld_producto("InStock", "749.99", "PcComponentes")),
        # pack en stock pero caro (máximo 674,99): no avisa
        base + "sony-playstation-5-slim-digital-chasis-e-plus-2x-mandos-dualsense-v3":
            (200, ld_producto("InStock", "699.00", "PcComponentes")),
    }
    pedidas = []

    def falso_http(url, timeout=25):
        pedidas.append(url)
        status, html = paginas[url]
        return status, html, url

    monkeypatch.setattr(v, "descargar_http", falso_http)
    buzon = Buzon()
    res = v.pasada(SimpleNamespace(probar=False, resumen=False), None, avisar=buzon, pausa=None)

    por_url = {p["url"]: (p, l) for p, l in res}
    assert base + "sony-playstation-5-pro" not in por_url          # Pro descartada
    assert base + "mando-ps5-sony-dualsense-v2-blanco" not in pedidas  # accesorio ni se pide
    lobezno, l = por_url[base + "sony-playstation-5-slim-chasis-e-marvels-lobezno-marvels-spider-man-2"]
    assert l.estado == v.EN_STOCK and lobezno["precio_max"] == pytest.approx(809.99)

    alertas = [t for titulo, t in buzon.mensajes if "en stock" in titulo.lower()]
    assert len(alertas) == 1 and "Lobezno" in alertas[0] and "Máximo calculado" in alertas[0]
    resumen_txt = [t for titulo, t in buzon.mensajes if "resumen" in titulo][0]
    assert "🔎 PcComponentes: 5 consolas/packs" in resumen_txt
    assert "❔ Fnac: no se pudo leer el listado" in resumen_txt
    assert "🟠" in resumen_txt and "🟢" in resumen_txt

    # segunda pasada: el listado de PcComponentes cae; sigue revisando lo que ya conocía
    paginas[URL_LISTADO] = (403, "<title>Just a moment...</title>")
    pedidas.clear()
    res2 = v.pasada(SimpleNamespace(probar=False, resumen=False), None, avisar=buzon, pausa=None)
    assert base + "sony-playstation-5-slim-chasis-e-marvels-lobezno-marvels-spider-man-2" in pedidas
    assert len([1 for titulo, _ in buzon.mensajes if "en stock" in titulo.lower()]) == 1  # no repite


def test_revisar_todos_mantiene_el_orden(monkeypatch):
    productos = [{"tienda": t, "nombre": str(i), "url": f"https://{t}.es/{i}", "precio_max": 600}
                 for i, t in enumerate(["a", "b", "a", "c", "b", "a"])]

    def falso_http(url, timeout=25):
        return 200, ld_producto("InStock", "500", "x"), url

    monkeypatch.setattr(v, "descargar_http", falso_http)
    res = v.revisar_todos(productos, None, None)
    assert [p["nombre"] for p, _ in res] == [str(i) for i in range(6)]
    assert all(l.estado == v.EN_STOCK for _, l in res)
