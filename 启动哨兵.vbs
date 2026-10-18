' 双击静默启动 Agent 哨兵（无控制台窗口）
' Python 解析顺序：环境变量 SENTINEL_PYTHON > 常见受管 venv > PATH 里的 pythonw
Dim fso, shell, dir, py, candidates, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

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

shell.CurrentDirectory = dir
shell.Run """" & py & """ """ & dir & "\sentinel.py""", 0, False
