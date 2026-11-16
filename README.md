# ChileDeals Radar 🇨🇱📡

Plataforma 100% gratuita (costo **$0**) para detectar **errores de precio (glitches)**, **ofertas flash reales** y **alertas de Lista VIP** en el retail oficial chileno (**Falabella, Paris, Ripley y MercadoLibre Tiendas Oficiales**), enfocada en **Tecnología y Gaming**.

Funciona 24/7 en la nube usando **GitHub Actions** (sin tener tu PC encendido), envía alertas instantáneas a tu celular por **Telegram** y publica un **sitio web público gratuito en GitHub Pages** para que cualquier persona pueda ver las ofertas sin instalar nada ni usar Telegram.

---

## Cómo ponerlo en marcha en la nube en 3 pasos (Costo $0)

### Paso 1: Crear tu Bot de Telegram
1. Abre Telegram y busca a **@BotFather**.
2. Envía `/newbot`, elige un nombre y un usuario terminado en `bot` (ej. `ChileDealsRadarBot`).
3. Copia el **Token** que te entrega `@BotFather` (será tu `TELEGRAM_BOT_TOKEN`).
4. Busca tu bot en Telegram, presiona **Iniciar** (`/start`), y luego visita en tu navegador:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
   para copiar tu número de `"chat":{"id": ...}` (será tu `TELEGRAM_CHAT_ID`).

### Paso 2: Subir a GitHub y configurar los Secrets
1. Crea un repositorio en GitHub (por ejemplo `deals`) y sube este proyecto.
2. En tu repositorio de GitHub, ve a **Settings → Secrets and variables → Actions → New repository secret**.
3. Agrega estos dos Secrets:
   - `TELEGRAM_BOT_TOKEN` → El token de `@BotFather`.
   - `TELEGRAM_CHAT_ID` → Tu ID de chat.
4. *(Opcional)* Si también quieres alertas en Discord, agrega `DISCORD_WEBHOOK_URL`.
5. Actualiza `"public_web_url"` en [`config/settings.json`](config/settings.json) con la URL de tu página (Paso 3).

### Paso 3: Activar tu Página Web Pública Gratis (sin pagar dominio/DNS)
1. En tu repositorio de GitHub, ve a **Settings → Pages**.
2. En **Build and deployment → Source**, elige **Deploy from a branch**.
3. Selecciona la rama **`main`** y la carpeta **`/docs`**, y presiona **Save**.
4. En ~1 minuto GitHub te dará una URL pública y gratuita con HTTPS, por ejemplo:
   `https://<tu-usuario>.github.io/<nombre-repo>/`

Cada vez que **GitHub Actions** ejecute el radar (`.github/workflows/price_radar.yml`), actualizará `data/prices.db` y `docs/data/deals.json`, y tu web pública se actualizará sola para que cualquiera pueda revisar y compartir las ofertas.

---

## Personalización rápida

- **Lista VIP de productos específicos:** Edita [`config/watchlist.json`](config/watchlist.json) para agregar productos y el precio máximo (`max_price_clp`) al que quieres recibir alerta inmediata.
- **Umbrales de descuento y pisos de glitch:** Ajusta [`config/settings.json`](config/settings.json) (`flash_drop_pct`, `glitch_drop_pct`, `category_floor_prices_clp`).

---

## Pruebas locales

```powershell
# Ejecutar suite completa de tests
C:\Users\mamb2\AppData\Local\Programs\Orange\python.exe -m unittest discover -s tests -v

# Ejecutar simulación de 2 pasadas (puebla data/prices.db y docs/data/deals.json)
C:\Users\mamb2\AppData\Local\Programs\Orange\python.exe -m src.main --demo

# Probar conectividad real con Telegram (requiere variables de entorno definidas)
C:\Users\mamb2\AppData\Local\Programs\Orange\python.exe -m src.main --test-telegram
```
