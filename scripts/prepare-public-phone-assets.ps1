param([switch]$Verify)
$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$destination = [IO.Path]::GetFullPath((Join-Path $project 'data/test-assets'))
if (-not (Test-Path -LiteralPath $destination)) {
    if ($Verify) { throw 'Public test assets are absent; omit -Verify to prepare them explicitly.' }
    New-Item -ItemType Directory -Path $destination | Out-Null
}
if ((Get-Item -LiteralPath $destination).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Refusing a reparse-point test asset directory.'
}
foreach ($name in @('public-phone-lotti', 'public-phone-android')) {
    $metadata = Get-Content -LiteralPath (Join-Path $PSScriptRoot "test-assets/$name.source.json") -Raw | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath((Join-Path $project $metadata.local_file))
    if (-not $target.StartsWith($destination + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Manifest destination escaped the exact public test asset directory.'
    }
    $url = [Uri]$metadata.download_url
    if ($url.Scheme -ne 'https' -or $url.Host -ne 'upload.wikimedia.org') { throw 'Only the verified official source is allowed.' }
    if (Test-Path -LiteralPath $target) {
        $existing = Get-Item -LiteralPath $target
        if (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $existing.PSIsContainer) { throw 'Refusing non-regular asset file.' }
        if ($existing.Length -ne $metadata.bytes -or (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $metadata.sha256) {
            throw "Existing file has a different hash; preserving it without overwrite: $target"
        }
    } else {
        if ($Verify) { throw "Missing public test asset: $target" }
        $partial = $target + '.part'
        if (Test-Path -LiteralPath $partial) { throw "Existing partial preserved; investigate it before retry: $partial" }
        Invoke-WebRequest -Uri $url.AbsoluteUri -OutFile $partial -TimeoutSec 45
        if ((Get-Item -LiteralPath $partial).Length -ne $metadata.bytes -or (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $metadata.sha256) {
            throw "Downloaded content did not match pinned hash; partial retained for diagnosis: $partial"
        }
        Move-Item -LiteralPath $partial -Destination $target
    }
    [PSCustomObject]@{ File=$target; Verified=$true; Bytes=$metadata.bytes; License=$metadata.license_selected; Author=$metadata.author }
}
