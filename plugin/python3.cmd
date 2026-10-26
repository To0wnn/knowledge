@rem Codex starts the MCP server as ./python3 in this folder, agy runs its hooks with cmd /c here (cmd looks here
@rem before PATH): run the machine's own Python. Each candidate is test-run, because "python" on PATH is often only the
@rem Microsoft Store stub. Order: the py launcher, Python on PATH, then every install registered the official way
@rem (PEP 514), which python.org installs do even without py or the PATH box ticked.
@echo off
for %%p in (py python) do call :try "%%p" && goto run
for %%k in (HKCU\SOFTWARE\Python HKLM\SOFTWARE\Python HKLM\SOFTWARE\WOW6432Node\Python) do (
  for /f "tokens=2,*" %%a in ('reg query %%k /s /v ExecutablePath 2^>nul ^| findstr /c:"ExecutablePath"') do (
    call :try "%%b" && goto run
  )
)
echo knowledge plugin: no working Python 3.8+ found (py, PATH, registry); install it from https://www.python.org 1>&2
exit /b 1
:try
"%~1" -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul || exit /b 1
set "KNOWLEDGE_PY=%~1"
exit /b 0
:run
"%KNOWLEDGE_PY%" %*
exit /b %errorlevel%
