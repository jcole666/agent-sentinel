
' Agent Sentinel - launcher
' NOTE: saved as GBK/ANSI on purpose. Windows Script Host reads .vbs as ANSI
' and the project path contains Chinese characters.
Option Explicit

Dim fso, shell, dir, py, cmd, proc, found, logfile, h2

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
logfile = fso.GetSpecialFolder(2) & "\agent-sentinel-launch.log"

Sub WriteLog(s)
    On Error Resume Next
    Dim f
    Set f = fso.OpenTextFile(logfile, 8, True)
    If Err.Number = 0 Then
        f.WriteLine Now & "  " & s
        f.Close
    End If
    Err.Clear
End Sub

Function RegEnv(name)
    On Error Resume Next
    RegEnv = shell.RegRead("HKCU\Environment\" & name)
    If Err.Number <> 0 Then RegEnv = ""
    Err.Clear
    On Error GoTo 0
End Function

' Is a sentinel instance already listening?
Function PortAlive()
    Dim h, alive
    alive = False
    On Error Resume Next
    Set h = CreateObject("WinHttp.WinHttpRequest.5.1")
    If Err.Number = 0 Then
        h.SetTimeouts 800, 800, 800, 800
        h.Open "GET", "http://127.0.0.1:17890/api/ping", False
        h.Send
        If Err.Number = 0 Then
            If h.Status = 200 Then alive = True
        End If
    End If
    Err.Clear
    On Error GoTo 0
    PortAlive = alive
End Function

WriteLog "=== launcher start ==="
WriteLog "script=" & WScript.ScriptFullName

' ---- project dir ----
dir = RegEnv("SENTINEL_HOME")
If dir = "" Then dir = shell.ExpandEnvironmentStrings("%SENTINEL_HOME%")
If dir = "%SENTINEL_HOME%" Then dir = ""
If dir <> "" Then dir = Replace(dir, "/", "\")
If dir = "" Or Not fso.FileExists(dir & "\sentinel.py") Then
    dir = fso.GetParentFolderName(WScript.ScriptFullName)
End If
If Not fso.FileExists(dir & "\sentinel.py") Then
    dir = "F:\self\agentπ‹¿Ì\agent-sentinel"
End If
WriteLog "dir=" & dir

' ---- python interpreter (pythonw.exe = no console) ----
py = RegEnv("SENTINEL_PYTHON")
If py = "" Then py = shell.ExpandEnvironmentStrings("%SENTINEL_PYTHON%")
If py = "%SENTINEL_PYTHON%" Then py = ""
If py <> "" Then py = Replace(py, "/", "\")
If Not fso.FileExists(py) Then py = "F:\AppData\.workbuddy\binaries\python\envs\sentinel\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"
WriteLog "py=" & py

If Not fso.FileExists(dir & "\sentinel.py") Then
    WriteLog "FAIL sentinel.py missing"
    MsgBox "Cannot find sentinel.py" & vbCrLf & "Looked in: " & dir, 16, "Agent Sentinel"
    WScript.Quit 1
End If

' ---- already running? -> bring the existing window back instead ----
If PortAlive() Then
    WriteLog "already running, asking it to show the window"
    On Error Resume Next
    Set h2 = CreateObject("WinHttp.WinHttpRequest.5.1")
    If Err.Number = 0 Then
        h2.SetTimeouts 800, 800, 800, 800
        h2.Open "GET", "http://127.0.0.1:17890/api/show", False
        h2.Send
    End If
    Err.Clear
    On Error GoTo 0
    WScript.Quit 0
End If

shell.CurrentDirectory = dir
cmd = """" & py & """ """ & dir & "\sentinel.py"""
WriteLog "cmd=" & cmd

On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    WriteLog "FAIL run " & Err.Number & " " & Err.Description
    MsgBox "Launch failed: " & Err.Description & vbCrLf & cmd, 16, "Agent Sentinel"
    WScript.Quit 1
End If
On Error GoTo 0

' pythonw.exe has no console: a crash would be completely silent, so verify.
WScript.Sleep 6000
found = PortAlive()
If Not found Then
    ' port not up yet - give it one more chance before complaining
    WScript.Sleep 4000
    found = PortAlive()
End If

If found Then
    WriteLog "started ok"
Else
    WriteLog "FAIL did not come up"
    MsgBox "Agent Sentinel did not start." & vbCrLf & vbCrLf & _
           "Command:" & vbCrLf & cmd & vbCrLf & vbCrLf & _
           "Logs:" & vbCrLf & _
           "  " & dir & "\sentinel-launch.log" & vbCrLf & _
           "  " & dir & "\sentinel-error.log", 16, "Agent Sentinel"
End If
