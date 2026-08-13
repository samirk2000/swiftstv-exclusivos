' Paste into your Roku channel Video scene / task that loads PROXY /v1/play.m3u8 URLs.
' When the proxy returns 404 (event not live yet), show a clear message instead of spinning forever.

function ShowEventUnavailableDialog(message as String) as Void
    dialog = createObject("roSGNode", "Dialog")
    dialog.title = "Swiftstv"
    if message = invalid or Len(message) = 0 then
        dialog.message = "Evento no disponible o aún no inicia"
    else
        dialog.message = message
    end if
    dialog.buttons = ["OK"]
    m.top.getScene().dialog = dialog
end function

function OnProxyPlaybackHttpResponse(event as Object) as Void
    ' Wire this to your roUrlTransfer / Task Node that hits the proxy before Video.content.
    code = event.getResponseCode()
    if code = 404 then
        body = event.getString()
        detail = "Evento no disponible o aún no inicia"
        if body <> invalid and Instr(1, LCase(body), "evento no disponible") > 0 then
            detail = "Evento no disponible o aún no inicia"
        end if
        if m.video <> invalid then
            m.video.control = "stop"
            m.video.visible = false
        end if
        if m.loadingSpinner <> invalid then m.loadingSpinner.visible = false
        ShowEventUnavailableDialog(detail)
        return
    end if
end function

function OnVideoStateChange() as Void
    ' Also catch Video node failures after a bad Location / empty playlist.
    state = m.video.state
    if state = "error" or state = "finished" then
        err = m.video.errorMsg
        if err = invalid then err = ""
        if Instr(1, LCase(err), "404") > 0 or Instr(1, LCase(err), "http:") > 0 then
            if m.loadingSpinner <> invalid then m.loadingSpinner.visible = false
            ShowEventUnavailableDialog("Evento no disponible o aún no inicia")
        end if
    end if
end function
