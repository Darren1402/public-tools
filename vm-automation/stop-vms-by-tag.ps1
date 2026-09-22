[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$TenantId,

    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $true)]
    [string]$TagName,

    [Parameter(Mandatory = $true)]
    [string]$TagValue,

    [switch]$Login
)

$ErrorActionPreference = "Stop"

try {
    if ($Login) {
        Write-Host "Connecting to Azure..." -ForegroundColor Cyan
        Connect-AzAccount -Tenant $TenantId -Subscription $SubscriptionId
    }

    Write-Host "Setting Azure context..." -ForegroundColor Cyan
    Set-AzContext -Tenant $TenantId -SubscriptionId $SubscriptionId | Out-Null

    Write-Host "Getting VMs from subscription..." -ForegroundColor Cyan
    $vms = Get-AzVM -Status

    $filteredVMs = $vms | Where-Object {
        $_.Tags[$TagName] -eq $TagValue
    }

    if (-not $filteredVMs) {
        Write-Host "No VMs found with tag $TagName = $TagValue" -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host "Matched VMs:" -ForegroundColor Green
    $filteredVMs | Select-Object Name, ResourceGroupName, Location, PowerState | Format-Table -AutoSize
    Write-Host ""

    foreach ($vm in $filteredVMs) {
        if ($vm.PowerState -eq "VM deallocated" -or $vm.PowerState -eq "VM stopped") {
            Write-Host "Skipping $($vm.Name) because it is already stopped." -ForegroundColor Yellow
            continue
        }

        if ($PSCmdlet.ShouldProcess($vm.Name, "Stop Azure VM")) {
            Write-Host "Stopping VM: $($vm.Name) in Resource Group: $($vm.ResourceGroupName)" -ForegroundColor Red
            Stop-AzVM -ResourceGroupName $vm.ResourceGroupName -Name $vm.Name -Force
        }
    }

    Write-Host ""
    Write-Host "Script completed successfully." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "Script failed: $($_.Exception.Message)" -ForegroundColor Red
}
