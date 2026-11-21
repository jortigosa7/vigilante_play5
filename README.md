# Vigilante de stock PS5

Revisa cada pocos minutos la PS5 y sus packs en PcComponentes, MediaMarkt, Sony (PlayStation Direct), Carrefour y El Corte Inglés. También prueba Worten y Fnac en modo experimental. Te manda un Telegram cuando alguna pasa a estar **en stock a precio oficial o menos**. Si la vende un revendedor por encima de ese precio, no te avisa.

**Cómo encuentra los packs:** además de las fichas fijas, lee la página de "consolas PS5" de cada tienda y saca solas las consolas y los packs (con juegos, mandos, saldo...), aunque salgan nuevos. Deja fuera los accesorios, los juegos sueltos, los reacondicionados y la PS5 Pro.

**Precio máximo de cada pack:** precio oficial de la consola (599,99 € la Digital y 649,99 € la de lector) + 60 € por cada juego o mando extra + el saldo que incluya. Por ejemplo, "PS5 con lector + Lobezno + Spider-Man 2" → 649,99 + 2 × 60 = 769,99 €. Lo puedes cambiar en `reglas_packs` dentro de `productos.json`.

Qué avisos puede mandarte:

- 🟢 **En stock**: la tienda tiene la PS5 a tu precio. Te manda el enlace para comprarla.
- 🟡 **Cola virtual en Sony**: suele significar que acaba de reponer.
- ⚠️ **Tienda sin vigilar**: una tienda lleva un rato bloqueando las consultas, así que esa no se está vigilando.

La primera vez que se ejecuta te manda un **resumen** con el estado de cada tienda, para que veas cuáles se están leyendo bien.

---

## Puesta en marcha en GitHub (unos 10 minutos)

### 1. Crea el repositorio
En github.com/new, ponle de nombre `vigilante-ps5` y elige **Public**.
Tiene que ser público porque en GitHub los minutos de Actions son gratis e ilimitados solo en repos públicos. Con una ejecución cada 5 minutos, un repo privado se queda sin los minutos gratis en pocos días. En el repo no hay nada privado: el token va en *Secrets*, que no se ven.

### 2. Sube los archivos
Dentro del repo, pulsa **Add file → Upload files** y arrastra todo el contenido de la carpeta descomprimida, incluida la carpeta `.github`.
Si la carpeta `.github` no se sube, crea el archivo a mano: pulsa **Add file → Create new file**, escribe como nombre `.github/workflows/vigilar.yml` y pega dentro el contenido de ese archivo.

### 3. Configura Telegram
Puedes reutilizar el bot de StackFit, porque los mensajes te llegarán a tu chat privado y no al canal.

1. En Telegram, abre el chat con tu bot y pulsa **Iniciar** (si no lo haces, el bot no puede escribirte).
2. Escríbele a **@userinfobot** y te dará tu número de ID. Ese es tu `TELEGRAM_CHAT_ID`.
3. En el repo, ve a **Settings → Secrets and variables → Actions → New repository secret** y crea estos dos:
   - `TELEGRAM_TOKEN` → el token del bot
   - `TELEGRAM_CHAT_ID` → tu ID

> **Alternativa sin Telegram:** instala la app **ntfy**, suscríbete a un tema con un nombre difícil de adivinar (por ejemplo `ps5-javier-7k2m9x`) y crea solo el secret `NTFY_TOPIC` con ese nombre.

### 4. Arráncalo
Ve a **Actions → Vigilar stock PS5 → Run workflow**. En 2 o 3 minutos te llegará el resumen. Desde ese momento se ejecuta solo cada 5 minutos.

### 5. Lee el resumen
- 🟢 en stock a tu precio · 🟠 en stock pero caro · 🔴 agotado → se está vigilando bien.
- 🔎 Tienda: N consolas/packs → el listado de esa tienda se lee bien y encuentra N productos.
- ⚠️ "la tienda bloquea" o ❔ "no se pudo leer" → eso **no** se está vigilando desde GitHub. Pasa a veces porque algunas tiendas bloquean los servidores de GitHub. En ese caso, ejecútalo también en tu PC (más abajo).

También puedes ver el estado de cada ejecución en **Actions**: entra en una ejecución y verás una tabla con el resultado de cada tienda.

---

## Cambiar tiendas, modelos o precios
Edita `productos.json` (en GitHub: abre el archivo → icono del lápiz → *Commit changes*). Tiene tres partes:

- `productos`: fichas concretas que se vigilan siempre. `precio_max` es el precio máximo al que te avisa.
- `listados`: la página de consolas PS5 de cada tienda, de donde saca los packs. Para añadir otra tienda, copia un bloque, pega la URL de su página de consolas PS5 y ponle `activo: true`. Con `experimental: true` no te avisa si falla.
- `reglas_packs`: precios oficiales y cuánto sumar por cada juego o mando extra. Si quieres que también vigile la PS5 Pro, pon `incluir_pro: true`.

`activo: false` deja de vigilar cualquier bloque.

---

## Ejecutarlo en tu PC (para las tiendas que bloquean a GitHub)
Algunas tiendas, como PcComponentes y MediaMarkt, bloquean las consultas que llegan desde los servidores de GitHub, pero no las de una conexión de casa. Cuando pasa eso, GitHub te manda **un solo aviso por tienda**, deja de insistir con ella y la reintenta cada 30 minutos. El resto (por ejemplo Sony) sigue vigilándose en GitHub.

Para cubrir esas tiendas, ejecútalo también en tu PC:

1. Descomprime el zip en una carpeta de tu PC (por ejemplo `Documentos\vigilante-ps5`). Esta carpeta es solo para tu PC: no la subas a GitHub.
2. Haz doble clic en **`vigilar_en_pc.bat`**.
   - La primera vez instala lo necesario (tarda un par de minutos).
   - Después te pide el token del bot y tu chat ID, y te manda un Telegram de prueba. Se guardan en `avisos_local.txt`. **No subas ese archivo a GitHub**, porque el repo es público y lleva tu token.
3. Se queda vigilando cada 2 minutos y te manda un resumen de lo que ve desde tu PC. Deja la ventana abierta o minimizada.

**Para que arranque solo al encender el PC:** pulsa Win + R, escribe `shell:startup` y pulsa Enter. En la carpeta que se abre, crea un acceso directo a `vigilar_en_pc.bat` (clic derecho en el .bat → Mostrar más opciones → Crear acceso directo, y arrastra el acceso directo a esa carpeta).

Te pueden llegar avisos repetidos de lo que se vigila en los dos sitios (por ejemplo Sony). Es normal.

---

## Limitaciones
- GitHub no garantiza los 5 minutos exactos: a veces lo retrasa entre 5 y 15 minutos. Si una reposición dura menos que eso, la versión del PC la pilla mejor.
- Las tiendas pueden cambiar su web. Si un día una deja de leerse, te llega un aviso de "sin vigilar".
- Amazon no está incluido porque bloquea mucho. Para Amazon usa **Keepa**: pon una alerta de precio en la casilla "Amazon" (no en la de terceros) y te avisará cuando Amazon reponga.
