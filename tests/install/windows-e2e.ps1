# ============================================================================
# Windows install + update E2E driver
# ============================================================================
# Installs an old revision with its own install.ps1, then updates to HEAD via
# `hermes update` or by re-running the current installer. A local bare clone
# and git URL redirect make the transition deterministic while installer and
# updater run their real production paths.
#
# Usage: powershell -File tests\install\windows-e2e.ps1 -Phase all
# Phases stage/install/update share state through <workroot>\shas.json.
# ============================================================================

param(
    [ValidateSet("stage", "install", "update", "all")]
    [string]$Phase = "all",

    [ValidateSet("installer-script")]
    [string]$InstallMethod = "installer-script",

    [ValidateSet("hermes-update", "installer-script")]
    [string]$Route = "hermes-update",

    # Empty or "auto" selects the newest release tag in the checkout.
    [string]$InstallRef = "auto",
    [string]$RepoRoot = "",
    [string]$WorkRoot = $(if ($env:HERMES_E2E_WORKROOT) { $env:HERMES_E2E_WORKROOT } else { Join-Path $env:TEMP "hermes-install-e2e" })
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# Match an interactive Unicode console when Python output is piped into the
# UTF-8 transcript. Old releases otherwise select cp1252 and crash on banners.
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = [Console]::OutputEncoding

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$ServeRepo   = Join-Path $WorkRoot "serve.git"
$HermesHome  = Join-Path $WorkRoot "hermes-home"
$InstallDir  = Join-Path $HermesHome "hermes-agent"
$StatePath   = Join-Path $WorkRoot "shas.json"
$ProofRoot   = Join-Path $WorkRoot "proof"
$AssetsDir   = Join-Path $PSScriptRoot "e2e-assets"

$RepoUrlHttps = "https://github.com/NousResearch/hermes-agent.git"
$RepoUrlSsh   = "git@github.com:NousResearch/hermes-agent.git"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("=" * 74)
    Write-Host "== $Message"
    Write-Host ("=" * 74)
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) {
        throw "E2E ASSERTION FAILED: $Message"
    }
    Write-Host "  [ok] $Message"
}

