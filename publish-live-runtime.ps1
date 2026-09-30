[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,

    [string]$PublisherWorktree = (Join-Path (Split-Path -Parent $PSScriptRoot) 'BTC-LRA-LIVE-PUBLISHER'),
    [string]$Branch = 'live-runtime',
    [string]$Remote = 'origin'
)

$ErrorActionPreference = 'Stop'

function Invoke-Git {
    param(
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )
    & git -C $WorkingDirectory @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git failed ($LASTEXITCODE): git -C `"$WorkingDirectory`" $($Arguments -join ' ')"
    }
}

function Resolve-FullPath {
    param([string]$PathValue)
    return [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PathValue -ErrorAction Stop).Path)
}

$repoRoot = Resolve-FullPath (Join-Path $PSScriptRoot '.')
$sourceRoot = Resolve-FullPath $RuntimeRoot
$publisherRoot = [IO.Path]::GetFullPath($PublisherWorktree)
$liveCurrent = Join-Path $publisherRoot 'live-current'

if ($sourceRoot -eq $repoRoot -or $publisherRoot -eq $repoRoot -or $publisherRoot -eq $sourceRoot) {
    throw 'Publisher worktree and source runtime must be separate from the main repository and BTC-LRA-LIVE runtime.'
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.git'))) {
    throw "Main repository was not found: $repoRoot"
}
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Runtime root was not found: $sourceRoot"
}

$files = @(
    @{ Relative = 'events/BTC_LRA_002_EVENTS.jsonl'; Destination = 'events/BTC_LRA_002_EVENTS.jsonl' },
    @{ Relative = 'events/BTC_LRA_002_BATTLES.jsonl'; Destination = 'events/BTC_LRA_002_BATTLES.jsonl' },
    @{ Relative = 'events/BTC_LRA_002_RELEASES.jsonl'; Destination = 'events/BTC_LRA_002_RELEASES.jsonl' },
    @{ Relative = 'events/BTC_LRA_002_OI_SAMPLES.jsonl'; Destination = 'events/BTC_LRA_002_OI_SAMPLES.jsonl' },
    @{ Relative = 'logs/BTC_LRA_002_HUMAN.log'; Destination = 'logs/BTC_LRA_002_HUMAN.log' },
    @{ Relative = 'debug/BTC_LRA_002_DEBUG.log'; Destination = 'debug/BTC_LRA_002_DEBUG.log' },
    @{ Relative = 'state/BTC_LRA_002_STATE.json'; Destination = 'state/BTC_LRA_002_STATE.json' },
    @{ Relative = 'state/BTC_LRA_002_ZONE_STATE.json'; Destination = 'state/BTC_LRA_002_ZONE_STATE.json' }
)

Write-Output "Source runtime: $sourceRoot"
Write-Output "Publisher worktree: $publisherRoot"
Write-Output "Branch: $Branch"

if (-not (Test-Path -LiteralPath $publisherRoot)) {
    New-Item -ItemType Directory -Path $publisherRoot -Force | Out-Null
    Invoke-Git $repoRoot @('worktree', 'add', '-B', $Branch, $publisherRoot, "$Remote/main")
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $publisherRoot '.git'))) {
        throw "Publisher path exists but is not a Git worktree: $publisherRoot"
    }
    $actualRoot = (git -C $publisherRoot rev-parse --show-toplevel).Trim()
    if ([IO.Path]::GetFullPath($actualRoot) -ne $publisherRoot) {
        throw "Unexpected publisher worktree root: $actualRoot"
    }
    Invoke-Git $publisherRoot @('fetch', $Remote)
    Invoke-Git $publisherRoot @('switch', '-C', $Branch, "$Remote/main")
    Invoke-Git $publisherRoot @('reset', '--hard', "$Remote/main")
    Invoke-Git $publisherRoot @('clean', '-fdx')
}

Invoke-Git $publisherRoot @('fetch', $Remote)
if (Test-Path -LiteralPath $liveCurrent) {
    Remove-Item -LiteralPath $liveCurrent -Recurse -Force
}
New-Item -ItemType Directory -Path $liveCurrent -Force | Out-Null

$published = @()
foreach ($file in $files) {
    $source = Join-Path $sourceRoot $file.Relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        Write-Output "ABSENT: $($file.Relative)"
        continue
    }
    $destination = Join-Path $liveCurrent $file.Destination
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
    $item = Get-Item -LiteralPath $destination
    $size = [int64]$item.Length
    Write-Output ("PUBLISHED: {0} | {1} bytes" -f $file.Relative, $size)
    if ($size -gt 100MB) {
        throw "GitHub per-file limit guard: $($file.Relative) is $size bytes (>100 MB). No push was attempted."
    }
    $published += $destination
}

$publicationTime = (Get-Date).ToUniversalTime().ToString('o')
Write-Output "COPY_COMPLETED_UTC: $publicationTime"
if ($published.Count -eq 0) {
    throw 'No runtime files were available for publication.'
}

Invoke-Git $publisherRoot @('add', '--', 'live-current')
Invoke-Git $publisherRoot @('commit', '-m', "Publish live runtime $publicationTime")
Invoke-Git $publisherRoot @('push', '--force', $Remote, "$Branch`:$Branch")
$remoteSha = (git -C $publisherRoot ls-remote $Remote "refs/heads/$Branch").Split("`t")[0].Trim()
$localSha = (git -C $publisherRoot rev-parse HEAD).Trim()
if ($remoteSha -ne $localSha) {
    throw "Remote SHA mismatch: local=$localSha remote=$remoteSha"
}
Write-Output "PUBLICATION_SHA: $localSha"
Write-Output "REMOTE_REF: $Remote/$Branch"
