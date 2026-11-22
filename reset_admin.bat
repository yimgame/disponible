@echo off
REM ============================================================
REM  Disponible de unidades - resetear la clave de un administrador
REM  Uso:  reset_admin.bat               (usuario admin, clave temporal al azar)
REM        reset_admin.bat juan           (otro usuario)
REM        reset_admin.bat admin MiClave1234   (clave elegida, minimo 10 caracteres)
REM  Si el panel corre en Docker (container "disponible-panel") lo hace adentro;
REM  si no, lo corre con Python sobre la base local (variable PANEL_DB).
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
    echo Reseteando "%USUARIO%" en el container %CONTENEDOR%...
    docker exec %CONTENEDOR% python -m app.reset_admin %USUARIO% %CLAVE%
    goto fin
  )
)

echo No encontre el container "%CONTENEDOR%": lo hago con Python sobre la base local.
if "%PANEL_DB%"=="" set "PANEL_DB=%~dp0data\disponible.db"
echo Base: %PANEL_DB%
pushd "%~dp0panel"
python -m app.reset_admin %USUARIO% %CLAVE%
popd

:fin
echo.
echo Si probaste muchas claves mal, el login queda frenado 15 minutos (o reinicia el panel).
pause
endlocal
