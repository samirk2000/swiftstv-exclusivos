# HLS Proxy (Render + optional Vercel front door)

Los manifests `.m3u8` de sitios como `po.tudeporteshoy.xyz` llevan `ip=` y `token=` ligados a la IP del cliente. Un `.m3u8` generado en GitHub Actions no sirve en el Roku del usuario.

Este proxy resuelve el manifest **en el momento de la reproducción**, usando la IP real del dispositivo.

## Arquitectura

```
Android/Roku  →  GET /v1/play.m3u8
             ←  200 playlist (líneas .ts apuntan a /v1/media?u=...)
             →  GET /v1/media?u=<url CDN>
Render       →  GET CDN con User-Agent + Referer + Host
             ←  .ts / clave / sub-playlist
```

El proxy:

1. Lee la IP del cliente desde `X-Forwarded-For` / `CF-Connecting-IP`
2. Abre la subpágina embed con Playwright simulando esa IP
3. Intercepta el `.m3u8` de red (`po.tudeporteshoy.xyz/.../mono.m3u8`)
4. Baja el manifiesto desde Render inyectando `Referer`, `User-Agent` y `Host` del CDN
5. Reescribe cada URI (`.ts`, `.m3u8`, `EXT-X-KEY`) a `/v1/media?u=` **sin recodificar** `token` / `auth` / `hash`
6. Responde **200** `application/vnd.apple.mpegurl` — sin redirect 302

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
curl -sI "https://TU-PROXY.onrender.com/v1/play.m3u8?embed=...&referer=https%3A%2F%2Ftudeporteshoy.xyz%2F"
```

Deberías ver `HTTP/2 200` y `content-type: application/vnd.apple.mpegurl`. El cuerpo empieza por `#EXTM3U` y las líneas de segmentos son `/v1/media?u=https%3A%2F%2Fpo.tudeporteshoy.xyz%2F...`.

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

El nodo `Video` de Roku debe usar **directamente** la URL `/v1/play.m3u8?...` de `exclusive_sources.json` (HTTP 200, playlist ya reescrita). No hace falta seguir un 302 ni apuntar a `po.tudeporteshoy.xyz`.

## Endpoints

| Ruta | Uso |
|------|-----|
| `GET /health` | Health check |
| `GET /v1/play.m3u8?embed=&referer=` | **200** playlist HLS (pipe, sin 302) |
| `GET /v1/media?u=` | Pipe de segmentos `.ts` / sub-playlists hacia el CDN |
| `GET /v1/resolve?embed=&referer=` | JSON `{ m3u8, client_ip, urls }` |

Header opcional: `X-Proxy-Key: <PROXY_API_KEY>`

## Notas

- Cache en memoria ~45 s por `(embed, referer, ip)` para no saturar el origen.
- Si el CDN firma el token con la IP del servidor y no respeta `X-Forwarded-For`, habría que localizar la API de token del reproductor; el proxy ya reescribe `ip=` cuando aparece en la URL capturada.
- Render **Starter** puede dormir el servicio; el primer play puede tardar ~30 s.
