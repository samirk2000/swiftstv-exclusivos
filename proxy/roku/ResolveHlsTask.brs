' Task: resolve PROXY /v1/play.m3u8 302 Location before Video.play.
' Copy next to this XML as components/ResolveHlsTask.brs (and .xml).
' roUrlTransfer must run on a Task thread, not the render thread.

function init() as Void
    m.top.functionName = "RunResolve"
end function

function RunResolve() as Void
    proxyUrl = m.top.proxyUrl
    result = ResolveProxyPlayUrl(proxyUrl)
    m.top.statusCode = result.code
    m.top.playUrl = result.url
    m.top.errorMessage = result.error
    m.top.done = true
end function

function ResolveProxyPlayUrl(proxyUrl as String) as Object
    result = { url: "", code: 0, error: "" }
    if proxyUrl = invalid or Len(proxyUrl) = 0 then
        result.code = 400
        result.error = "URL vacía"
        return result
    end if

    current = proxyUrl
    hop = 0
    while hop < 5
        hop = hop + 1
        ev = HttpExchange(current, hop = 1)
        if ev = invalid then
            result.code = 0
            result.error = "Timeout al resolver el stream"
            return result
        end if

        code = ev.GetResponseCode()
        result.code = code
        location = HeaderCI(ev.GetResponseHeaders(), "Location")
        body = ev.GetString()

        if code = 404 then
            result.error = "Evento no disponible o aún no inicia"
            return result
        end if

        if hop = 1 and code >= 200 and code < 300 then
            result.url = proxyUrl
            result.code = 200
            return result
        end if

        nextUrl = ""
        if Len(location) > 0 then
            nextUrl = AbsoluteUrl(current, location)
        end if

        if Len(nextUrl) > 0 and IsPlayableHlsUrl(nextUrl) then
            result.url = nextUrl
            return result
        end if

        if Len(nextUrl) > 0 and (code = 301 or code = 302 or code = 303 or code = 307 or code = 308) then
            current = nextUrl
        else
            parsed = ParseJson(body)
            if parsed <> invalid and parsed.m3u8 <> invalid and Len(parsed.m3u8) > 0 then
                result.url = parsed.m3u8
                result.code = 200
                return result
            end if

            if code >= 200 and code < 300 and IsPlayableHlsUrl(current) then
                result.url = current
                return result
            end if

            fallback = ResolveJsonEndpoint(proxyUrl)
            if Len(fallback) > 0 then
                result.url = fallback
                result.code = 200
                return result
            end if

            result.error = "Evento no disponible o aún no inicia"
            return result
        end if
    end while

    if IsPlayableHlsUrl(current) then
        result.url = current
        return result
    end if
    result.error = "Evento no disponible o aún no inicia"
    return result
end function

function HttpExchange(url as String, preferHead as Boolean) as Object
    port = CreateObject("roMessagePort")
    xfer = CreateObject("roUrlTransfer")
    xfer.SetCertificatesFile("common:/certs/ca-bundle.crt")
    xfer.InitClientCertificates()
    xfer.SetPort(port)
    xfer.EnableEncodings(true)
    xfer.RetainBodyOnError(true)
    xfer.AddHeader("User-Agent", HlsUserAgent())
    xfer.AddHeader("Referer", HlsReferer())
    xfer.SetUrl(url)

    started = false
    if preferHead then
        started = xfer.AsyncHead()
    end if
    if not started then
        started = xfer.AsyncGetToString()
    end if
    if not started then
        return invalid
    end if

    ' Playwright + cold Render can take ~30s; keep a hard cap.
    msg = wait(60000, port)
    if type(msg) <> "roUrlEvent" then
        return invalid
    end if
    return msg
end function

function ResolveJsonEndpoint(playUrl as String) as String
    jsonUrl = playUrl
    jsonUrl = jsonUrl.Replace("/v1/play.m3u8", "/v1/resolve")
    if jsonUrl = playUrl then
        return ""
    end if
    ev = HttpExchange(jsonUrl, false)
    if ev = invalid then
        return ""
    end if
    if ev.GetResponseCode() <> 200 then
        return ""
    end if
    parsed = ParseJson(ev.GetString())
    if parsed = invalid or parsed.m3u8 = invalid then
        return ""
    end if
    return parsed.m3u8
end function

function IsPlayableHlsUrl(url as String) as Boolean
    if url = invalid or Len(url) = 0 then
        return false
    end if
    lower = LCase(url)
    if Instr(1, lower, "/v1/play.m3u8") > 0 then
        return false
    end if
    return Instr(1, lower, ".m3u8") > 0
end function

function HeaderCI(headers as Object, name as String) as String
    if headers = invalid then
        return ""
    end if
    want = LCase(name)
    for each key in headers
        if LCase(key) = want then
            value = headers[key]
            if value = invalid then
                return ""
            end if
            return value
        end if
    end for
    return ""
end function

function AbsoluteUrl(baseUrl as String, location as String) as String
    if location = invalid or Len(location) = 0 then
        return ""
    end if
    lower = LCase(location)
    if Left(lower, 7) = "http://" or Left(lower, 8) = "https://" then
        return location
    end if
    schemeEnd = Instr(1, baseUrl, "://")
    if schemeEnd = 0 then
        return location
    end if
    pathStart = Instr(schemeEnd + 3, baseUrl, "/")
    if pathStart = 0 then
        origin = baseUrl
    else
        origin = Left(baseUrl, pathStart - 1)
    end if
    if Left(location, 1) = "/" then
        return origin + location
    end if
    return origin + "/" + location
end function

function HlsReferer() as String
    return "https://tudeporteshoy.xyz/"
end function

function HlsUserAgent() as String
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
end function
