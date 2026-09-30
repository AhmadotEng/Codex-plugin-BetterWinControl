param([Parameter(Mandatory=$true)][string]$Toolchain)
$ErrorActionPreference='Stop'
foreach ($arch in @('x64','x86')) {
    $triple=if($arch -eq 'x64'){'x86_64'}else{'i686'}
    $compiler=Join-Path $Toolchain "bin\$triple-w64-mingw32-clang++.exe"
    $output=Join-Path $PSScriptRoot "bin\$arch"
    New-Item -ItemType Directory -Path $output -Force|Out-Null
    $source=Join-Path $PSScriptRoot 'quiescence_fixture.cpp'
    $dll=Join-Path $output 'QuiescenceFixture.dll'
    $exe=Join-Path $output 'QuiescenceAudit.exe'
    & $compiler -std=c++20 -O2 -static -shared -DBWC_AUDIT_DLL $source -o $dll '-Wl,--kill-at'
    if($LASTEXITCODE){throw 'Owned quiescence DLL build failed.'}
    & $compiler -std=c++20 -O2 -static -municode $source -o $exe
    if($LASTEXITCODE){throw 'Owned quiescence audit build failed.'}
    $result=& $exe $dll
    if($LASTEXITCODE){throw "Quiescence audit failed ($arch): $result"}
    $parsed=$result|ConvertFrom-Json
    $parsed|Add-Member -NotePropertyName architecture -NotePropertyValue $arch
    $parsed|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $PSScriptRoot "quiescence-$arch-results.json") -Encoding utf8
    $result
}