function Invoke-Git([string[]]$GitArgs) {
    # PS 5.1 trap: under $ErrorActionPreference = "Stop", a native command
    # that writes ANYTHING to stderr while merged via 2>&1 throws a
    # NativeCommandError even when it exits 0 (git loves stderr for
    # progress/notices). Relax EAP around the native call only; exit-code
    # checking below is the real error gate.
    #
    # ALWAYS the real git.exe, never the shim we ship.
    # annoying bug where .bat files eat ^ args.
    # if hermes ever adds a git command that calls something with ^ this will break, lol.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $script:RealGitExe @GitArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE): $output"
        }
        return ($output | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

function Set-GitRedirect {
    # we redirect to our own repo so we can play around with what commit hermes thinks we're on.
    # MECHANISM: a driver-owned global gitconfig selected via
    # GIT_CONFIG_GLOBAL. Do NOT use GIT_CONFIG_COUNT/KEY_n/VALUE_n env
    # config here -- install.ps1 SETS those itself (GIT_CONFIG_COUNT=1,
    # windows.appendAtomically), silently clobbering any redirect we put
    # there. install.ps1's own `git config --global` writes simply land in
    # our file, so its compat settings still apply. Nothing leaks onto the
    # machine: the file lives in the workroot and dies with it.
    $fileUrl = "file:///" + ($ServeRepo -replace "\\", "/")
    $gitCfg = Join-Path $WorkRoot "e2e-gitconfig"
    if (-not (Test-Path -LiteralPath $WorkRoot)) {
        New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
    }
    # first, get the set origin url
    $actualGitUrl = Invoke-Git @("-C", $RepoRoot, "remote", "get-url", "origin")
    # then override it 
    @"
[url "$fileUrl"]
	insteadOf = $actualGitUrl
    insteadOf = $RepoUrlHttps
    insteadOf = $RepoUrlSsh
"@ | Set-Content -LiteralPath $gitCfg -Encoding ASCII
    $env:GIT_CONFIG_GLOBAL = $gitCfg

    # check it worked
    $actualGitUrl = Invoke-Git @("-C", $RepoRoot, "remote", "get-url", "origin")
    Assert-True ($actualGitUrl -eq $fileUrl) "git URL redirect: origin resolves to '$actualGitUrl', expected '$fileUrl'."
    Write-Host "  git URL redirect via GIT_CONFIG_GLOBAL=$gitCfg"
    Write-Host "    $RepoUrlHttps -> $fileUrl"

    # shim git and make 'git remote get-url origin' report the actual HA upstream

    # insteadOf is transparent for transport but `git remote get-url origin` gives you the
    # replacement, so _get_origin_url() sees file://$SERVE_REPO and _is_fork() would return true.
    # we check for the arguments "remote get-url origin" in order in any position
    # to allow for e.g. -c with some config being passed.
    # if we didn't do this, we'd need the  .skip_upstream_prompt file to prevent a hang in headless,"add the
    # official repo as upstream?" prompt would hang a headless run. But we don't anymore :D

    $realGit = (Get-Command git.exe -ErrorAction Stop).Source
    $shimDir = Join-Path $WorkRoot "shim"
    New-Item -ItemType Directory -Path $shimDir -Force | Out-Null
    $shimPath = Join-Path $shimDir "git.bat"
    @"
@echo off
setlocal enabledelayedexpansion
set prev2=
set prev1=
:loop
if "%~1"=="" goto passthrough
if /I "!prev2!"=="remote" if /I "!prev1!"=="get-url" if /I "%~1"=="origin" (
    echo $RepoUrlHttps
    exit /b 0
)
set prev2=!prev1!
set prev1=%~1
shift
goto loop
:passthrough
`"$realGit`" %*
exit /b %ERRORLEVEL%
"@ | Set-Content -LiteralPath $shimPath -Encoding ASCII

    $env:PATH = "$shimDir;$env:PATH"

    # Check it worked THROUGH the shim - deliberately not Invoke-Git, which
    # pins the real git.exe. `git` via PATH here is exactly how the
    # product's callers resolve it. Probe the intercepted verb AND plain
    # passthrough.
    #
    # KNOWN HOLE, accepted: cmd parses the command line before the bat sees
    # %*, and callers only quote args containing whitespace (PowerShell
    # native binding and python's list2cmdline alike) - so a caret arg like
    # rev-parse HEAD^{commit} loses its caret THROUGH ANY .bat, unfixably.
    # A .ps1 shim would dodge cmd but PATHEXT-resolving callers (python -
    # the shim's entire audience) never see .ps1 files, so .bat it stays.
    # The driver's own git plumbing therefore pins git.exe (Invoke-Git),
    # and the product's shimmed flows (fork detection: remote get-url) use
    # no caret revs. If a product path ever sends carets through the shim,
    # the leg fails loudly on a bad-revision error naming the mangled arg.
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $observedGitUrl = (& git -C $RepoRoot remote get-url origin 2>&1 | Out-String).Trim()
    $passthroughProbe = (& git -C $RepoRoot rev-parse HEAD 2>&1 | Out-String).Trim()
    $passthroughExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    Assert-True ($observedGitUrl -eq $RepoUrlHttps) "git remote get-url shim: origin resolves to '$observedGitUrl', expected '$RepoUrlHttps'"
    Assert-True ($passthroughExit -eq 0 -and $passthroughProbe -match '^[0-9a-f]{40}$') "shim passthrough works: rev-parse HEAD -> '$passthroughProbe'"
    Write-Host "  git remote get-url shim: $shimPath -> $realGit"
    Write-Host "    'remote get-url origin' now reports $RepoUrlHttps"
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        throw "State file not found: $StatePath -- run '-Phase stage' first."
    }
    return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
}

