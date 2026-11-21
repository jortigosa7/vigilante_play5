# Vigilante de stock PS5

Revisa cada 5 minutos las fichas de la PS5 en PcComponentes, MediaMarkt y la tienda oficial de Sony (y Carrefour / El Corte Inglés si añades sus URLs). Te manda un Telegram cuando alguna pasa a estar **en stock a precio oficial o menos**. Si la vende un revendedor por encima de tu `precio_max`, no te avisa.

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
- 🔴 agotado / 🟢 en stock → esa tienda se está vigilando bien.
- ⚠️ "la tienda bloquea" o ❔ "no se pudo leer" → esa tienda **no** se está vigilando desde GitHub. Pasa a veces porque algunas tiendas bloquean los servidores de GitHub. En ese caso, ejecútalo también en tu PC (más abajo).

También puedes ver el estado de cada ejecución en **Actions**: entra en una ejecución y verás una tabla con el resultado de cada tienda.

---

## Cambiar tiendas, modelos o precios
Edita `productos.json` (en GitHub: abre el archivo → icono del lápiz → *Commit changes*):

- `precio_max`: solo te avisa si el precio es este o más bajo.
- `activo`: `false` para dejar de vigilar ese producto.
- **Carrefour / El Corte Inglés**: abre la ficha del producto en su web, copia la URL, pégala en el `url` del bloque correspondiente y cambia `activo` a `true`.

---

## Ejecutarlo en tu PC (más rápido, y funciona si GitHub está bloqueado)
En PowerShell, dentro de la carpeta:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
$env:TELEGRAM_TOKEN="EL_TOKEN_DE_TU_BOT"
$env:TELEGRAM_CHAT_ID="TU_ID"
python vigilar.py --probar-aviso
python vigilar.py --probar
python vigilar.py --bucle 120
```

- `--probar-aviso`: te manda un Telegram de prueba.
- `--probar`: revisa todas las tiendas y muestra lo que detecta, sin avisarte.
- `--bucle 120`: lo deja revisando cada 2 minutos hasta que pulses Ctrl+C. Para eso el PC tiene que estar encendido.

---

## Limitaciones
- GitHub no garantiza los 5 minutos exactos: a veces lo retrasa entre 5 y 15 minutos. Si una reposición dura menos que eso, la versión del PC la pilla mejor.
- Las tiendas pueden cambiar su web. Si un día una pasa a ⚠️ o ❔, te llega el aviso de "tienda sin vigilar".
- Amazon no está incluido porque bloquea mucho. Para Amazon usa **Keepa**: pon una alerta de precio en la casilla "Amazon" (no en la de terceros) y te avisará cuando Amazon reponga.
