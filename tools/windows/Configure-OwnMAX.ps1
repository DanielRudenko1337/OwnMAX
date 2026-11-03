param([string]$PackageRoot = $PSScriptRoot, [switch]$CheckOnly)

$ErrorActionPreference = 'Stop'

function Get-Digest([byte[]]$Bytes) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
}

function Get-Replacement($Patch, [string]$ServerName, [int]$ServerPort) {
    $length = [int]$Patch.length
    switch ($Patch.kind) {
        'host_json' { return [Text.Encoding]::ASCII.GetBytes(('"ssl_url2":"' + $ServerName + '"').PadRight($length, ' ')) }
        'port_json' { return [Text.Encoding]::ASCII.GetBytes(('"ssl_port":' + $ServerPort.ToString([Globalization.CultureInfo]::InvariantCulture)).PadRight($length, ' ')) }
        'cstring' { return [Text.Encoding]::ASCII.GetBytes($ServerName.PadRight($length, [char]0)) }
        'qstring' {
            $result = New-Object byte[] $length
            [Array]::Copy([BitConverter]::GetBytes([int]$ServerName.Length), 0, $result, 0, 4)
            $encoded = [Text.Encoding]::Unicode.GetBytes($ServerName)
            [Array]::Copy($encoded, 0, $result, 4, $encoded.Length)
            return $result
        }
        default { throw ('Unknown patch kind: ' + $Patch.kind) }
    }
}

try {
    $PackageRoot = [IO.Path]::GetFullPath($PackageRoot)
    $settings = Get-Content -LiteralPath (Join-Path $PackageRoot 'server.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $serverName = [string]$settings.host
    $serverPort = 0
    if ($serverName -eq 'test.invalid' -or $serverName.Length -gt 12 -or $serverName -notmatch '^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$') {
        throw 'Edit server.json: host must be your IPv4 address or ASCII DNS name, at most 12 characters. Use a short DNS alias for longer addresses. Do not include a scheme, port or path.'
    }
    if (-not [int]::TryParse([string]$settings.port, [ref]$serverPort) -or $serverPort -lt 1 -or $serverPort -gt 65535) {
        throw 'Edit server.json: port must be an integer from 1 to 65535.'
    }
    $running = @(Get-Process -Name 'max', 'max-service' -ErrorAction SilentlyContinue)
    foreach ($process in $running) {
        if ($process.Path -and [IO.Path]::GetFullPath($process.Path).StartsWith($PackageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Close this OwnMAX client, including its tray icon and background service, before changing the server.'
        }
    }
    $manifest = Get-Content -LiteralPath (Join-Path $PackageRoot 'binary-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.version -ne 1) { throw 'Unsupported package manifest.' }
    $prepared = New-Object 'System.Collections.Generic.List[object]'
    foreach ($entry in $manifest.files) {
        $path = [IO.Path]::GetFullPath((Join-Path $PackageRoot $entry.path))
        if (-not $path.StartsWith($PackageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid binary path.' }
        $original = [IO.File]::ReadAllBytes($path)
        $normalized = [byte[]]$original.Clone()
        foreach ($patch in $entry.patches) {
            $template = [Convert]::FromBase64String($patch.template)
            if ($template.Length -ne $patch.length -or $patch.offset -lt 0 -or $patch.offset + $patch.length -gt $normalized.Length) { throw 'Invalid patch range.' }
            [Array]::Copy($template, 0, $normalized, [int]$patch.offset, $template.Length)
        }
        if ((Get-Digest $normalized) -ne $entry.sha256) { throw ('Unexpected or damaged binary: ' + $entry.path + '. Extract a fresh release archive; do not mix client versions.') }
        foreach ($patch in $entry.patches) {
            [byte[]]$replacement = Get-Replacement $patch $serverName $serverPort
            if ($replacement.Length -ne $patch.length) { throw 'Replacement exceeds the binary slot.' }
            [Array]::Copy($replacement, 0, $normalized, [int]$patch.offset, $replacement.Length)
        }
        $prepared.Add([pscustomobject]@{ Path = $path; Original = $original; Updated = $normalized })
    }
    if ($CheckOnly) { Write-Host 'OwnMAX package and server settings verified.'; exit 0 }
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable.' }
    $localFolder = Join-Path $env:LOCALAPPDATA 'OwnMAX'
    $localConfigPath = Join-Path $localFolder 'config.json'
    $config = Get-Content -LiteralPath (Join-Path $PackageRoot 'config.template.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $config.ssl_url2 = $serverName
    $config.ssl_port = $serverPort
    $config.app_folder_name = 'OwnMAX'
    $localJson = $config | ConvertTo-Json -Depth 20
    $changed = New-Object 'System.Collections.Generic.List[object]'
    try {
        foreach ($item in $prepared) {
            $temporary = $item.Path + '.ownmax.tmp'
            [IO.File]::WriteAllBytes($temporary, $item.Updated)
            [IO.File]::Replace($temporary, $item.Path, [NullString]::Value)
            $changed.Add($item)
        }
        [IO.Directory]::CreateDirectory($localFolder) | Out-Null
        $utf8 = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($localConfigPath + '.tmp', $localJson, $utf8)
        if ([IO.File]::Exists($localConfigPath)) { [IO.File]::Replace($localConfigPath + '.tmp', $localConfigPath, [NullString]::Value) }
        else { [IO.File]::Move($localConfigPath + '.tmp', $localConfigPath) }
    }
    catch {
        foreach ($item in $changed) { [IO.File]::WriteAllBytes($item.Path, $item.Original) }
        throw
    }
    Write-Host ('OwnMAX server: ' + $serverName + ':' + $serverPort)
    Write-Host ('Local configuration: ' + $localConfigPath)
}
catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
