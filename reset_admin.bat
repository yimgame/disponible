@echo off
REM ============================================================
REM  Disponible de unidades - resetear la clave de un administrador
REM  Uso:  reset_admin.bat               (usuario admin, clave temporal al azar)
REM        reset_admin.bat juan           (otro usuario)
REM        reset_admin.bat admin MiClave1234   (clave elegida, minimo 10 caracteres)
REM        reset_admin.bat desbloquear     (solo libera la espera por muchos intentos fallidos)
REM  Resetear una clave tambien libera la espera.
REM  1) Si en esta PC corre el container "disponible-panel" (Docker), lo hace ahi.
REM  2) Si no, se conecta a Portainer (pide usuario y clave de Portainer) y lo hace
REM     en el container del servidor. Direccion en ignorar\reset_admin.conf
REM ============================================================
setlocal
chcp 65001 >nul
set "CONTENEDOR=disponible-panel"
set "USUARIO=%~1"
if "%USUARIO%"=="" set "USUARIO=admin"
set "CLAVE=%~2"

where docker >nul 2>&1
if %errorlevel%==0 (
  docker ps --format "{{.Names}}" 2>nul | findstr /x /c:"%CONTENEDOR%" >nul
  if not errorlevel 1 (
    echo Ejecutando "%USUARIO%" en el container local %CONTENEDOR%...
    docker exec %CONTENEDOR% python -m app.reset_admin %USUARIO% %CLAVE%
    goto fin
  )
)

echo Conectando a Portainer para ejecutar "%USUARIO%" en el servidor...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset_admin.ps1" -Usuario "%USUARIO%" -Clave "%CLAVE%"

:fin
echo.
pause
endlocal
