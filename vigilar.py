#!/usr/bin/env python3
"""
Vigilante de stock de PS5.

Hace dos cosas en cada pasada:
  1. Lee los LISTADOS de consolas PS5 de varias tiendas y descubre solo las consolas y packs
     (con juegos, mandos, saldo...) que haya, aunque sean nuevos.
  2. Revisa cada ficha (las descubiertas + las fijas de productos.json) y te avisa por Telegram y/o
     ntfy cuando una pasa a estar EN STOCK a un precio igual o menor que su máximo.

Precio máximo de los packs descubiertos = precio oficial de la consola (digital / con lector)
+ X € por cada juego o mando extra + el saldo que incluya (todo configurable en productos.json).

Uso:
  python vigilar.py                 una pasada (lo que ejecuta GitHub Actions)
  python vigilar.py --probar        una pasada sin avisar ni guardar: imprime lo que detecta
  python vigilar.py --probar-aviso  manda un mensaje de prueba para comprobar Telegram/ntfy
  python vigilar.py --resumen       además de vigilar, manda un resumen del estado de todo
  python vigilar.py --bucle 120     en tu PC: repite cada ~120 segundos hasta que lo pares (Ctrl+C)

Avisos (variables de entorno):
  TELEGRAM_TOKEN + TELEGRAM_CHAT_ID   -> mensaje de Telegram
  NTFY_TOPIC (y opcional NTFY_SERVER) -> notificación push con la app ntfy
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

BASE = Path(__file__).resolve().parent
RUTA_PRODUCTOS = BASE / "productos.json"
RUTA_ESTADO = BASE / "estado.json"

# Tras cuántos fallos seguidos te aviso de que algo no se está pudiendo vigilar
UMBRAL_FALLOS = 6

EN_STOCK = "en_stock"
AGOTADO = "agotado"
COLA = "cola"            # cola virtual (Queue-it): suele significar reposición en Sony
DESCONOCIDO = "desconocido"
ERROR = "error"

REGLAS_POR_DEFECTO = {"digital": 599.99, "lector": 649.99, "pro": 899.99,
                      "incluir_pro": False, "extra_por_juego_o_mando": 60}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CABECERAS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


@dataclass
class Lectura:
    estado: str
    precio: Optional[float] = None
    vendedor: Optional[str] = None
    metodo: str = ""
    detalle: str = ""


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------

def sin_acentos(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def normalizar_nombre(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", sin_acentos(s or ""))


def normalizar_titulo(s: str) -> str:
    s = sin_acentos(s or "").replace("®", "").replace("™", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def a_precio(valor) -> Optional[float]:
    """Convierte '599,00', '599.00', '1.336,32', 599 ... a float."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).replace("€", "").replace("EUR", "").replace("\xa0", "").replace(" ", "").strip()
    s = re.sub(r"[^\d.,]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")     # 1.336,32
        else:
            s = s.replace(",", "")                        # 1,336.32
    elif "," in s:
        partes = s.split(",")
        s = s.replace(",", ".") if len(partes[-1]) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s and len(s.split(".")[-1]) == 3:
        s = s.replace(".", "")                            # 1.336 (miles)
    try:
        return float(s)
    except ValueError:
        return None


def euros(p: Optional[float]) -> str:
    if p is None:
        return "precio no detectado"
    txt = f"{p:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{txt} €"


def ahora_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def html_a_texto(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    texto = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", htmlmod.unescape(texto)).strip()


def limpiar_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "", ""))


def dominio(url: str) -> str:
    d = urllib.parse.urlsplit(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


# --------------------------------------------------------------------------------------
# Lectura de una ficha: JSON-LD (schema.org) -> metaetiquetas -> texto visible
# --------------------------------------------------------------------------------------

RE_LDJSON = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)

DISPONIBLES = {"en_stock", "reserva", "bajo_pedido", "solo_tienda"}
NOTAS_DISPONIBILIDAD = {
    "reserva": "es una reserva (preventa)",
    "bajo_pedido": "bajo pedido: te la envían más tarde",
    "solo_tienda": "solo en tienda física (mira qué tienda la tiene)",
}


def normalizar_disponibilidad(valor) -> Optional[str]:
    if not valor:
        return None
    if isinstance(valor, list):
        valor = valor[0] if valor else ""
    if isinstance(valor, dict):
        valor = valor.get("@id") or valor.get("name") or ""
    s = re.sub(r"[\s_-]", "", str(valor).lower().rsplit("/", 1)[-1])
    if s in {"instock", "limitedavailability", "onlineonly", "available", "disponible"}:
        return "en_stock"
    if s == "instoreonly":
        return "solo_tienda"
    if s in {"preorder", "presale"}:
        return "reserva"
    if s == "backorder":
        return "bajo_pedido"
    if s in {"outofstock", "soldout", "discontinued", "oos", "agotado", "notavailable"}:
        return "agotado"
    return None


def _tipos(nodo: dict) -> list[str]:
    t = nodo.get("@type")
    lista = t if isinstance(t, list) else [t]
    return [x.lower() for x in lista if isinstance(x, str)]


