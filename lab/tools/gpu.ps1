# Pod remote control from your Windows laptop, so you can check on / stop the
# GPU without opening the RunPod website.
#
# One-time setup:
#   1. Install runpodctl: download runpodctl-windows-amd64.exe from
#      https://github.com/runpod/runpodctl/releases, rename to runpodctl.exe,
#      put it somewhere on your PATH.
#   2. runpodctl config --apiKey YOUR_RUNPOD_API_KEY
#      (create the key at runpod.io -> Settings -> API Keys)
#
# Usage (from the lab/ directory or anywhere):
#   .\tools\gpu.ps1 status              # list pods and whether they are running
#   .\tools\gpu.ps1 stop  <podId>       # stop a pod NOW (volume is safe)
#   .\tools\gpu.ps1 start <podId>       # restart a stopped pod
#
# Note: starting a stopped pod re-requests the same GPU type in the same
# datacenter; if none are free at that moment, deploy a fresh pod from the
# website instead. The network volume keeps models/results either way.

param(
    [Parameter(Mandatory = $true)][ValidateSet("status", "start", "stop")]
    [string]$Command,
    [string]$PodId
)

if (-not (Get-Command runpodctl -ErrorAction SilentlyContinue)) {
    Write-Host "runpodctl not found. See the setup comment at the top of this script."
    exit 1
}

switch ($Command) {
    "status" {
        runpodctl get pod
    }
    "start" {
        if (-not $PodId) { Write-Host "usage: gpu.ps1 start <podId>  (find ids via: gpu.ps1 status)"; exit 1 }
        runpodctl start pod $PodId
    }
    "stop" {
        if (-not $PodId) { Write-Host "usage: gpu.ps1 stop <podId>  (find ids via: gpu.ps1 status)"; exit 1 }
        runpodctl stop pod $PodId
        Write-Host "stop requested. Verify with: .\tools\gpu.ps1 status"
    }
}
