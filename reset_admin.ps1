# Disponible de unidades - resetear la clave de un admin (o liberar la espera por intentos fallidos)
# en el panel que corre en un servidor con Portainer, desde Windows. Lo llama reset_admin.bat.
#   -Usuario admin | otro | desbloquear     -Clave (opcional)
# Pide el usuario y la contraseña de Portainer en el momento (no se guardan). La dirección de Portainer y el
# endpoint se leen de ignorar\reset_admin.conf (fuera de git), por ejemplo:
#   PORTAINER_URL=https://servidor:9443
#   ENDPOINT_ID=3
#   CONTENEDOR=disponible-panel
param([string]$Usuario = "admin", [string]$Clave = "", [string]$ApiKey = "")
$ErrorActionPreference = "Stop"

# --- configuración
$conf = @{ PORTAINER_URL = ""; ENDPOINT_ID = "3"; CONTENEDOR = "disponible-panel" }
$archivo = Join-Path $PSScriptRoot "ignorar\reset_admin.conf"
if (Test-Path $archivo) {
  Get-Content $archivo | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$' } | ForEach-Object { $conf[$Matches[1]] = $Matches[2] }
}
if (-not $conf.PORTAINER_URL) { $conf.PORTAINER_URL = Read-Host "Direccion de Portainer (ej. https://servidor:9443)" }
$base = $conf.PORTAINER_URL.TrimEnd("/")

# --- Portainer usa certificado propio: aceptarlo solo para esta sesion
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($PSVersionTable.PSVersion.Major -lt 6) {
  Add-Type @"
using System.Net; using System.Security.Cryptography.X509Certificates;
public class TodoCert : ICertificatePolicy { public bool CheckValidationResult(ServicePoint s, X509Certificate c, WebRequest r, int p) { return true; } }
"@
  [Net.ServicePointManager]::CertificatePolicy = New-Object TodoCert
  $extra = @{}
} else { $extra = @{ SkipCertificateCheck = $true } }

# --- autenticación: usuario y contraseña de Portainer (o -ApiKey)
if ($ApiKey) { $hdr = @{ "X-API-Key" = $ApiKey } }
else {
  $u = Read-Host "Usuario de Portainer"
  $p = Read-Host "Contrasena de Portainer" -AsSecureString
  $plano = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))
  try {
    $jwt = (Invoke-RestMethod -Method Post -Uri "$base/api/auth" -ContentType "application/json" `
            -Body (@{ username = $u; password = $plano } | ConvertTo-Json) @extra).jwt
  } catch { Write-Host "`n  No pude entrar a Portainer: usuario o contrasena incorrectos, o no llego al servidor." -ForegroundColor Red; exit 1 }
  finally { $plano = $null }
  $hdr = @{ Authorization = "Bearer $jwt" }
}

# --- correr el comando dentro del container
$cmd = @("python", "-m", "app.reset_admin", $Usuario)
if ($Clave) { $cmd += $Clave }
$docker = "$base/api/endpoints/$($conf.ENDPOINT_ID)/docker"
try {
  $exec = Invoke-RestMethod -Method Post -Uri "$docker/containers/$($conf.CONTENEDOR)/exec" -Headers $hdr -ContentType "application/json" `
            -Body (@{ AttachStdout = $true; AttachStderr = $true; Tty = $true; Cmd = $cmd } | ConvertTo-Json) @extra
  $r = Invoke-WebRequest -Method Post -Uri "$docker/exec/$($exec.Id)/start" -Headers $hdr -ContentType "application/json" `
            -Body (@{ Detach = $false; Tty = $true } | ConvertTo-Json) -UseBasicParsing @extra
  $salida = if ($r.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($r.Content) } else { $r.Content }
  Write-Host $salida
} catch {
  Write-Host "`n  No pude ejecutar en el container $($conf.CONTENEDOR): $($_.Exception.Message)" -ForegroundColor Red; exit 1
}
