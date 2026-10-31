<#
.SYNOPSIS
  中文唤醒词训练一键入口（T-3A.2）。

.DESCRIPTION
  torch 不在应用依赖白名单内（design §1.1），本脚本在独立 .venv-train 环境中运行训练脚本，
  不污染 .venv 与应用镜像。

.EXAMPLE
  .\tools\wakeword\run_train.ps1 -Stage all
  .\tools\wakeword\run_train.ps1 -Stage verify -Wav var\wakeword_train\positives\pos_0000.wav
#>
param(
    [ValidateSet('prepare', 'train', 'all', 'verify')]
    [string]$Stage = 'all',
    [string]$Config = 'tools/wakeword/train_zh_wakeword.yaml',
    [string]$Venv = '.venv-train',
    [string]$Python = 'python',
    [string[]]$Wav = @()
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$venvPython = Join-Path $root "$Venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "创建训练环境 $Venv（torch 体积较大，首次安装较慢）..."
    & $Python -m venv (Join-Path $root $Venv)
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r tools/wakeword/requirements-train.txt
}

$runArgs = @('tools/wakeword/train_zh_wakeword.py', $Stage, '--config', $Config)
if ($Wav.Count -gt 0) { $runArgs += '--wav'; $runArgs += $Wav }
& $venvPython @runArgs
exit $LASTEXITCODE
