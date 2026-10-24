@rem Codex starts the MCP server as ./python3 in this folder, agy runs its hooks with cmd /c here (cmd looks here
@rem before PATH): run the machine's own Python. python.org has py and python, the Microsoft Store only python.
@echo off
where py >nul 2>nul && goto py
python %*
exit /b %errorlevel%
:py
py %*
