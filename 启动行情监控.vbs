' Launch GoldInfo desktop widget without a console window.
Option Explicit
Dim fso, sh, base
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

On Error Resume Next
sh.Run "pythonw.exe """ & base & "\app.py""", 0, False
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "pyw.exe """ & base & "\app.py""", 0, False
End If
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python.exe """ & base & "\app.py""", 1, False
End If
If Err.Number <> 0 Then
    MsgBox "Python was not found. Please install Python 3 and enable 'Add Python to PATH'.", 16, "GoldInfo"
End If
