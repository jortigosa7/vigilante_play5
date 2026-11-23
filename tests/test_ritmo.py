import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vigilar as v  # noqa: E402


def ld(disp, precio):
    return ('<script type="application/ld+json">' + json.dumps(
        {"@type": "Product", "offers": {"@type": "Offer", "availability": f"https://schema.org/{disp}",
                                        "price": precio}}) + "</script>")


def test_cada_minutos_reduce_peticiones(tmp_path, monkeypatch):
    listado = "https://www.tienda.es/consolas-ps5"
    config = {
        "productos": [
            {"tienda": "Rapida", "nombre": "PS5", "url": "https://rapida.es/ps5", "precio_max": 600},
            {"tienda": "Lenta", "nombre": "PS5", "url": "https://lenta.es/ps5", "precio_max": 600, "cada_minutos": 6},
        ],
        "listados": [{"tienda": "Tienda", "url": listado, "cada_minutos": 15, "cada_minutos_fichas": 10}],
    }
    (tmp_path / "productos.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(v, "RUTA_PRODUCTOS", tmp_path / "productos.json")
    monkeypatch.setattr(v, "RUTA_ESTADO", tmp_path / "estado.json")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    paginas = {
        "https://rapida.es/ps5": ld("OutOfStock", "599"),
        "https://lenta.es/ps5": ld("OutOfStock", "599"),
        listado: '<a href="/ps5-pack">Consola PlayStation 5 Digital 825GB</a>',
        "https://www.tienda.es/ps5-pack": ld("OutOfStock", "599"),
    }
    pedidas = []

    def falso_http(url, timeout=25):
        pedidas.append(url)
        return 200, paginas[url], url

    monkeypatch.setattr(v, "descargar_http", falso_http)
    avisos = []
    args = SimpleNamespace(probar=False, resumen=False)

    v.pasada(args, None, avisar=lambda *a, **k: avisos.append(a) or True, pausa=None)
    assert sorted(pedidas) == sorted(paginas)                     # primera pasada: todo

    pedidas.clear()
    v.pasada(args, None, avisar=lambda *a, **k: True, pausa=None)
    assert pedidas == ["https://rapida.es/ps5"]                   # segunda: solo lo que va en cada pasada

    # pasa el tiempo: vuelve a tocar todo
    estado = v.cargar_estado()
    for clave, e in list(estado.items()) + list(estado["_listados"].items()):
        if isinstance(e, dict) and "proxima" in e:
            e["proxima"] = 0
    v.guardar_estado(estado)
    pedidas.clear()
    v.pasada(args, None, avisar=lambda *a, **k: True, pausa=None)
    assert sorted(pedidas) == sorted(paginas)


def test_mientras_hay_stock_se_revisa_en_cada_pasada(tmp_path, monkeypatch):
    config = {"productos": [{"tienda": "MediaMarkt", "nombre": "PS5", "url": "https://mm.es/ps5",
                             "precio_max": 650, "cada_minutos": 6}]}
    (tmp_path / "productos.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(v, "RUTA_PRODUCTOS", tmp_path / "productos.json")
    monkeypatch.setattr(v, "RUTA_ESTADO", tmp_path / "estado.json")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    pagina = {"html": ld("InStock", "649")}
    pedidas = []

    def falso_http(url, timeout=25):
        pedidas.append(url)
        return 200, pagina["html"], url

    monkeypatch.setattr(v, "descargar_http", falso_http)
    avisos = []
    args = SimpleNamespace(probar=False, resumen=False)
    v.pasada(args, None, avisar=lambda texto, **k: avisos.append(texto) or True, pausa=None)
    assert "EN STOCK" in avisos[0]               # (la primera vez también manda un resumen)

    pagina["html"] = ld("OutOfStock", "649")
    v.pasada(args, None, avisar=lambda texto, **k: avisos.append(texto) or True, pausa=None)
    assert len(pedidas) == 2                      # no esperó sus 6 minutos: estaba en stock
    assert "Se ha vuelto a agotar" in avisos[-1]

    v.pasada(args, None, avisar=lambda texto, **k: avisos.append(texto) or True, pausa=None)
    assert len(pedidas) == 2                      # ya agotada: vuelve a su ritmo normal
