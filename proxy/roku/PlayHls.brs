' Paste into the Video scene that plays exclusive_sources.json proxy URLs.
' Point Video.content.url at /v1/play.m3u8?... (HTTP 200 playlist pipe).

function HlsReferer() as String
    return "https://tudeporteshoy.xyz/"
end function

function HlsUserAgent() as String
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
end function

function HlsHttpHeaders() as Object
    return [
        "Referer: https://tudeporteshoy.xyz/",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ]
end function

function AppendQueryParam(url as String, key as String, value as String) as String
    if url = invalid or Len(url) = 0 or value = invalid or Len(value) = 0 then
        return url
    end if
    needle = LCase(key) + "="
    if Instr(1, LCase(url), needle) > 0 then
        return url
    end if
    sep = "?"
    if Instr(1, url, "?") > 0 then
        sep = "&"
    end if
    return url + sep + key + "=" + value
end function

function ProxySubscriberId() as String
    ' Use the logged-in account token when you have one. ChannelClientId is
    ' per-device and will not stop the same URL being opened on another box.
    if m.userToken <> invalid and Len(m.userToken) > 0 then
        return m.userToken
    end if
    di = CreateObject("roDeviceInfo")
    return di.GetChannelClientId()
end function

function ProxyDeviceId() as String
    di = CreateObject("roDeviceInfo")
    return di.GetChannelClientId()
end function

function WithProxySession(url as String) as String
    out = AppendQueryParam(url, "sid", ProxySubscriberId())
    return AppendQueryParam(out, "did", ProxyDeviceId())
end function

sub PlayProxyOrHls(proxyOrManifestUrl as String)
    if m.loadingSpinner <> invalid then m.loadingSpinner.visible = true
    ' /v1/play.m3u8 now returns HTTP 200 with the rewritten playlist. Keep the
    ' Video node on the proxy URL so live reloads stay on Render, not the CDN.
    StartVideoWithHeaders(WithProxySession(proxyOrManifestUrl))
end sub

sub OnProxyHlsResolved()
    task = m.resolveTask
    code = 0
    playUrl = ""
    err = ""
    if task <> invalid then
        code = task.statusCode
        playUrl = task.playUrl
        err = task.errorMessage
        task.unobserveField("done")
    end if
    m.resolveTask = invalid

    if code = 404 or playUrl = invalid or Len(playUrl) = 0 then
        if m.loadingSpinner <> invalid then m.loadingSpinner.visible = false
        if m.video <> invalid then
            m.video.control = "stop"
            m.video.visible = false
        end if
        ShowEventUnavailableDialog(err)
        return
    end if

    StartVideoWithHeaders(WithProxySession(playUrl))
end sub

sub StartVideoWithHeaders(manifestUrl as String)
    headers = HlsHttpHeaders()
    content = CreateObject("roSGNode", "ContentNode")
    content.url = manifestUrl
    content.streamFormat = "hls"
    content.live = true
    content.HttpHeaders = headers
    if not content.hasField("HttpHeader") then
        content.addField("HttpHeader", "array", false)
    end if
    content.HttpHeader = headers

    if m.video <> invalid then
        ' ifHttpAgent on the Video node: sent on playlist + segment requests.
        m.video.SetCertificatesFile("common:/certs/ca-bundle.crt")
        m.video.InitClientCertificates()
        m.video.SetHeaders({
            "Referer": HlsReferer(),
            "User-Agent": HlsUserAgent()
        })
        m.video.visible = true
        m.video.content = content
        m.video.control = "play"
        m.video.observeField("state", "OnVideoStateChange")
    end if
end sub

function IsDirectHlsUrl(url as String) as Boolean
    if url = invalid or Len(url) = 0 then
        return false
    end if
    lower = LCase(url)
    if Instr(1, lower, "/v1/play.m3u8") > 0 then
        return false
    end if
    return Instr(1, lower, ".m3u8") > 0
end function

function ShowEventUnavailableDialog(message as String) as Void
    dialog = CreateObject("roSGNode", "Dialog")
    dialog.title = "Swiftstv"
    if message = invalid or Len(message) = 0 then
        dialog.message = "Evento no disponible o aún no inicia"
    else
        dialog.message = message
    end if
    dialog.buttons = ["OK"]
    if m.top <> invalid and m.top.getScene() <> invalid then
        m.top.getScene().dialog = dialog
    end if
end function

function OnVideoStateChange() as Void
    if m.video = invalid then
        return
    end if
    state = m.video.state
    if state = "playing" or state = "buffering" then
        if m.loadingSpinner <> invalid then m.loadingSpinner.visible = false
        return
    end if
    if state = "error" then
        if m.loadingSpinner <> invalid then m.loadingSpinner.visible = false
        ShowEventUnavailableDialog("Evento no disponible o aún no inicia")
    end if
end function
