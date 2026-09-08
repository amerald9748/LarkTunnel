' Hidden launcher for watcher-service.bat (no console window at logon).
Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.Run """" & here & "watcher-service.bat""", 0, False
