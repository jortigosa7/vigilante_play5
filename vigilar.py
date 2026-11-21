#!/usr/bin/env python3
"""
Vigilante de stock de PS5.

Revisa las fichas de producto de productos.json y te avisa por Telegram y/o ntfy
cuando un producto pasa a estar EN STOCK a un precio igual o menor que su precio_max.

Uso:
  python vigilar.py                 una pasada (lo que ejecuta GitHub Actions)
  python vigilar.py --probar        una pasada sin avisar ni guardar: imprime lo que detecta en cada tienda
  python vigilar.py --probar-aviso  manda un mensaje de prueba para comprobar Telegram/ntfy
  python vigilar.py --resumen       además de vigilar, manda un resumen del estado de todas las tiendas
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
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

BASE = Path(__file__).resolve().parent
RUTA_PRODUCTOS = BASE / "productos.json"
RUTA_ESTADO = BASE / "estado.json"

# Tras cuántos fallos seguidos en un producto te aviso de que esa tienda no se está pudiendo vigilar
UMBRAL_FALLOS = 6

EN_STOCK = "en_stock"
AGOTADO = "agotado"
COLA = "cola"            # cola virtual (Queue-it): suele significar reposición en Sony
DESCONOCIDO = "desconocido"
ERROR = "error"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CABECERAS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
}


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
    return re.sub(r"\s+", " ", htmlmod.unescape(texto))


# --------------------------------------------------------------------------------------
# Lectura de la página: JSON-LD (schema.org) -> metaetiquetas -> texto visible
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
            principal = n.get("mainEntity")
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
                   "verify you are human", "are you a robot", "acceso denegado"]


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


def analizar(status: int, html: str, url_final: str, prod: dict,
             texto_visible: Optional[str] = None, modo: str = "http") -> Lectura:
    if es_cola(url_final, html, texto_visible):
        return Lectura(COLA, metodo=modo, detalle="cola virtual activa")

    previa: Optional[Lectura] = None
    if prod.get("fuente", "auto") != "texto":
        previa = leer_ldjson(html, prod) or leer_meta(html)
        if previa and previa.estado in (EN_STOCK, AGOTADO):
            previa.metodo = f"{modo}/{previa.metodo}"
            return previa

    if texto_visible is not None or prod.get("fuente") == "texto":
        lectura = leer_texto(texto_visible if texto_visible is not None else html_a_texto(html), prod)
        if lectura.estado in (EN_STOCK, AGOTADO):
            if lectura.precio is None and previa is not None:
                lectura.precio = previa.precio
            lectura.metodo = f"{modo}/{lectura.metodo}"
            return lectura

    if status == 404:
        return Lectura(ERROR, metodo=modo, detalle="la página no existe (404): revisa la URL")
    if status >= 400 or parece_bloqueo(html):
        return Lectura(ERROR, metodo=modo, detalle=f"la tienda bloquea la consulta (HTTP {status})")
    detalle = (previa.detalle if previa and previa.detalle else "no encuentro el stock en la página")
    return Lectura(DESCONOCIDO, previa.precio if previa else None, metodo=modo, detalle=detalle)


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

    def descargar(self, url: str, timeout: int = 45) -> tuple[int, str, str, str]:
        if not self.arrancar():
            raise RuntimeError(f"navegador no disponible ({self.motivo})")
        ctx = self._browser.new_context(locale="es-ES", user_agent=UA,
                                        viewport={"width": 1366, "height": 900})
        pagina = ctx.new_page()
        try:
            resp = pagina.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                pagina.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            pagina.wait_for_timeout(2500)
            return (resp.status if resp else 0), pagina.content(), pagina.inner_text("body"), pagina.url
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


def revisar(prod: dict, nav: Optional[Navegador]) -> Lectura:
    url = prod["url"]
    try:
        if prod.get("modo") == "navegador":
            if nav is None:
                return Lectura(ERROR, metodo="navegador", detalle="navegador desactivado")
            status, html, texto, final = nav.descargar(url)
            return analizar(status, html, final, prod, texto, "navegador")

        status, html, final = descargar_http(url)
        lectura = analizar(status, html, final, prod, None, "http")
        if (lectura.estado in (ERROR, DESCONOCIDO) and nav is not None
                and prod.get("reintentar_con_navegador", True) and status != 404 and nav.arrancar()):
            try:
                status2, html2, texto2, final2 = nav.descargar(url)
                segunda = analizar(status2, html2, final2, prod, texto2, "navegador")
                if segunda.estado in (EN_STOCK, AGOTADO, COLA):
                    return segunda
            except Exception:  # noqa: BLE001
                pass
        return lectura
    except Exception as e:  # noqa: BLE001
        return Lectura(ERROR, metodo=prod.get("modo", "http"), detalle=f"{type(e).__name__}: {e}"[:200])


# --------------------------------------------------------------------------------------
# Avisos
# --------------------------------------------------------------------------------------

def enviar_aviso(texto: str, titulo: str = "Vigilante PS5", url: Optional[str] = None,
                 urgente: bool = False) -> bool:
    enviado = False
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        datos = urllib.parse.urlencode({"chat_id": chat, "text": texto,
                                        "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=datos, timeout=20)
            enviado = True
        except Exception as e:  # noqa: BLE001
            print(f"  ! Telegram falló: {e}")
    tema = os.getenv("NTFY_TOPIC")
    if tema:
        servidor = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        cuerpo = {"topic": tema, "title": titulo, "message": texto, "priority": 5 if urgente else 3,
                  "tags": ["video_game"]}
        if url:
            cuerpo["click"] = url
        peticion = urllib.request.Request(servidor, data=json.dumps(cuerpo).encode("utf-8"),
                                          headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(peticion, timeout=20)
            enviado = True
        except Exception as e:  # noqa: BLE001
            print(f"  ! ntfy falló: {e}")
    if not enviado:
        print(f"  [aviso sin enviar: no hay Telegram ni ntfy configurado]\n{texto}")
    return enviado


def mensaje_stock(prod: dict, l: Lectura) -> str:
    lineas = [f"🟢 PS5 EN STOCK en {prod['tienda']}",
              f"{prod['nombre']}: {euros(l.precio)} (tu máximo: {euros(float(prod['precio_max']))})"]
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
            f"Mientras tanto esa tienda NO está vigilada. Si sigue así, prueba a ejecutarlo en tu PC "
            f"(python vigilar.py --bucle 120) o revisa la URL:\n{prod['url']}")


def procesar_lectura(prod: dict, l: Lectura, estado: dict, avisar: Callable[..., bool]) -> None:
    e = estado.setdefault(prod["url"], {"ok": False, "cola": False, "fallos": 0, "aviso_fallos": False})
    precio_max = float(prod.get("precio_max") or 10**9)

    if l.estado in (ERROR, DESCONOCIDO):
        e["fallos"] = e.get("fallos", 0) + 1
        if e["fallos"] >= UMBRAL_FALLOS and not e.get("aviso_fallos"):
            avisar(mensaje_fallos(prod, l), titulo="Vigilante PS5: tienda sin vigilar", url=prod["url"])
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


def icono(prod: dict, l: Lectura) -> str:
    if l.estado == EN_STOCK:
        return "🟢" if l.precio is None or l.precio <= float(prod["precio_max"]) + 0.005 else "🟠"
    return {AGOTADO: "🔴", COLA: "🟡", ERROR: "⚠️"}.get(l.estado, "❔")


def descripcion(prod: dict, l: Lectura) -> str:
    if l.estado == EN_STOCK:
        if l.precio is not None and l.precio > float(prod["precio_max"]) + 0.005:
            return f"en stock pero a {euros(l.precio)} (por encima de tu máximo)"
        return f"EN STOCK a {euros(l.precio)}"
    if l.estado == AGOTADO:
        return "agotado" + (f" ({l.detalle})" if l.detalle else "")
    if l.estado == COLA:
        return "cola virtual activa"
    if l.estado == ERROR:
        return f"sin vigilar: {l.detalle}"
    return f"no se pudo leer: {l.detalle}"


def resumen(resultados: list[tuple[dict, Lectura]]) -> str:
    lineas = [f"📋 Vigilante PS5: estado de {len(resultados)} productos"]
    for prod, l in resultados:
        lineas.append(f"{icono(prod, l)} {prod['tienda']} · {prod['nombre']}: {descripcion(prod, l)}")
    vigiladas = sum(1 for _, l in resultados if l.estado in (EN_STOCK, AGOTADO, COLA))
    lineas.append(f"\nVigilando bien {vigiladas} de {len(resultados)}. "
                  "Te aviso cuando alguno pase a estar en stock a tu precio máximo o menos.")
    return "\n".join(lineas)


def escribir_resumen_github(resultados: list[tuple[dict, Lectura]]) -> None:
    ruta = os.getenv("GITHUB_STEP_SUMMARY")
    if not ruta:
        return
    filas = ["| | Tienda | Producto | Estado | Método |", "|---|---|---|---|---|"]
    for prod, l in resultados:
        filas.append(f"| {icono(prod, l)} | {prod['tienda']} | [{prod['nombre']}]({prod['url']}) | "
                     f"{descripcion(prod, l)} | {l.metodo} |")
    with open(ruta, "a", encoding="utf-8") as f:
        f.write("\n".join(filas) + "\n")


# --------------------------------------------------------------------------------------
# Programa
# --------------------------------------------------------------------------------------

def cargar_productos() -> list[dict]:
    datos = json.loads(RUTA_PRODUCTOS.read_text(encoding="utf-8"))
    productos = []
    for p in datos.get("productos", []):
        if not p.get("activo", True):
            continue
        if not str(p.get("url", "")).startswith("http"):
            print(f"  (salto {p.get('tienda')} · {p.get('nombre')}: falta la URL)")
            continue
        p.setdefault("precio_max", 10**9)
        productos.append(p)
    return productos


def cargar_estado() -> dict:
    try:
        return json.loads(RUTA_ESTADO.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def guardar_estado(estado: dict) -> None:
    RUTA_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def pasada(args, nav: Optional[Navegador], avisar=enviar_aviso, pausa=(1.5, 4.0)) -> list:
    productos = cargar_productos()
    primera_vez = not RUTA_ESTADO.exists()
    estado = cargar_estado()
    resultados = []
    print(f"[{ahora_iso()}] Revisando {len(productos)} productos")
    for i, prod in enumerate(productos):
        if i and pausa:
            time.sleep(random.uniform(*pausa))
        l = revisar(prod, nav)
        resultados.append((prod, l))
        print(f"  {icono(prod, l)} {prod['tienda']:<15} {prod['nombre']:<28} {descripcion(prod, l)}  [{l.metodo}]")
        if not args.probar:
            procesar_lectura(prod, l, estado, avisar)
    if not args.probar:
        if primera_vez or args.resumen:
            avisar(resumen(resultados), titulo="Vigilante PS5: resumen")
        guardar_estado(estado)
    escribir_resumen_github(resultados)
    return resultados


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Vigilante de stock de PS5")
    ap.add_argument("--probar", action="store_true", help="revisar sin avisar ni guardar estado")
    ap.add_argument("--probar-aviso", action="store_true", help="mandar un aviso de prueba y salir")
    ap.add_argument("--resumen", action="store_true", help="mandar resumen del estado de todas las tiendas")
    ap.add_argument("--bucle", type=int, metavar="SEGUNDOS", help="repetir cada N segundos")
    ap.add_argument("--sin-navegador", action="store_true", help="no usar Playwright")
    args = ap.parse_args(argv)

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
            print(f"  ...siguiente revisión en {espera:.0f} s (Ctrl+C para parar)")
            time.sleep(espera)
    except KeyboardInterrupt:
        print("Parado.")
        return 0
    finally:
        if nav:
            nav.cerrar()


if __name__ == "__main__":
    sys.exit(main())
