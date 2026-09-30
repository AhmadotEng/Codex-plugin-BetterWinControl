param(
    [string]$Toolchain = $env:BWC_NATIVE_TOOLCHAIN,
    [ValidateSet('all','x64','x86')][string]$Architecture = 'all'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$native = Join-Path $repo 'native'
if (!$Toolchain) {
    $compiler = Get-Command 'x86_64-w64-mingw32-clang++.exe' -ErrorAction SilentlyContinue
    if ($compiler) { $Toolchain = Split-Path -Parent (Split-Path -Parent $compiler.Source) }
    else { throw 'Set BWC_NATIVE_TOOLCHAIN to an existing llvm-mingw directory, add its bin directory to PATH, or pass -Toolchain <directory>. No compiler is installed by this script.' }
}
$archive = Join-Path $native 'vendor\minhook-v1.3.4.zip'
$jsonHeader = Join-Path $native 'vendor\json\json.hpp'
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '172708123DAA0C98D20D3A980B16A50BE14AF243DC95DEE6F79C24193AD010E4') { throw 'MinHook v1.3.4 archive hash mismatch.' }
if ((Get-FileHash -LiteralPath $jsonHeader -Algorithm SHA256).Hash -ne '9BEA4C8066EF4A1C206B2BE5A36302F8926F7FDC6087AF5D20B417D0CF103EA6') { throw 'nlohmann json v3.11.3 header hash mismatch.' }
$manifest = Get-Content -LiteralPath (Join-Path $native 'vendor-source-hashes.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.files) {
    $sourceFile = Join-Path $native $entry.path
    if ((Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne $entry.sha256) { throw "Pinned vendor source changed: $($entry.path)" }
}
$architectures = if ($Architecture -eq 'all') { @('x64','x86') } else { @($Architecture) }
foreach ($arch in $architectures) {
    $triple = if ($arch -eq 'x64') { 'x86_64' } else { 'i686' }
    $cc = Join-Path $Toolchain "bin\$triple-w64-mingw32-clang.exe"
    $cxx = Join-Path $Toolchain "bin\$triple-w64-mingw32-clang++.exe"
    if (!(Test-Path -LiteralPath $cxx)) { throw "Missing compiler: $cxx" }
    $out = Join-Path $native "bin\$arch"
    $obj = Join-Path $native "obj\$arch"
    New-Item -ItemType Directory -Path $out,$obj -Force | Out-Null
    $sources = @('buffer.c','hook.c','trampoline.c',"hde\hde$(if ($arch -eq 'x64') {'64'} else {'32'}).c")
    $objects = @()
    foreach ($source in $sources) {
        $inputFile = Join-Path $native "vendor\minhook-1.3.4\src\$source"
        $objectFile = Join-Path $obj ((Split-Path -Leaf $source) + '.o')
        & $cc '-O2' '-D_WIN32_WINNT=0x0A00' '-c' $inputFile '-o' $objectFile
        if ($LASTEXITCODE -ne 0) { throw "MinHook compile failed: $source" }
        $objects += $objectFile
    }
    $common = @('-std=c++20','-O2','-Wall','-Wextra','-Wno-unused-parameter','-D_WIN32_WINNT=0x0A00','-DUNICODE','-D_UNICODE','-static')
    & $cxx @common '-shared' (Join-Path $native 'virtual_input.cpp') @objects '-o' (Join-Path $out 'VirtualInput.dll') '-luser32' '-Wl,--kill-at'
    if ($LASTEXITCODE -ne 0) { throw "Native DLL build failed: $arch" }
    & $cxx @common '-municode' (Join-Path $native 'host.cpp') '-o' (Join-Path $out 'BetterWinControl.NativeHost.exe') '-luser32' '-ladvapi32' '-lbcrypt'
    if ($LASTEXITCODE -ne 0) { throw "Native host build failed: $arch" }
    Write-Output "Built native $arch host and helper."
}
