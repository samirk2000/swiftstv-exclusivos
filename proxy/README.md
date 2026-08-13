# HLS Proxy (Render + optional Vercel front door)

Los manifests `.m3u8` de sitios como `po.tudeporteshoy.xyz` llevan `ip=` y `token=` ligados a la IP del cliente. Un `.m3u8` generado en GitHub Actions no sirve en el Roku del usuario.

Este proxy resuelve el manifest **en el momento de la reproducción**, usando la IP real del dispositivo.

## Arquitectura

```
Roku  →  PROXY (/v1/play.m3u8?embed=...)  →  Playwright abre embed  →  captura .m3u8 con IP del Roku
```

Ejemplo de manifest objetivo:

```
https://po.tudeporteshoy.xyz/mls2es/tracks-v1a1/mono.m3u8?ip=189.180.44.54&token=...
```

El proxy:

1. Lee la IP del cliente desde `X-Forwarded-For` / `CF-Connecting-IP`
2. Abre la subpágina embed con Playwright simulando esa IP en **todas** las peticiones
3. Intercepta el `.m3u8` de red
4. Responde con redirect `302` al manifest final

## Despliegue en Render (recomendado)

1. Sube este repo a GitHub.
2. En [Render](https://render.com) → **New +** → **Blueprint** o **Web Service**.
3. Conecta el repo y usa:
   - **Root directory**: `proxy`
   - **Dockerfile path**: `Dockerfile`
4. Variables de entorno:
   - `HLS_WAIT_MS=12000`
   - `PROXY_API_KEY=` (opcional, recomendado)
5. Tras el deploy obtienes una URL como:
   `https://swiftstv-hls-proxy.onrender.com`

### Probar

```bash
curl -I "https://TU-PROXY.onrender.com/v1/play.m3u8?embed=https%3A%2F%2Ffutbollibre.mx%2Fen-vivo%2Fespn-1&referer=https%3A%2F%2Ffutbollibre.mx%2F"
```

Deberías ver `HTTP/2 302` con `Location: https://...m3u8?ip=...&token=...`.

JSON alternativo:

```bash
curl "https://TU-PROXY.onrender.com/v1/resolve?embed=...&referer=..."
```

## Vercel (opcional, solo como puerta de entrada)

Vercel **no ejecuta Playwright** de forma fiable. Usa Vercel solo para reenviar tráfico al backend de Render:

1. Importa la carpeta `proxy/` como proyecto Vercel.
2. Variables:
   - `RENDER_PROXY_URL=https://swiftstv-hls-proxy.onrender.com`
   - `PROXY_API_KEY=` (misma clave que en Render)
3. La app Roku puede usar `https://tu-proyecto.vercel.app/v1/play.m3u8?...` (rewrite hacia Render).

## Integración con el scraper

En GitHub Actions (secret o env):

```yaml
PROXY_BASE_URL: https://swiftstv-hls-proxy.onrender.com
```

Con `PROXY_BASE_URL` configurado:

- El scraper **no** intenta guardar `.m3u8` en CI
- Guarda URLs proxy en `exclusive_sources.json`:

```json
{
  "id": "espn-1",
  "name": "ESPN 1",
  "type": "hls",
  "urls": [
    "https://swiftstv-hls-proxy.onrender.com/v1/play.m3u8?embed=https%3A%2F%2Ffutbollibre.mx%2Fen-vivo%2Fespn-1&referer=https%3A%2F%2Ffutbollibre.mx%2F"
  ]
}
```

El Roku abre esa URL → el proxy resuelve el `.m3u8` con la IP del Roku.

## Endpoints

| Ruta | Uso |
|------|-----|
| `GET /health` | Health check |
| `GET /v1/play.m3u8?embed=&referer=` | Redirect 302 al manifest (para Roku) |
| `GET /v1/resolve?embed=&referer=` | JSON `{ m3u8, client_ip, urls }` |

Header opcional: `X-Proxy-Key: <PROXY_API_KEY>`

## Notas

- Cache en memoria ~45 s por `(embed, referer, ip)` para no saturar el origen.
- Si el CDN firma el token con la IP del servidor y no respeta `X-Forwarded-For`, habría que localizar la API de token del reproductor; el proxy ya reescribe `ip=` cuando aparece en la URL capturada.
- Render **Starter** puede dormir el servicio; el primer play puede tardar ~30 s.
