# Azure VM Start/Stop Automation

![PowerShell](https://img.shields.io/badge/PowerShell-5391FE?style=flat&logo=powershell&logoColor=white)
![Azure](https://img.shields.io/badge/Azure-0078D4?style=flat&logo=microsoftazure&logoColor=white)

## Overview
This project automates the starting and stopping of Azure virtual machines based on Azure tags using PowerShell.

It is useful for managing dev/test VMs that do not need to run 24/7, helping reduce cloud costs and manual effort.

## Use Case
This automation is designed for managing non-production environments (e.g., Dev/Test) where virtual machines do not need to run continuously.

It helps:
- Reduce cloud costs
- Automate daily operations
- Avoid manual start/stop of resources

---

## Features
| Feature | What it does |
|---|---|
| Start by tag | Starts every VM matching a tag name/value pair |
| Stop by tag | Stops every VM matching a tag name/value pair |
| Tenant/subscription targeting | Runs against a specific tenant and subscription via parameters |
| Dry-run support | `-WhatIf` previews which VMs would be affected without acting |
| Skip logic | Skips VMs already in the desired state, running or stopped |
| Reusable parameters | No hardcoded tenant, subscription, or tag values in the script |

---

## Prerequisites
Before running the scripts, ensure you have:

- PowerShell 7
- Azure PowerShell module installed (`Az`)
- An Azure account with access to the target subscription
- Azure VMs tagged appropriately

---

## Example Tag
```text
Tag Name: Environment
Tag Value: Dev
```

---

## Usage

### Stop VMs
```powershell
./stop-vms-by-tag.ps1 -TenantId "YOUR-TENANT-ID" -SubscriptionId "YOUR-SUBSCRIPTION-ID" -TagName "Environment" -TagValue "Dev"
```

### Stop VMs (Dry Run)
```powershell
./stop-vms-by-tag.ps1 -TenantId "YOUR-TENANT-ID" -SubscriptionId "YOUR-SUBSCRIPTION-ID" -TagName "Environment" -TagValue "Dev" -WhatIf
```

---

### Start VMs
```powershell
./start-vms-by-tag.ps1 -TenantId "YOUR-TENANT-ID" -SubscriptionId "YOUR-SUBSCRIPTION-ID" -TagName "Environment" -TagValue "Dev"
```

### Start VMs (Dry Run)
```powershell
./start-vms-by-tag.ps1 -TenantId "YOUR-TENANT-ID" -SubscriptionId "YOUR-SUBSCRIPTION-ID" -TagName "Environment" -TagValue "Dev" -WhatIf
```

---

## Notes
| Note | Detail |
|---|---|
| Dry run first | Always use `-WhatIf` first to validate changes before execution |
| Tag accuracy | Ensure correct tag values are applied to avoid unintended actions |
| Scope | Designed for automation scenarios such as dev/test cost optimization |

---

## Author
Built as part of a cloud automation learning project to improve Azure and PowerShell skills.