def _como_lista(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _cargar_ldjson(html: str) -> list:
    bloques = []
    for m in RE_LDJSON.finditer(html):
        crudo = m.group(1).strip()
        for intento in (crudo, htmlmod.unescape(crudo)):
            try:
                bloques.append(json.loads(intento, strict=False))
                break
            except (ValueError, TypeError):
                continue
    return bloques


def _nodos_raiz(bloques: list):
    for b in bloques:
        for n in _como_lista(b):
            if not isinstance(n, dict):
                continue
            grafo = n.get("@graph")
            if isinstance(grafo, list):
                for g in grafo:
                    if isinstance(g, dict):
                        yield g
            else:
                yield n


TIPOS_PRODUCTO = {"product", "productgroup", "individualproduct", "productmodel", "videogame"}


def _productos_principales(bloques: list) -> list[dict]:
    """Productos de primer nivel. Ignora listas de productos relacionados (ItemList)."""
    productos = []
    for n in _nodos_raiz(bloques):
        tipos = set(_tipos(n))
        if tipos & TIPOS_PRODUCTO:
            productos.append(n)
        else:
            for clave in ("mainEntity", "object", "item"):
                principal = n.get(clave)
                if isinstance(principal, dict) and set(_tipos(principal)) & TIPOS_PRODUCTO:
                    productos.append(principal)
    return productos


def _ofertas_de(nodo: dict) -> list[dict]:
    ofertas = []
    for o in _como_lista(nodo.get("offers")):
        if not isinstance(o, dict):
            continue
        if "aggregateoffer" in _tipos(o) and o.get("offers"):
            ofertas += [x for x in _como_lista(o["offers"]) if isinstance(x, dict)]
            if o.get("availability"):
                ofertas.append(o)
        else:
            ofertas.append(o)
    for variante in _como_lista(nodo.get("hasVariant")):
        if isinstance(variante, dict):
            ofertas += _ofertas_de(variante)
    return ofertas


def _nombre_vendedor(v) -> Optional[str]:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return v.get("name") or v.get("legalName")
    if isinstance(v, str):
        return v
    return None


def _precio_oferta(o: dict) -> Optional[float]:
    for clave in ("price", "lowPrice"):
        p = a_precio(o.get(clave))
        if p is not None:
            return p
    for spec in _como_lista(o.get("priceSpecification")):
        if isinstance(spec, dict):
            p = a_precio(spec.get("price"))
            if p is not None:
                return p
    return None


def evaluar_ofertas(ofertas: list[dict], prod: dict, metodo: str) -> Lectura:
    vendedor_pedido = normalizar_nombre(prod.get("vendedor", ""))
    validas, descartadas = [], 0
    for o in ofertas:
        condicion = str(o.get("itemCondition") or "").lower()
        if any(x in condicion for x in ("used", "refurbished", "damaged")) and not prod.get("acepta_reacondicionado"):
            descartadas += 1
            continue
        vendedor = _nombre_vendedor(o.get("seller"))
        if vendedor_pedido and vendedor and vendedor_pedido not in normalizar_nombre(vendedor):
            descartadas += 1
            continue
        validas.append((normalizar_disponibilidad(o.get("availability")), _precio_oferta(o), vendedor))

    if not validas:
        if descartadas:
            return Lectura(AGOTADO, metodo=metodo, detalle="solo la venden vendedores externos o reacondicionada")
        return Lectura(DESCONOCIDO, metodo=metodo, detalle="ficha sin ofertas")

    disponibles = [v for v in validas if v[0] in DISPONIBLES]
    if disponibles:
        disp, precio, vendedor = min(disponibles, key=lambda v: (v[1] is None, v[1] or 0))
        return Lectura(EN_STOCK, precio, vendedor, metodo, NOTAS_DISPONIBILIDAD.get(disp, ""))

    precios = [v[1] for v in validas if v[1] is not None]
    precio = min(precios) if precios else None
    if any(v[0] == "agotado" for v in validas):
        return Lectura(AGOTADO, precio, metodo=metodo)
    return Lectura(DESCONOCIDO, precio, metodo=metodo, detalle="la ficha no dice si hay stock")


def leer_ldjson(html: str, prod: dict) -> Optional[Lectura]:
    for producto in _productos_principales(_cargar_ldjson(html)):
        ofertas = _ofertas_de(producto)
        if ofertas:
            return evaluar_ofertas(ofertas, prod, "json-ld")
    return None


RE_ETIQUETA_META = re.compile(r"<(?:meta|link)\b[^>]*>", re.I)
RE_ATRIBUTO = re.compile(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']")


def leer_meta(html: str) -> Optional[Lectura]:
    disponibilidad, precio = None, None
    for etiqueta in RE_ETIQUETA_META.findall(html):
        attrs = {k.lower(): v for k, v in RE_ATRIBUTO.findall(etiqueta)}
        nombre = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        valor = attrs.get("content") or attrs.get("href")
        if nombre in {"product:availability", "og:availability", "availability"} and disponibilidad is None:
            disponibilidad = normalizar_disponibilidad(valor)
        elif nombre in {"product:price:amount", "og:price:amount", "price"} and precio is None:
            precio = a_precio(valor)
    if disponibilidad in DISPONIBLES:
        return Lectura(EN_STOCK, precio, metodo="meta", detalle=NOTAS_DISPONIBILIDAD.get(disponibilidad, ""))
    if disponibilidad == "agotado":
        return Lectura(AGOTADO, precio, metodo="meta")
    if precio is not None:
        return Lectura(DESCONOCIDO, precio, metodo="meta")
    return None


TEXTO_EN_STOCK = ["anadir al carrito", "anadir a la cesta", "anadir a mi cesta", "comprar ahora",
                  "agregar al carrito", "add to cart", "add to basket"]
TEXTO_AGOTADO = ["agotado", "sin stock", "no disponible actualmente", "actualmente no disponible",
                 "fuera de stock", "temporalmente sin existencias", "avisame cuando", "notificarme cuando",
                 "currently unavailable", "out of stock", "sold out"]


def leer_texto(texto: str, prod: dict) -> Lectura:
    t = sin_acentos(texto)
    marcas_si = [sin_acentos(x) for x in _como_lista(prod.get("texto_en_stock"))] or TEXTO_EN_STOCK
    marcas_no = [sin_acentos(x) for x in _como_lista(prod.get("texto_agotado"))] or TEXTO_AGOTADO
    hay_si = any(m in t for m in marcas_si)
    hay_no = any(m in t for m in marcas_no)
    if hay_si and not hay_no:
        return Lectura(EN_STOCK, metodo="texto")
    if hay_no and not hay_si:
        return Lectura(AGOTADO, metodo="texto")
    if hay_si and hay_no:
        return Lectura(DESCONOCIDO, metodo="texto", detalle="la página dice cosas contradictorias")
    return Lectura(DESCONOCIDO, metodo="texto", detalle="no veo botón de compra ni aviso de agotado")


SEÑALES_BLOQUEO = ["just a moment...", "cf-chl-", "access denied", "pardon our interruption",
                   "px-captcha", "_incapsula_resource", "request unsuccessful", "attention required!",
                   "verify you are human", "are you a robot", "acceso denegado", "captcha-delivery.com"]


def parece_bloqueo(html: str) -> bool:
    cabeza = html[:30000].lower()
    return any(s in cabeza for s in SEÑALES_BLOQUEO)


def es_cola(url_final: str, html: str, texto: Optional[str]) -> bool:
    if "queue-it.net" in (url_final or "").lower():
        return True
    if "queue-it" in html.lower():
        t = sin_acentos(texto if texto is not None else html_a_texto(html))
        return any(s in t for s in ("estas en la cola", "en la cola", "sala de espera", "you are now in line"))
    return False


# --- MediaMarkt: su JSON-LD mezcla vendedores externos y variantes; el estado real va en __PRELOADED_STATE__ ---
RE_ESTADO_PRECARGADO = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*")


def _objeto_js(html: str, inicio: int) -> Optional[str]:
    profundidad, en_cadena, escape = 0, False, False
    for j in range(inicio, len(html)):
        c = html[j]
        if en_cadena:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == "{":
            profundidad += 1
        elif c == "}":
            profundidad -= 1
            if profundidad == 0:
                return html[inicio:j + 1]
    return None


def estado_mediamarkt(html: str) -> Optional[dict]:
    m = RE_ESTADO_PRECARGADO.search(html)
    if not m:
        return None
    txt = _objeto_js(html, m.end())
    if not txt:
        return None
    txt = re.sub(r"([:\[,])undefined(?=[,\]}])", r"\1null", txt)
    try:
        return json.loads(txt).get("apolloState") or {}
    except ValueError:
        return None


def _precio_mm(p: dict) -> Optional[float]:
    for clave in ("promoPrice", "price"):
        v = p.get(clave)
        if isinstance(v, dict) and a_precio(v.get("amount")) is not None:
            return a_precio(v.get("amount"))
    return None


def leer_mediamarkt(html: str, prod: dict, url_final: str) -> Optional[Lectura]:
    ap = estado_mediamarkt(html)
    if not ap:
        return None
    ids = []
    for u in (prod.get("url", ""), url_final or ""):
        m = re.search(r"-(\d+)\.html", u)
        if m:
            ids.append(m.group(1))
    candidatos = list(ids)
    for v in ap.values():
        if isinstance(v, dict) and v.get("__typename") == "GraphqlProductMatch":
            relacionados = {str(v.get(k)) for k in ("productId", "marketplaceProductId", "retailProductId",
                                                     "preferredProductId")}
            if relacionados & set(ids) and v.get("retailProductId"):
                candidatos.insert(0, str(v["retailProductId"]))   # el producto que vende MediaMarkt
    estados = {v.get("id"): v for v in ap.values()
               if isinstance(v, dict) and v.get("__typename") == "CofrOnlineStatusFeature"}
    precios = {v.get("id"): v for v in ap.values()
               if isinstance(v, dict) and v.get("__typename") == "CofrPriceFeature"}
    for pid in dict.fromkeys(candidatos):
        s = estados.get(f"Media:es:{pid}")
        if not s:
            continue
        p = precios.get(f"Media:es:{pid}") or {}
        precio = _precio_mm(p)
        st = str(s.get("onlineStatus") or "").upper()
        if p.get("isProductOfTypeMarketplace") or st == "MP_OFFER":
            return Lectura(AGOTADO, precio, metodo="mediamarkt", detalle="solo la venden vendedores externos")
        if st == "AVAILABLE" or (s.get("isAvailableAndBuyable") and s.get("isAvailableForDelivery")):
            return Lectura(EN_STOCK, precio, "MediaMarkt", "mediamarkt")
        if st == "STORE_ONLY":
            return Lectura(AGOTADO, precio, metodo="mediamarkt", detalle="online agotada, solo en tiendas físicas")
        return Lectura(AGOTADO, precio, metodo="mediamarkt", detalle=st.lower().replace("_", " ") or "no disponible")
    return None


def analizar(status: int, html: str, url_final: str, prod: dict,
             texto_visible: Optional[str] = None, modo: str = "http") -> Lectura:
    if es_cola(url_final, html, texto_visible):
        return Lectura(COLA, metodo=modo, detalle="cola virtual activa")

    previa: Optional[Lectura] = None
    es_mediamarkt = "mediamarkt." in (dominio(prod.get("url", "")) + dominio(url_final or ""))
    if es_mediamarkt:
        lectura_mm = leer_mediamarkt(html, prod, url_final)
        if lectura_mm:
            lectura_mm.metodo = f"{modo}/{lectura_mm.metodo}"
            return lectura_mm
    elif prod.get("fuente", "auto") != "texto":
        previa = leer_ldjson(html, prod) or leer_meta(html)
        if previa and previa.estado in (EN_STOCK, AGOTADO):
            previa.metodo = f"{modo}/{previa.metodo}"
            return previa

    if (texto_visible is not None or prod.get("fuente") == "texto") and not es_mediamarkt:
        lectura = leer_texto(texto_visible if texto_visible is not None else html_a_texto(html), prod)
        if lectura.estado in (EN_STOCK, AGOTADO):
            if lectura.precio is None and previa is not None:
                lectura.precio = previa.precio
            if lectura.estado == EN_STOCK and not prod.get("texto_fiable"):
                lectura.detalle = "detectado por el botón de compra de la página, compruébalo"
            lectura.metodo = f"{modo}/{lectura.metodo}"
            return lectura

    if status in (404, 410):
        return Lectura(ERROR, metodo=modo, detalle=f"la página ya no existe (HTTP {status}): revisa la URL")
    if status >= 400 or parece_bloqueo(html):
        return Lectura(ERROR, metodo=modo, detalle=f"la tienda bloquea la consulta (HTTP {status})")
    detalle = (previa.detalle if previa and previa.detalle else "no encuentro el stock en la página")
    return Lectura(DESCONOCIDO, previa.precio if previa else None, metodo=modo, detalle=detalle)


# --------------------------------------------------------------------------------------
# Listados: descubrir consolas y packs
# --------------------------------------------------------------------------------------

RE_ENLACE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
RE_ALT = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.I)

RE_ACCESORIO_AL_INICIO = re.compile(
    r"^(?:sony\s+|playstation\s+)?(?:mando|juego|videojuego|auricular|cascos|soporte|cargador|base\b|"
    r"estacion|lector|unidad de disco|cubierta|carcasa|funda|disco|ssd|cable|volante|camara|tarjeta|"
    r"playstation portal|ps portal|gafas|ps ?vr|kit\b|protector|skin|vinilo|ventilador|bolsa|maleta|"
    r"mochila|dualsense|dualshock|teclado|raton|mini consola|codigo|suscripcion|playstation plus|ps plus|"
    r"pegatina|adaptador|hub\b|memoria|wd_?black|seagate|samsung|nacon|razer|logitech|corsair)")
PALABRAS_EXCLUIDAS = ("reacondicionad", "seminuevo", "usado", "segunda mano", "playstation portal",
                      "ps portal", "vr2", "ps vr")
RE_ES_CONSOLA = re.compile(
    r"\bconsola\b|\bslim\b|chasis|chassis|edicion digital|digital edition|\bbundle\b|"
    r"\bpack\s+(?:de\s+)?(?:consola\s+)?(?:ps5|playstation\s*5)|"
    r"(?:ps5|playstation\s*5)\s+(?:digital|pro|estandar|standard)\b")


def es_consola_ps5(titulo: str) -> bool:
    t = normalizar_titulo(titulo)
    if not re.search(r"playstation\s*5|\bps5\b", t):
        return False
    if RE_ACCESORIO_AL_INICIO.match(t):
        return False
    if any(x in t for x in PALABRAS_EXCLUIDAS):
        return False
    principal = _partes_pack(t)[0][1]
    if re.search(r"\bpara\s+(?:la\s+|tu\s+|las\s+)?(?:consolas?|ps5|playstation)", principal):
        return False  # "Soporte para consola PS5", "SSD para PS5"...
    return bool(RE_ES_CONSOLA.search(principal))


def _partes_pack(t: str) -> list[tuple[str, str]]:
    """Divide 'consola + juego + 2 mandos' en [(sep, trozo)]; el primero es la consola."""
    piezas = re.split(r"(\+|\s+con\s+|\s+y\s+(?=\d+\s*x?\s*mandos?))", t)
    partes = [("", piezas[0].strip())]
    for i in range(1, len(piezas) - 1, 2):
        partes.append(("con" if "con" in piezas[i] else "+", piezas[i + 1].strip()))
    return [p for p in partes if p[1]]


def contar_extras(titulo: str) -> tuple[int, float]:
    """(número de juegos/mandos extra, € de saldo incluido)."""
    t = normalizar_titulo(titulo)
    partes = _partes_pack(t)
    principal = partes[0][1]
    n, saldo = 0, 0.0
    m = re.search(r"(\d+)\s*x?\s*mandos", principal)
    if m and int(m.group(1)) > 1:
        n += int(m.group(1)) - 1
    for sep, trozo in partes[1:]:
        if any(w in trozo for w in ("fortnite", "cosmetic", "avatar", "contenido descargable")):
            continue
        if any(w in trozo for w in ("tarjeta", "saldo", "ps plus", "playstation plus", "prepago", "psn")):
            mm = re.search(r"(\d+)\s*(?:€|eur)", trozo)
            saldo += float(mm.group(1)) if mm else 0.0
            continue
        if re.match(r"(?:\d+\s*x?\s*)?(?:mando|dualsense|lector|unidad de disco|1\s*mando)", trozo) and sep == "con":
            mk = re.search(r"(\d+)\s*x?\s*(?:mandos?|dualsense)", trozo)
            n += max(int(mk.group(1)) - 1, 0) if mk else 0
            continue
        mk = re.search(r"(\d+)\s*x?\s*(?:mandos?|dualsense)", trozo)
        if mk or re.search(r"\bmando|\bdualsense", trozo):
            k = int(mk.group(1)) if mk else 1
            n += k - 1 if k >= 2 else 1
            continue
        n += 1
    if n == 0 and saldo == 0 and len(partes) == 1 and \
            re.search(r"edicion limitada|limited edition|\bbundle\b", principal) and "fortnite" not in principal:
        n = 1
    return n, saldo


def precio_max_pack(titulo: str, reglas: dict) -> Optional[tuple[float, str]]:
    """Precio máximo razonable para una consola/pack según su título. None = no vigilar (p. ej. Pro)."""
    r = {**REGLAS_POR_DEFECTO, **(reglas or {})}
    t = normalizar_titulo(titulo)
    principal = _partes_pack(t)[0][1]
    if re.search(r"\bpro\b", principal):
        if not r["incluir_pro"]:
            return None
        base, tipo = float(r["pro"]), "PS5 Pro"
    elif "digital" in principal or re.search(r"\b825\s*gb", principal):
        base, tipo = float(r["digital"]), "PS5 Digital"
    else:
        base, tipo = float(r["lector"]), "PS5 con lector"
    n, saldo = contar_extras(titulo)
    extra = float(r["extra_por_juego_o_mando"])
    total = round(base + n * extra + saldo, 2)
    explicacion = f"{tipo} {euros(base)}"
    if n:
        explicacion += f" + {n} extra{'s' if n > 1 else ''} × {euros(extra)}"
    if saldo:
        explicacion += f" + saldo {euros(saldo)}"
    return total, explicacion


def unir_textos(partes) -> str:
    """Une nombre, title, aria-label y alt sin repetir lo que ya está contenido en otro."""
    limpias: list[str] = []
    for p in partes:
        p = re.sub(r"\s+", " ", p or "").strip()
        if not p or any(sin_acentos(p) in sin_acentos(q) for q in limpias):
            continue
        limpias = [q for q in limpias if sin_acentos(q) not in sin_acentos(p)]
        limpias.append(p)
    return " ".join(limpias)


def extraer_enlaces(html: str, url_base: str) -> list[tuple[str, str]]:
    pares = []
    for attrs_txt, interior in RE_ENLACE.findall(html):
        attrs = {k.lower(): htmlmod.unescape(v) for k, v in RE_ATRIBUTO.findall(attrs_txt)}
        href = attrs.get("href", "")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        texto = unir_textos([html_a_texto(interior), attrs.get("title", ""), attrs.get("aria-label", ""),
                             *RE_ALT.findall(interior)])
        pares.append((urllib.parse.urljoin(url_base, href), texto))
    for nodo in _nodos_raiz(_cargar_ldjson(html)):
        if "itemlist" in _tipos(nodo):
            for el in _como_lista(nodo.get("itemListElement")):
                if not isinstance(el, dict):
                    continue
                item = el.get("item") if isinstance(el.get("item"), dict) else el
                url = item.get("url") or el.get("url") or (el.get("item") if isinstance(el.get("item"), str) else None)
                if url:
                    pares.append((urllib.parse.urljoin(url_base, url), item.get("name") or el.get("name") or ""))
    return pares


RE_URL_NO_PRODUCTO = re.compile(
    r"/(?:list|category|categories|categorias|specials|buscar|search|searchresult|opiniones|reviews|marcas?|"
    r"brand|campaign|ofertas|promociones)(?:/|$)|/c$|/c/", re.I)
# precio de la tarjeta (>= 100 €): corta ahí el texto; no corta "Tarjeta 20€"
RE_PRECIO_EN_TEXTO = re.compile(r"\s(?:desde\s)?(?:\d{1,3}(?:[.,]\d{3})+|\d{3,4})(?:[.,]\d{1,2})?\s?€")


RE_ETIQUETA_INICIAL = re.compile(
    r"^(?:-?\s*\d+\s*%\s*|top black friday\s*|black friday\s*|recomendado\s*|oferta\s*|novedad\s*|"
    r"nuevo\s*|exclusivo\s*|top ventas\s*|mas vendido\s*|más vendido\s*|-\s*)+", re.I)


def _quitar_repeticion(t: str) -> str:
    """'PACK PS5 + Lobezno PACK PS5 + Lobezno ...' -> 'PACK PS5 + Lobezno ...'"""
    for i in range(len(t) // 2, 9, -1):
        if t[i] == " " and t[i + 1:].startswith(t[:i]):
            return t[:i] + t[i + 1 + i:]
    return t


def limpiar_titulo(texto: str) -> str:
    """Quita lo que acompaña al nombre en las tarjetas de producto (precio, etiquetas, repeticiones...)."""
    texto = re.sub(r"\s+", " ", texto or "").strip()
    texto = RE_PRECIO_EN_TEXTO.split(" " + texto)[0].strip()
    texto = RE_ETIQUETA_INICIAL.sub("", texto).strip()
    return _quitar_repeticion(texto).strip()


def consolas_del_listado(pares: list[tuple[str, str]], url_listado: str, maximo: int) -> list[tuple[str, str]]:
    dom = dominio(url_listado)
    listado_limpio = limpiar_url(url_listado)
    titulos: "OrderedDict[str, str]" = OrderedDict()
    for url, texto in pares:
        if not url.startswith("http") or dominio(url) != dom:
            continue
        u = limpiar_url(url)
        if u == listado_limpio or RE_URL_NO_PRODUCTO.search(urllib.parse.urlsplit(u).path):
            continue
        texto = limpiar_titulo(texto)
        if u not in titulos or len(texto) > len(titulos[u]):
            titulos[u] = texto
    elegidos = [(u, t[:200]) for u, t in titulos.items() if t and es_consola_ps5(t)]
    return elegidos[:maximo]


# --------------------------------------------------------------------------------------
# Descarga
# --------------------------------------------------------------------------------------

def descargar_http(url: str, timeout: int = 25) -> tuple[int, str, str]:
    try:
        from curl_cffi import requests as creq  # imita la huella TLS de Chrome: menos bloqueos
    except ImportError:
        creq = None
    if creq is not None:
        r = creq.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True,
                     headers={"Accept-Language": "es-ES,es;q=0.9"})
        return r.status_code, r.text or "", str(r.url)
    req = urllib.request.Request(url, headers=CABECERAS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url


JS_ENLACES = """() => Array.from(document.querySelectorAll('a[href]')).map(a => [a.href,
  [(a.innerText || ''), a.getAttribute('title') || '', a.getAttribute('aria-label') || '',
   ...Array.from(a.querySelectorAll('img[alt]')).map(i => i.alt)]])"""


class Navegador:
    """Chromium sin ventana (Playwright) para webs que cargan el stock con JavaScript."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self.disponible: Optional[bool] = None
        self.motivo = ""

    def arrancar(self) -> bool:
        if self.disponible is not None:
            return self.disponible
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            self.disponible = True
        except Exception as e:  # noqa: BLE001
            self.disponible = False
            self.motivo = f"{type(e).__name__}: {e}"[:200]
        return self.disponible

    def _abrir(self, url: str, timeout: int, desplazar: bool):
        if not self.arrancar():
            raise RuntimeError(f"navegador no disponible ({self.motivo})")
        ctx = self._browser.new_context(locale="es-ES", user_agent=UA,
                                        viewport={"width": 1366, "height": 900})
        pagina = ctx.new_page()
        resp = pagina.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        try:
            pagina.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        if desplazar:
            for _ in range(4):
                pagina.mouse.wheel(0, 2500)
                pagina.wait_for_timeout(700)
        pagina.wait_for_timeout(2000)
        return ctx, pagina, (resp.status if resp else 0)

    def descargar(self, url: str, timeout: int = 45) -> tuple[int, str, str, str]:
        ctx, pagina, status = self._abrir(url, timeout, desplazar=False)
        try:
            return status, pagina.content(), pagina.inner_text("body"), pagina.url
        finally:
            ctx.close()

    def descargar_enlaces(self, url: str, timeout: int = 45) -> tuple[int, str, str, list]:
        ctx, pagina, status = self._abrir(url, timeout, desplazar=True)
        try:
            enlaces = [(href, unir_textos(partes)) for href, partes in pagina.evaluate(JS_ENLACES)]
            return status, pagina.content(), pagina.url, enlaces
        finally:
            ctx.close()

    def cerrar(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _ya_no_existe(l: Lectura) -> bool:
    return "ya no existe" in (l.detalle or "")


def revisar_http(prod: dict) -> Lectura:
    try:
        status, html, final = descargar_http(prod["url"])
        return analizar(status, html, final, prod, None, "http")
    except Exception as e:  # noqa: BLE001
        return Lectura(ERROR, metodo="http", detalle=f"{type(e).__name__}: {e}"[:200])


def revisar_navegador(prod: dict, nav: Optional[Navegador]) -> Lectura:
    if nav is None:
        return Lectura(ERROR, metodo="navegador", detalle="navegador desactivado")
    try:
        status, html, texto, final = nav.descargar(prod["url"])
        return analizar(status, html, final, prod, texto, "navegador")
    except Exception as e:  # noqa: BLE001
        return Lectura(ERROR, metodo="navegador", detalle=f"{type(e).__name__}: {e}"[:200])


def revisar(prod: dict, nav: Optional[Navegador]) -> Lectura:
    """Revisa una ficha: http y, si no sale nada claro, navegador."""
    if prod.get("modo") == "navegador":
        return revisar_navegador(prod, nav)
    lectura = revisar_http(prod)
    if (lectura.estado in (ERROR, DESCONOCIDO) and nav is not None
            and prod.get("reintentar_con_navegador", True) and not _ya_no_existe(lectura) and nav.arrancar()):
        segunda = revisar_navegador(prod, nav)
        if segunda.estado in (EN_STOCK, AGOTADO, COLA):
            return segunda
    return lectura


def revisar_todos(productos: list[dict], nav: Optional[Navegador], pausa) -> list[tuple[dict, Lectura]]:
    """Fase 1: http en paralelo (una cola por tienda). Fase 2: navegador, uno a uno."""
    lecturas: dict[int, Lectura] = {}
    por_tienda = defaultdict(list)
    for i, p in enumerate(productos):
        if p.get("modo") != "navegador":
            por_tienda[dominio(p["url"])].append(i)

    def trabajar(indices):
        for k, i in enumerate(indices):
            if k and pausa:
                time.sleep(random.uniform(*pausa))
            lecturas[i] = revisar_http(productos[i])

    if por_tienda:
        with ThreadPoolExecutor(max_workers=min(8, len(por_tienda))) as ex:
            list(ex.map(trabajar, por_tienda.values()))

    tiendas_que_fallan_en_navegador = set()
    for i, p in enumerate(productos):
        if p.get("modo") == "navegador":
            lecturas[i] = revisar_navegador(p, nav)
            continue
        l = lecturas[i]
        dom = dominio(p["url"])
        if (l.estado in (ERROR, DESCONOCIDO) and nav is not None and dom not in tiendas_que_fallan_en_navegador
                and p.get("reintentar_con_navegador", True) and not _ya_no_existe(l) and nav.arrancar()):
            segunda = revisar_navegador(p, nav)
            if segunda.estado in (EN_STOCK, AGOTADO, COLA):
                lecturas[i] = segunda
            else:
                tiendas_que_fallan_en_navegador.add(dom)  # no insistir con esa tienda en esta pasada
    return [(p, lecturas[i]) for i, p in enumerate(productos)]


def descubrir_listados(listados: list[dict], nav: Optional[Navegador]) -> dict[str, tuple[list, str]]:
    """Devuelve {url_listado: ([(url, titulo)], detalle_error)}."""
    resultado: dict[str, tuple[list, str]] = {}

    def por_http(lst):
        url = lst["url"]
        try:
            status, html, final = descargar_http(url)
            if status >= 400 or (parece_bloqueo(html) and "<a" not in html[:200000].lower()):
                return url, [], f"HTTP {status}" + (" (bloqueo)" if parece_bloqueo(html) else "")
            return url, consolas_del_listado(extraer_enlaces(html, final), url, lst.get("max_productos", 25)), ""
        except Exception as e:  # noqa: BLE001
            return url, [], f"{type(e).__name__}: {e}"[:150]

    http = [l for l in listados if l.get("modo") != "navegador"]
    if http:
        with ThreadPoolExecutor(max_workers=min(8, len(http))) as ex:
            for url, items, detalle in ex.map(por_http, http):
                resultado[url] = (items, detalle)

    for lst in listados:
        url = lst["url"]
        items, detalle = resultado.get(url, ([], ""))
        suficientes = len(items) >= lst.get("minimo_esperado", 5)
        if suficientes or nav is None or not lst.get("navegador_si_falla", True) or not nav.arrancar():
            resultado[url] = (items, detalle or ("" if items else "no encuentro consolas en el listado"))
            continue
        try:
            status, html, final, enlaces = nav.descargar_enlaces(url)
            extra = consolas_del_listado(list(enlaces) + extraer_enlaces(html, final), url,
                                         lst.get("max_productos", 25))
            vistos = {u for u, _ in items}
            juntos = (items + [x for x in extra if x[0] not in vistos])[:lst.get("max_productos", 25)]
            resultado[url] = (juntos, "" if juntos else f"no encuentro consolas en el listado (HTTP {status})")
        except Exception as e:  # noqa: BLE001
            resultado[url] = (items, "" if items else (detalle or f"{type(e).__name__}: {e}"[:150]))
    return resultado


# --------------------------------------------------------------------------------------
# Avisos
# --------------------------------------------------------------------------------------

def trocear(texto: str, maximo: int = 3900) -> list[str]:
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        while len(linea) > maximo:
            trozos.append(linea[:maximo])
            linea = linea[maximo:]
        if len(actual) + len(linea) + 1 > maximo:
            trozos.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        trozos.append(actual)
    return trozos


def explicar_error_telegram(e: "urllib.error.HTTPError") -> str:
    try:
        descripcion = json.loads(e.read().decode("utf-8", "replace")).get("description", "")
    except Exception:  # noqa: BLE001
        descripcion = ""
    pistas = {
        401: "el TOKEN no es válido: cópialo otra vez de @BotFather (/mybots → tu bot → API Token).",
        400: "el CHAT ID no es correcto: es el número que te da @userinfobot.",
        403: "el bot no tiene permiso para escribirte: abre en Telegram el chat con ESE bot (el del token) "
             "y pulsa Iniciar. Si ya lo hiciste, revisa que el chat ID sea el tuyo y no el de otro bot.",
    }
    return f"HTTP {e.code} {descripcion} -> {pistas.get(e.code, 'revisa el token y el chat ID')}"


def enviar_aviso(texto: str, titulo: str = "Vigilante PS5", url: Optional[str] = None,
                 urgente: bool = False) -> bool:
    enviado = False
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            for trozo in trocear(texto):
                datos = urllib.parse.urlencode({"chat_id": chat, "text": trozo,
                                                "disable_web_page_preview": "true"}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=datos, timeout=20)
            enviado = True
        except urllib.error.HTTPError as e:
            log(f"  ! Telegram falló: {explicar_error_telegram(e)}")
        except Exception as e:  # noqa: BLE001
            log(f"  ! Telegram falló: {e}")
    tema = os.getenv("NTFY_TOPIC")
    if tema:
        servidor = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        cuerpo = {"topic": tema, "title": titulo, "message": texto[:3900], "priority": 5 if urgente else 3,
                  "tags": ["video_game"]}
        if url:
            cuerpo["click"] = url
        peticion = urllib.request.Request(servidor, data=json.dumps(cuerpo).encode("utf-8"),
                                          headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(peticion, timeout=20)
            enviado = True
        except Exception as e:  # noqa: BLE001
            log(f"  ! ntfy falló: {e}")
    if not enviado:
        log(f"  [aviso sin enviar: no hay Telegram ni ntfy configurado]\n{texto}")
    return enviado


def mensaje_stock(prod: dict, l: Lectura) -> str:
    lineas = [f"🟢 PS5 EN STOCK en {prod['tienda']}",
              f"{prod['nombre']}: {euros(l.precio)} (tu máximo: {euros(float(prod['precio_max']))})"]
    if prod.get("regla"):
        lineas.append(f"Máximo calculado: {prod['regla']}")
    if l.vendedor:
        lineas.append(f"Vendedor: {l.vendedor}")
    if l.detalle:
        lineas.append(f"Ojo: {l.detalle}")
    if l.precio is None:
        lineas.append("No he podido leer el precio: compruébalo antes de pagar.")
    lineas.append(prod["url"])
    return "\n".join(lineas)


def mensaje_cola(prod: dict) -> str:
    return (f"🟡 {prod['tienda']} tiene cola virtual en {prod['nombre']}.\n"
            f"Suele significar que acaba de reponer. Entra ya y no cierres la pestaña:\n{prod['url']}")


def mensaje_fallos(prod: dict, l: Lectura) -> str:
    return (f"⚠️ No consigo vigilar {prod['tienda']} · {prod['nombre']} ({l.detalle or l.estado}).\n"
            f"Mientras tanto esa ficha NO está vigilada. Si sigue así, prueba a ejecutarlo en tu PC "
            f"(python vigilar.py --bucle 120) o revisa la URL:\n{prod['url']}")


def procesar_lectura(prod: dict, l: Lectura, estado: dict, avisar: Callable[..., bool],
                     silenciar_fallos: bool = False) -> None:
    e = estado.setdefault(prod["url"], {"ok": False, "cola": False, "fallos": 0, "aviso_fallos": False})
    precio_max = float(prod.get("precio_max") or 10**9)

    if l.estado in (ERROR, DESCONOCIDO):
        e["fallos"] = e.get("fallos", 0) + 1
        if (e["fallos"] >= UMBRAL_FALLOS and not e.get("aviso_fallos") and not silenciar_fallos
                and not prod.get("descubierto") and not prod.get("experimental")):
            avisar(mensaje_fallos(prod, l), titulo="Vigilante PS5: ficha sin vigilar", url=prod["url"])
            e["aviso_fallos"] = True
    else:
        e["fallos"] = 0
        e["aviso_fallos"] = False
        ok = l.estado == EN_STOCK and (l.precio is None or l.precio <= precio_max + 0.005)
        if ok and not e.get("ok"):
            avisar(mensaje_stock(prod, l), titulo=f"PS5 en stock: {prod['tienda']}", url=prod["url"], urgente=True)
        e["ok"] = ok
        cola = l.estado == COLA
        if cola and not e.get("cola"):
            avisar(mensaje_cola(prod), titulo=f"Cola virtual en {prod['tienda']}", url=prod["url"], urgente=True)
        e["cola"] = cola

    e["ultimo"] = {"estado": l.estado, "precio": l.precio, "detalle": l.detalle, "hora": ahora_iso()}


def procesar_listado(lst: dict, items: list, detalle: str, estado: dict, avisar) -> list:
    """Guarda lo descubierto y, si el listado falla, reutiliza lo último conocido."""
    todos = estado.setdefault("_listados", {})
    e = todos.setdefault(lst["url"], {"productos": [], "fallos": 0, "aviso_fallos": False})
    if items:
        e.update(productos=[list(x) for x in items], fallos=0, aviso_fallos=False, ultimo_ok=ahora_iso())
        return items
    e["fallos"] = e.get("fallos", 0) + 1
    e["detalle"] = detalle
    if avisar:
        avisar_listado_caido(lst, estado, avisar)
    return [tuple(x) for x in e.get("productos", [])]


def avisar_listado_caido(lst: dict, estado: dict, avisar) -> None:
    e = estado.get("_listados", {}).get(lst["url"], {})
    if e.get("fallos", 0) >= UMBRAL_FALLOS and not e.get("aviso_fallos") and not lst.get("experimental"):
        conocidos = len(e.get("productos", []))
        avisar(f"⚠️ No consigo leer el listado de {lst['tienda']} ({e.get('detalle', '')}).\n"
               f"Sigo vigilando las {conocidos} consolas/packs que ya conocía, pero no detectaré packs nuevos "
               f"de esa tienda hasta que vuelva a funcionar.\n{lst['url']}",
               titulo="Vigilante PS5: listado sin leer", url=lst["url"])
        e["aviso_fallos"] = True


# --------------------------------------------------------------------------------------
# Tiendas que bloquean: un solo aviso por tienda y pausa
# --------------------------------------------------------------------------------------

PAUSA_TIENDA_BLOQUEADA_MIN = 30


def lugar() -> str:
    return "GitHub" if os.getenv("GITHUB_ACTIONS") == "true" else "este ordenador"


def tiendas_en_pausa(estado: dict, ahora: Optional[float] = None) -> set:
    ahora = time.time() if ahora is None else ahora
    return {t for t, e in estado.get("_tiendas", {}).items()
            if e.get("bloqueada") and e.get("siguiente", 0) > ahora}


def actualizar_tiendas(resultados: list, info_listados: list, estado: dict, avisar,
                       ahora: Optional[float] = None) -> set:
    """Marca las tiendas que no responden en nada. Devuelve las que están caídas en esta pasada."""
    ahora = time.time() if ahora is None else ahora
    lecturas_ok = defaultdict(list)
    solo_experimental = defaultdict(lambda: True)
    for p, l in resultados:
        lecturas_ok[p["tienda"]].append(l.estado in (EN_STOCK, AGOTADO, COLA))
        solo_experimental[p["tienda"]] &= bool(p.get("experimental") or p.get("descubierto"))
    for lst, n, _ in info_listados:
        lecturas_ok[lst["tienda"]].append(n > 0)
        solo_experimental[lst["tienda"]] &= bool(lst.get("experimental"))

    caidas = set()
    tiendas = estado.setdefault("_tiendas", {})
    for tienda, oks in lecturas_ok.items():
        e = tiendas.setdefault(tienda, {"fallos": 0, "bloqueada": False})
        if any(oks):
            if e.get("bloqueada") and avisar:
                avisar(f"✅ {tienda} vuelve a responder desde {lugar()}: la vuelvo a vigilar.",
                       titulo=f"{tienda} vuelve a funcionar")
            e.update(fallos=0, bloqueada=False, siguiente=0)
            continue
        caidas.add(tienda)
        e["fallos"] = e.get("fallos", 0) + 1
        if e["fallos"] >= UMBRAL_FALLOS:
            if not e.get("bloqueada") and avisar and not solo_experimental[tienda]:
                donde = "GitHub" if lugar() == "GitHub" else "tu PC"
                consejo = ("Desde tu PC suele funcionar: abre vigilar_en_pc.bat (lo explica el README)."
                           if donde == "GitHub" else "Prueba más tarde o desde otra conexión.")
                avisar(f"⚠️ {tienda} bloquea las consultas desde {donde}, así que desde aquí NO se está vigilando.\n"
                       f"Lo reintentaré cada {PAUSA_TIENDA_BLOQUEADA_MIN} minutos y te aviso si vuelve a funcionar.\n"
                       f"{consejo}", titulo=f"{tienda} sin vigilar")
            e["bloqueada"] = True
            e["siguiente"] = ahora + PAUSA_TIENDA_BLOQUEADA_MIN * 60
    return caidas


# --------------------------------------------------------------------------------------
# Resumen
# --------------------------------------------------------------------------------------

def _supera_maximo(prod: dict, l: Lectura) -> bool:
    return l.precio is not None and l.precio > float(prod["precio_max"]) + 0.005


def icono(prod: dict, l: Lectura) -> str:
    if l.estado == EN_STOCK:
        return "🟠" if _supera_maximo(prod, l) else "🟢"
    return {AGOTADO: "🔴", COLA: "🟡", ERROR: "⚠️"}.get(l.estado, "❔")


def descripcion(prod: dict, l: Lectura) -> str:
    if l.estado == EN_STOCK:
        if _supera_maximo(prod, l):
            return f"en stock pero a {euros(l.precio)} (tu máximo: {euros(float(prod['precio_max']))})"
        return f"EN STOCK a {euros(l.precio)}"
    if l.estado == AGOTADO:
        return "agotado" + (f" ({l.detalle})" if l.detalle else "")
    if l.estado == COLA:
        return "cola virtual activa"
    if l.estado == ERROR:
        return f"sin vigilar: {l.detalle}"
    return f"no se pudo leer: {l.detalle}"


def resumen(resultados: list[tuple[dict, Lectura]], info_listados: Optional[list] = None,
            pausadas: Optional[set] = None) -> str:
    fijos = [(p, l) for p, l in resultados if not p.get("descubierto")]
    descubiertos = [(p, l) for p, l in resultados if p.get("descubierto")]
    lineas = [f"📋 Vigilante PS5 ({lugar()}): {len(resultados)} consolas y packs vigilados"]
    for tienda in sorted(pausadas or []):
        lineas.append(f"⏸ {tienda}: bloquea las consultas desde {lugar()}; la reintento cada "
                      f"{PAUSA_TIENDA_BLOQUEADA_MIN} min")
    if fijos:
        lineas.append("\nFichas fijas:")
        for prod, l in fijos:
            lineas.append(f"{icono(prod, l)} {prod['tienda']} · {prod['nombre']}: {descripcion(prod, l)}")
    if info_listados:
        lineas.append("\nListados (detecta packs nuevos solos):")
        por_listado = defaultdict(list)
        for p, l in descubiertos:
            por_listado[p.get("listado")].append((p, l))
        for lst, n_items, detalle in info_listados:
            grupo = por_listado.get(lst["url"], [])
            if not n_items:
                marca = "❔" if lst.get("experimental") else "⚠️"
                lineas.append(f"{marca} {lst['tienda']}: no se pudo leer el listado ({detalle})")
                continue
            cuenta = defaultdict(int)
            for p, l in grupo:
                cuenta[icono(p, l)] += 1
            detalle_cuenta = " · ".join(f"{k} {v}" for k, v in sorted(cuenta.items())) or "repetidos en otro listado"
            lineas.append(f"🔎 {lst['tienda']}: {n_items} consolas/packs ({detalle_cuenta})")
        for p, l in descubiertos:
            if l.estado == EN_STOCK:
                lineas.append(f"   {icono(p, l)} {p['nombre']}: {descripcion(p, l)}")
    vigiladas = sum(1 for _, l in resultados if l.estado in (EN_STOCK, AGOTADO, COLA))
    lineas.append(f"\nLeyendo bien {vigiladas} de {len(resultados)}. "
                  "🟢 en stock a tu precio · 🟠 en stock pero caro · 🔴 agotado · ⚠️/❔ no se pudo leer. "
                  "Te aviso cuando alguno pase a 🟢.")
    return "\n".join(lineas)


def escribir_resumen_github(resultados: list[tuple[dict, Lectura]]) -> None:
    ruta = os.getenv("GITHUB_STEP_SUMMARY")
    if not ruta:
        return
    filas = ["| | Tienda | Producto | Máximo | Estado | Método |", "|---|---|---|---|---|---|"]
    for prod, l in resultados:
        nombre = prod["nombre"].replace("|", "/")
        filas.append(f"| {icono(prod, l)} | {prod['tienda']} | [{nombre}]({prod['url']}) | "
                     f"{euros(float(prod['precio_max']))} | {descripcion(prod, l).replace('|', '/')} | {l.metodo} |")
    with open(ruta, "a", encoding="utf-8") as f:
        f.write("\n".join(filas) + "\n")


# --------------------------------------------------------------------------------------
# Programa
# --------------------------------------------------------------------------------------

def _url_valida(u) -> bool:
    return str(u or "").startswith("http")


def cargar_config() -> dict:
    datos = json.loads(RUTA_PRODUCTOS.read_text(encoding="utf-8"))
    productos = []
    for p in datos.get("productos", []):
        if not p.get("activo", True):
            continue
        if not _url_valida(p.get("url")):
            log(f"  (salto {p.get('tienda')} · {p.get('nombre')}: falta la URL)")
            continue
        p.setdefault("precio_max", 10**9)
        productos.append(p)
    listados = [l for l in datos.get("listados", []) if l.get("activo", True) and _url_valida(l.get("url"))]
    reglas = {**REGLAS_POR_DEFECTO, **datos.get("reglas_packs", {})}
    return {"productos": productos, "listados": listados, "reglas": reglas}


def cargar_estado() -> dict:
    try:
        return json.loads(RUTA_ESTADO.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def guardar_estado(estado: dict) -> None:
    RUTA_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def productos_descubiertos(lst: dict, items: list, reglas: dict) -> list[dict]:
    salida = []
    for url, titulo in items:
        calculo = precio_max_pack(titulo, reglas)
        if calculo is None:
            continue
        precio_max, regla = calculo
        nombre = re.sub(r"\s+", " ", titulo).strip()
        salida.append({
            "tienda": lst["tienda"], "nombre": nombre[:90] + ("…" if len(nombre) > 90 else ""), "url": url,
            "precio_max": precio_max, "regla": regla, "vendedor": lst.get("vendedor", ""),
            "modo": lst.get("modo_productos", "http"), "fuente": lst.get("fuente_productos", "auto"),
            "reintentar_con_navegador": lst.get("navegador_en_fichas", True), "descubierto": True,
            "listado": lst["url"],
        })
    return salida


def pasada(args, nav: Optional[Navegador], avisar=enviar_aviso, pausa=(1.5, 4.0)) -> list:
    cfg = cargar_config()
    primera_vez = not RUTA_ESTADO.exists()
    estado = cargar_estado()

    # Tiendas que nos tienen bloqueados: se saltan hasta su siguiente reintento
    pausadas = set() if args.probar else tiendas_en_pausa(estado)
    if pausadas:
        log(f"[{ahora_iso()}] En pausa por bloqueo: {', '.join(sorted(pausadas))}")
    listados = [l for l in cfg["listados"] if l["tienda"] not in pausadas]

    # 1) Listados -> consolas y packs descubiertos
    productos = [p for p in cfg["productos"] if p["tienda"] not in pausadas]
    vistas = {limpiar_url(p["url"]) for p in productos}
    info_listados = []
    if listados:
        log(f"[{ahora_iso()}] Leyendo {len(listados)} listados")
        encontrados = descubrir_listados(listados, nav)
        for lst in listados:
            items, detalle = encontrados.get(lst["url"], ([], "sin respuesta"))
            usados = items if args.probar else procesar_listado(lst, items, detalle, estado, None)
            info_listados.append((lst, len(items), detalle))
            log(f"  🔎 {lst['tienda']:<15} {len(items)} consolas/packs en el listado"
                + (f"  ({detalle})" if detalle else ""))
            for prod in productos_descubiertos(lst, usados, cfg["reglas"]):
                clave = limpiar_url(prod["url"])
                if clave not in vistas:
                    vistas.add(clave)
                    productos.append(prod)

    # 2) Fichas
    log(f"[{ahora_iso()}] Revisando {len(productos)} fichas")
    resultados = revisar_todos(productos, nav, pausa)
    for prod, l in resultados:
        log(f"  {icono(prod, l)} {prod['tienda']:<15} {prod['nombre'][:60]:<60} {descripcion(prod, l)}  [{l.metodo}]")

    if not args.probar:
        # 3) Avisos: primero por tienda (una tienda entera bloqueada = un solo aviso), luego por ficha
        caidas = actualizar_tiendas(resultados, info_listados, estado, avisar)
        for prod, l in resultados:
            procesar_lectura(prod, l, estado, avisar, silenciar_fallos=prod["tienda"] in caidas)
        for lst, n, _ in info_listados:
            if not n and lst["tienda"] not in caidas:
                avisar_listado_caido(lst, estado, avisar)
        if primera_vez or args.resumen:
            avisar(resumen(resultados, info_listados, pausadas), titulo="Vigilante PS5: resumen")
        guardar_estado(estado)
    else:
        log("\n" + resumen(resultados, info_listados, pausadas))
    escribir_resumen_github(resultados)
    return resultados


def volcar(url: str) -> int:
    """Guarda lo que ve el script en una ficha (http y navegador) para poder ajustarlo."""
    print(f"Descargando {url}")
    try:
        status, html, final = descargar_http(url)
        (BASE / "volcado_http.html").write_text(html, encoding="utf-8")
        print(f"  http: HTTP {status}, {len(html)} caracteres -> volcado_http.html")
        print(f"  lectura http: {analizar(status, html, final, {'url': url})}")
    except Exception as e:  # noqa: BLE001
        print(f"  http falló: {e}")
    nav = Navegador()
    if nav.arrancar():
        try:
            status, html, texto, final = nav.descargar(url)
            (BASE / "volcado_navegador.html").write_text(html, encoding="utf-8")
            (BASE / "volcado_navegador.txt").write_text(texto, encoding="utf-8")
            print(f"  navegador: HTTP {status} -> volcado_navegador.html y volcado_navegador.txt")
            print(f"  lectura navegador: {analizar(status, html, final, {'url': url}, texto, 'navegador')}")
        except Exception as e:  # noqa: BLE001
            print(f"  navegador falló: {e}")
        finally:
            nav.cerrar()
    print("\nMándale a Claude los archivos volcado_* que se han creado en esta carpeta.")
    return 0


RUTA_AVISOS_LOCAL = BASE / "avisos_local.txt"


def cargar_avisos_local() -> None:
    """En tu PC, lee el token y el chat ID de avisos_local.txt (ese archivo NO se sube a GitHub)."""
    if not RUTA_AVISOS_LOCAL.exists():
        return
    for linea in RUTA_AVISOS_LOCAL.read_text(encoding="utf-8").splitlines():
        if "=" in linea and not linea.strip().startswith("#"):
            clave, valor = linea.split("=", 1)
            if valor.strip():
                os.environ.setdefault(clave.strip(), valor.strip())


def configurar() -> int:
    print("Configuración de avisos para el vigilante en este ordenador.")
    print("Se guarda en avisos_local.txt. NO subas ese archivo a GitHub: lleva el token de tu bot.\n")
    token = input("Token del bot (el que te dio @BotFather): ").strip()
    chat = input("Tu chat ID (el número que te dio @userinfobot): ").strip()
    RUTA_AVISOS_LOCAL.write_text(f"TELEGRAM_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\n", encoding="utf-8")
    os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = token, chat
    ok = enviar_aviso("✅ Vigilante PS5 configurado en tu PC: los avisos te llegan bien.")
    print("\nTe he mandado un Telegram de prueba." if ok else
          "\nNo se pudo mandar el Telegram (mira el motivo de arriba). Para repetir: borra avisos_local.txt "
          "y vuelve a abrir vigilar_en_pc.bat.")
    return 0 if ok else 1


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Vigilante de stock de PS5")
    ap.add_argument("--probar", action="store_true", help="revisar sin avisar ni guardar estado")
    ap.add_argument("--probar-aviso", action="store_true", help="mandar un aviso de prueba y salir")
    ap.add_argument("--resumen", action="store_true", help="mandar resumen del estado de todo")
    ap.add_argument("--bucle", type=int, metavar="SEGUNDOS", help="repetir cada N segundos")
    ap.add_argument("--sin-navegador", action="store_true", help="no usar Playwright")
    ap.add_argument("--configurar", action="store_true", help="guardar token y chat ID para usarlo en tu PC")
    ap.add_argument("--volcar", metavar="URL", help="guardar lo que ve el script en esa ficha (para ajustarlo)")
    args = ap.parse_args(argv)

    if args.configurar:
        return configurar()
    if args.volcar:
        return volcar(args.volcar)
    cargar_avisos_local()

    if args.probar_aviso:
        if not ((os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) or os.getenv("NTFY_TOPIC")):
            print("No hay canal de aviso: define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID, o NTFY_TOPIC.")
            return 1
        ok = enviar_aviso("✅ Prueba del vigilante de PS5: los avisos te llegan bien.", titulo="Vigilante PS5")
        print("Aviso enviado." if ok else "No se pudo enviar el aviso (mira el error de arriba).")
        return 0 if ok else 1

    nav = None if args.sin_navegador else Navegador()
    try:
        if not args.bucle:
            pasada(args, nav)
            return 0
        while True:
            pasada(args, nav)
            args.resumen = False
            espera = args.bucle * random.uniform(0.8, 1.2)
            log(f"  ...siguiente revisión en {espera:.0f} s (Ctrl+C para parar)")
            time.sleep(espera)
    except KeyboardInterrupt:
        print("Parado.")
        return 0
    finally:
        if nav:
            nav.cerrar()


if __name__ == "__main__":
    sys.exit(main())
