# # Variables
# Install-Module Microsoft.Graph.Applications -Scope CurrentUser -Force
# Install-Module Microsoft.Graph.Sites -Scope CurrentUser -Force


# 1. Login to Azure
Connect-AzAccount -UseDeviceAuthentication

# 2. Reuse Az token for Microsoft Graph (no second login)
$tokenObj = Get-AzAccessToken -ResourceTypeName MSGraph
if ($tokenObj.Token -is [securestring]) {
    $secureToken = $tokenObj.Token
} else {
    $secureToken = ConvertTo-SecureString $tokenObj.Token -AsPlainText -Force
}
Connect-MgGraph -AccessToken $secureToken -NoWelcome

$functionAppName = "func-mermaid-poc-2026"
$resourceGroup = "rg-jt-poc"

# Récupérer l'Object ID de la Managed Identity
$mi = (Get-AzFunctionApp -Name $functionAppName -ResourceGroupName $resourceGroup).IdentityPrincipalId
# OU si System-assigned:
$mi = (Get-AzWebApp -Name $functionAppName -ResourceGroupName $resourceGroup).Identity.PrincipalId

# Object ID du Service Principal "Microsoft Graph" (constant dans tous les tenants)
$graphSpId = (Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'").Id

# Trouver l'ID du rôle Sites.Selected
$siteSelectedRole = (Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'").AppRoles | 
    Where-Object { $_.Value -eq "Sites.Selected" }

# Assigner le rôle applicatif
New-MgServicePrincipalAppRoleAssignment `
    -ServicePrincipalId $mi `
    -PrincipalId $mi `
    -ResourceId $graphSpId `
    -AppRoleId $siteSelectedRole.Id