function Get-InstalledHead {
    return Invoke-Git @("-C", $InstallDir, "rev-parse", "HEAD")
}

# Install-side state snapshot, taken BEFORE Test-HermesRuns can throw: the
# updater can run detached and leave its transcript in product logs and hand-off
# files rather than in this driver. Copy those plus the
# venv entry-point dir while the install is still there to inspect, so a
# failed post-update assertion leaves its evidence in the proof tree.
function Save-InstallSideState([string]$Label) {
    $dest = Join-Path $ProofRoot "install-side-$Label"
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    $logsDir = Join-Path $HermesHome "logs"
    if (Test-Path -LiteralPath $logsDir) {
        Copy-Item $logsDir (Join-Path $dest "hermes-logs") -Recurse -Force -ErrorAction SilentlyContinue
    }
    $resultFile = Join-Path $HermesHome ".hermes-update-result.json"
    if (Test-Path -LiteralPath $resultFile) {
        Copy-Item $resultFile $dest -Force -ErrorAction SilentlyContinue
    }
    $venvScripts = Join-Path $InstallDir "venv\Scripts"
    if (Test-Path -LiteralPath $venvScripts) {
        Get-ChildItem -LiteralPath $venvScripts |
            Select-Object Name, Length, LastWriteTime |
            Format-Table -AutoSize | Out-String |
            Set-Content (Join-Path $dest "venv-scripts-ls.txt")
    }
    Get-ChildItem -LiteralPath $HermesHome -ErrorAction SilentlyContinue |
        Select-Object Name, Length, LastWriteTime |
        Format-Table -AutoSize | Out-String |
        Set-Content (Join-Path $dest "hermes-home-ls.txt")
}

function Test-HermesRuns([string]$Label) {
    Save-InstallSideState $Label
    $hermesExe = Join-Path $InstallDir "venv\Scripts\hermes.exe"
    Assert-True (Test-Path -LiteralPath $hermesExe) "$Label -- venv\Scripts\hermes.exe exists"
    & $hermesExe --version 2>&1 | ForEach-Object { Write-Host "    hermes --version| $_" }
    Assert-True ($LASTEXITCODE -eq 0) "$Label -- hermes --version exits 0"
}

# ----------------------------------------------------------------------------
# Script-install arm: the irm | iex one-liner, headless (the install.ps1
# shipped AT the ref under test, run with flags probed from that ref's own
# script text - older releases reject parameters added later).
# ----------------------------------------------------------------------------
# shellcheck source=../e2e-assets/ts-prefix.ps1
. (Join-Path $PSScriptRoot "e2e-assets\ts-prefix.ps1")

function Write-LogGroup([string]$Title, [string]$LogPath) {
    Write-Host "::group::$Title"
    if (Test-Path -LiteralPath $LogPath) { Get-Content -LiteralPath $LogPath | Write-Host }
    Write-Host "::endgroup::"
}

function Invoke-RefInstaller {
    param([string]$Ref, [string]$Label)
    $script = Join-Path $WorkRoot "install-$Label.ps1"
    (Invoke-Git @("-C", $RepoRoot, "show", "$Ref`:scripts/install.ps1")) -join "`n" |
        Set-Content -LiteralPath $script -Encoding UTF8
    $flags = @("-SkipSetup", "-HermesHome", $HermesHome, "-InstallDir", $InstallDir)
    $text = Get-Content -LiteralPath $script -Raw
    if ($text -match '\$NonInteractive') { $flags += "-NonInteractive" }
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot "logs") -Force | Out-Null
    $log = Join-Path $WorkRoot "logs\install-$Label.log"
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script @flags 2>&1 | Add-TsPrefix | Out-File -Encoding UTF8 $log
    $installExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    Write-LogGroup "install.ps1 ($Label) transcript" $log
    Assert-True ($installExit -eq 0) "install.ps1 ($Label) exited 0"
}

