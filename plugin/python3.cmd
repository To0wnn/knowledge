@rem Codex starts the MCP server as ./python3 in this folder, agy runs its hooks with cmd /c here (cmd looks here
@rem before PATH): run the machine's own Python. Each candidate is test-run, because "python" on PATH is often only the
@rem Microsoft Store stub. Order: the py launcher, Python on PATH, then every install registered the official way
@rem (PEP 514), which python.org installs do even without py or the PATH box ticked. Probes use call: a .cmd/.bat shim
@rem (pyenv-win) started without it would end this script.
@echo off
set "KNOWLEDGE_PY="
for %%p in (py python) do if not defined KNOWLEDGE_PY (
  call %%p -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "KNOWLEDGE_PY=%%p"
)
if defined KNOWLEDGE_PY goto run
for %%k in (HKCU\SOFTWARE\Python HKLM\SOFTWARE\Python HKLM\SOFTWARE\WOW6432Node\Python) do (
  for /f "tokens=2,*" %%a in ('reg query %%k /s /v ExecutablePath 2^>nul ^| findstr /c:"ExecutablePath"') do (
    if not defined KNOWLEDGE_PY (
      call "%%b" -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "KNOWLEDGE_PY=%%b"
    )
  )
)
if defined KNOWLEDGE_PY goto run
echo knowledge plugin: no working Python 3.8+ found via py, PATH or the registry; install it from https://www.python.org 1>&2
exit /b 1
:run
"%KNOWLEDGE_PY%" %*
exit /b %errorlevel%
