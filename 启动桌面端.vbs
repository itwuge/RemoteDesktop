' RDC desktop single entry: no console window.
' Prefer the packaged rdc-desktop.exe next to this file (portable layout);
' otherwise run the dev tree through pythonw so the window never needs a console.
Option Explicit

Dim fso, shell, root, exePath, pythonw, buildRoot, command, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = fso.BuildPath(root, "rdc-desktop.exe")

If fso.FileExists(exePath) Then
    code = shell.Run(Quote(exePath), 0, False)
    If code <> 0 Then ShowFailure
    WScript.Quit 0
End If

buildRoot = shell.ExpandEnvironmentStrings("%RDC_BUILD_ROOT%")
If buildRoot = "%RDC_BUILD_ROOT%" Then
    buildRoot = "D:\RDC-Build"
End If

pythonw = fso.BuildPath(buildRoot, "venv\Scripts\pythonw.exe")
If Not fso.FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If

shell.Environment("Process")("PYTHONPATH") = fso.BuildPath(root, "src")
shell.CurrentDirectory = root
command = Quote(pythonw) & " -m rdc.ui"
code = shell.Run(command, 0, False)
If code <> 0 Then ShowFailure
WScript.Quit 0

Sub ShowFailure()
    MsgBox "RDC desktop failed to start. Check logs under " & root & "\logs", 16, "RDC"
End Sub

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