function Invoke-HermesUpdate {
    # The venv updater. --yes reaches the update subcommand only in later
    # releases; ask the installed binary, never parse its source.
    $hermesExe = Join-Path $InstallDir "venv\Scripts\hermes.exe"
    $updateArgs = @("update")
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $helpText = & $hermesExe update --help 2>&1 | Out-String
    if ($helpText -match '--yes') { $updateArgs += "--yes" }
    New-Item -ItemType Directory -Path (Join-Path $WorkRoot "logs") -Force | Out-Null
    $log = Join-Path $WorkRoot "logs\update.log"
    Push-Location $InstallDir
    try {
        & $hermesExe @updateArgs 2>&1 | Add-TsPrefix | Out-File -Encoding UTF8 $log
        $updateExit = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEap
    }
    Write-LogGroup "hermes update transcript" $log
    Assert-True ($updateExit -eq 0) "hermes update exited $updateExit (expected 0)"
}

function Get-ManagedNode {
    # Use the Hermes-managed Node for the known-failure classifier so no
    # system Node installation is required.
    $candidates = @(
        (Join-Path $HermesHome "node\node.exe"),
        (Join-Path $HermesHome "bin\node\node.exe"),
        (Join-Path $InstallDir "node\node.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $fromPath = Get-Command node -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    throw "No node.exe found (managed or on PATH)"
}

# ----------------------------------------------------------------------------
# Phase: stage -- serve.git with `main` at OLD (advanced to HEAD for update)
# ----------------------------------------------------------------------------
function Invoke-PhaseStage {
    Write-Step "STAGE: bare serve repo, main -> OLD (install base)"

    if (Test-Path -LiteralPath $WorkRoot) {
        Remove-Item -LiteralPath $WorkRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
    # The purge above deleted the redirect gitconfig; re-arm it so the
    # bare-clone below (and everything after) sees the redirect file.
    Set-GitRedirect

    $current = Invoke-Git @("-C", $RepoRoot, "rev-parse", "HEAD")
    Write-Host "  HEAD (update target): $current"

    # OLD: explicit -InstallRef, or the newest release tag -- the version a
    # user who installed on release day is on.
    $oldRef = $InstallRef
    if (-not $oldRef -or $oldRef -eq "auto") {
        # Parens matter: without them PowerShell binds -split as an
        # argument to Invoke-Git instead of an operator on its result.
        $tagList = Invoke-Git @("-C", $RepoRoot, "tag", "--list", "v*", "--sort=-creatordate")
        $oldRef = ($tagList -split "\r?\n" | Select-Object -First 1)
        if (-not $oldRef) { throw "no v* release tags in the checkout and no -InstallRef given -- cannot pick an OLD version" }
    }
    $old = Invoke-Git @("-C", $RepoRoot, "rev-parse", "$oldRef^{commit}")
    Write-Host "  OLD  ($oldRef): $old"
    Assert-True ($old -ne $current) "OLD differs from HEAD (an update is genuinely available)"

    # Bare-clone the checkout: this is the repo the installer and updater
    # actually talk to. Local-path clone hardlinks objects, so it's fast
    # even for full history. The published installer carries NO commit pin
    # (Pin { commit: None, branch: main }) -- it installs whatever `main`
    # serves, so staging OLD means parking `main` there; the update phase
    # advances it to HEAD.
    Invoke-Git @("clone", "--bare", "--quiet", $RepoRoot, $ServeRepo) | Out-Null
    Invoke-Git @("-C", $ServeRepo, "update-ref", "refs/heads/main", $old) | Out-Null
    Invoke-Git @("-C", $ServeRepo, "symbolic-ref", "HEAD", "refs/heads/main") | Out-Null

    # Belt-and-braces: SOME installer builds do bake a -Commit pin. A pinned
    # sha is in serve.git's history but not at a ref tip, so the redirected
    # fetch needs any-SHA1 upload-pack permission (GitHub grants the
    # equivalent for fetch of reachable commits).
    Invoke-Git @("-C", $ServeRepo, "config", "uploadpack.allowAnySHA1InWant", "true") | Out-Null
    Write-Host "  serve.git: uploadpack.allowAnySHA1InWant=true (installer commit pin, if any)"

    @{ old = $old; old_ref = $oldRef; current = $current } |
        ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-Host "  state written: $StatePath"
    New-Item -ItemType Directory -Path $ProofRoot -Force | Out-Null
}

# ----------------------------------------------------------------------------
# Phase: install -- OLD's own install.ps1, headless
# ----------------------------------------------------------------------------
function Invoke-PhaseInstall {
    $state = Read-State
    $env:HERMES_HOME = $HermesHome
    New-Item -ItemType Directory -Path $HermesHome -Force | Out-Null
    Write-Step "INSTALL (script): OLD's own install.ps1, headless"
    Invoke-RefInstaller $state.old "old"
    Assert-True ((Get-InstalledHead) -eq $state.old) "installed checkout is at OLD"
    Test-HermesRuns "post-install-script"
}

function Invoke-PhaseUpdate {
    $state = Read-State
    $env:HERMES_HOME = $HermesHome
    New-Item -ItemType File -Path (Join-Path $HermesHome ".skip_upstream_prompt") -Force | Out-Null
    Invoke-Git @("-C", $ServeRepo, "update-ref", "refs/heads/main", $state.current) | Out-Null
    Write-Host "  serve.git main advanced to $($state.current)"

    switch ($Route) {
        "hermes-update" {
            Invoke-HermesUpdate
        }
        "installer-script" {
            Invoke-RefInstaller $state.current "head"
        }
    }

    Assert-True ((Get-InstalledHead) -eq $state.current) "checkout landed on HEAD"
    Test-HermesRuns "post-update"
}

function Invoke-CheckedPhaseUpdate {
    Remove-Item -LiteralPath (Join-Path $WorkRoot "known-failure.json") -Force -ErrorAction SilentlyContinue
    # Only evidence produced by this update attempt can match an exception.
    foreach ($oldLog in @((Join-Path $WorkRoot "logs\update.log"))) {
        if (Test-Path -LiteralPath $oldLog) { Move-Item -LiteralPath $oldLog -Destination "$oldLog.before-update" -Force }
    }
    try {
        Invoke-PhaseUpdate
    } catch {
        $failure = $_
        $node = Get-ManagedNode
        $classification = & $node (Join-Path $AssetsDir "known-failures.cjs") $WorkRoot $InstallMethod $Route $failure.Exception.Message
        $classificationExit = $LASTEXITCODE
        if ($classificationExit -ne 0) { throw $failure }
        $receipt = ($classification | Out-String) | ConvertFrom-Json
        Write-Host "KNOWN FAILURE [$($receipt.id)]: $($receipt.title)"
        Write-Host "  $($receipt.explanation)"
        if ($env:GITHUB_OUTPUT) {
            Add-Content -LiteralPath $env:GITHUB_OUTPUT -Value "known_failure=$($receipt.id)" -Encoding UTF8
        }
        if ($env:GITHUB_STEP_SUMMARY) {
            Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Encoding UTF8 -Value "Known historical failure: $($receipt.title). See the result chart footnote and uploaded known-failure.json."
        }
    }
}

# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------
Write-Host "Windows install/update E2E driver (real user flows)"
Write-Host "  phase:    $Phase"
Write-Host "  install:  $InstallMethod"
Write-Host "  route:    $Route"
Write-Host "  repo:     $RepoRoot"
Write-Host "  workroot: $WorkRoot"

$script:RealGitExe = (Get-Command git.exe -ErrorAction Stop).Source

Set-GitRedirect

switch ($Phase) {
    "stage"   { Invoke-PhaseStage }
    "install" { Invoke-PhaseInstall }
    "update"  { Invoke-CheckedPhaseUpdate }
    "all" {
        Invoke-PhaseStage
        Invoke-PhaseInstall
        Invoke-CheckedPhaseUpdate
    }
}

Write-Host ""
Write-Host "Phase '$Phase' completed successfully."
