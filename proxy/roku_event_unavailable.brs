' Moved: copy proxy/roku/* into the Swiftstv channel.
'   components/ResolveHlsTask.xml
'   components/ResolveHlsTask.brs
'   (paste PlayHls.brs into your Video scene)
'
' Playback path:
'   1) Task hits /v1/play.m3u8, reads 302 Location (po..../mono.m3u8)
'   2) Video ContentNode gets that URL + Referer / User-Agent headers

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
