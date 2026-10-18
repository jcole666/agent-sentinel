' Agent Sentinel - silent launcher
' IMPORTANT: keep this file pure ASCII. Windows Script Host reads .vbs as ANSI,
' so any non-ASCII character (Chinese comments etc.) makes the script fail silently.
'
' Project dir resolution order:
'   1) user env var SENTINEL_HOME   (set for Desktop / Startup copies)
'   2) the folder this .vbs sits in (works when the .vbs is next to sentinel.py)
Dim fso, shell, dir, py, candidates, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

dir = shell.ExpandEnvironmentStrings("%SENTINEL_HOME%")
If dir = "%SENTINEL_HOME%" Then dir = fso.GetParentFolderName(WScript.ScriptFullName)

py = shell.ExpandEnvironmentStrings("%SENTINEL_PYTHON%")
If py = "%SENTINEL_PYTHON%" Then py = ""

If py = "" Then
    candidates = Array( _
        "F:\AppData\.workbuddy\binaries\python\envs\sentinel\Scripts\pythonw.exe", _
        shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\envs\sentinel\Scripts\pythonw.exe")
    For i = 0 To UBound(candidates)
        If fso.FileExists(candidates(i)) Then
            py = candidates(i)
            Exit For
        End If
    Next
End If

If py = "" Then py = "pythonw.exe"

If Not fso.FileExists(dir & "\sentinel.py") Then
    MsgBox "sentinel.py not found in " & dir, 16, "Agent Sentinel"
    WScript.Quit 1
End If

shell.CurrentDirectory = dir
shell.Run """" & py & """ """ & dir & "\sentinel.py""", 0, False
