# Exclusivos Swiftstv

Lista remota de canales exclusivos. El canal de Roku la descarga al abrir **TV EN VIVO**.

## Como editar

1. Abre `exclusive_sources.json` en este repo.
2. Agrega canales en `sources` y haz commit a `main`.
3. No hace falta reinstalar la app. Entra de nuevo a TV EN VIVO (o cierra y abre la carpeta **EXCLUSIVOS SWIFTSTV**).

## Ejemplo

```json
{
  "module_name": "Exclusivos Swiftstv",
  "user_agent": "VLC/3.0.18 LibVLC/3.0.18",
  "sources": [
    {
      "id": "deportes1",
      "name": "Deportes Exclusivo",
      "logo": "",
      "type": "direct",
      "urls": [
        "https://tu-cdn/canal.m3u8",
        "https://tu-espejo/canal.m3u8"
      ]
    }
  ]
}
```

Tipos: `direct` (URL .m3u8), `m3u` (lista M3U), `json` (array de canales), `extract` (pagina de la que se saca el .m3u8).

URL que lee el Roku:

https://raw.githubusercontent.com/samirk2000/swiftstv-exclusivos/main/exclusive_sources.json
