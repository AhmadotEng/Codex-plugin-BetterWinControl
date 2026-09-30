param([switch]$Locked)
$ErrorActionPreference = 'Stop'
$pluginRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$controllerProject = Join-Path $pluginRoot 'controller\BackgroundControl.csproj'
$env:DOTNET_CLI_HOME = Join-Path $pluginRoot '.build\dotnet-home'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:NUGET_PACKAGES = Join-Path $pluginRoot '.build\packages'
$env:NUGET_HTTP_CACHE_PATH = Join-Path $pluginRoot '.build\http-cache'
$restoreArguments = @('restore', $controllerProject, '--configfile', (Join-Path $pluginRoot 'controller\NuGet.Config'), '--verbosity', 'minimal')
if ($Locked) { $restoreArguments += '--locked-mode' }
& dotnet @restoreArguments
if ($LASTEXITCODE -ne 0) { throw 'Restore failed' }
& dotnet publish $controllerProject --no-restore -c Release -o (Join-Path $pluginRoot 'runtime') --verbosity minimal
if ($LASTEXITCODE -ne 0) { throw 'Publish failed' }
