# ==============================================================================
# BusinessBooster - Deploy All Knowledge Sources (Index Only - ACL Enabled)
# ==============================================================================
# This script deploys AI Search indexes with ACL-based permission filtering for:
# - marketing: Marketing files
# - programs: Account team activation programs
# - pitchdeck: Sales pitch decks for Azure technologies
#
# ACL-based filtering uses permissionFilterOption and UserIds/GroupIds fields
# to enable query-time security trimming based on user identity.
# See: https://learn.microsoft.com/en-us/azure/search/search-query-access-control-rbac-enforcement
# ==============================================================================

param(
    [Parameter(Mandatory = $false)]
    [string]$ResourceGroupName = "Rg-hack-26",
    
    [Parameter(Mandatory = $false)]
    [string]$SearchServiceName = "ais-hackathon",
    
    [Parameter(Mandatory = $false)]
    [string]$StorageAccountName = "storagehackathongris",
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIResourceUri = "https://foundry-hackathon-gris.cognitiveservices.azure.com",
    
    [Parameter(Mandatory = $false)]
    [string]$ChatCompletionDeploymentId = "gpt-4.1",
    
    [Parameter(Mandatory = $false)]
    [string]$EmbeddingDeploymentId = "text-embedding-3-large",
    
    [Parameter(Mandatory = $false)]
    [string]$EmbeddingModelName = "text-embedding-3-large",
    
    [Parameter(Mandatory = $false)]
    [int]$EmbeddingDimensions = 3072,
    
    [Parameter(Mandatory = $false)]
    [string]$ApiVersion = "2025-11-01-preview",
    
    [Parameter(Mandatory = $false)]
    [switch]$WhatIf
)

$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "BusinessBooster Knowledge Source Deployment" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Define knowledge sources to deploy
$knowledgeSources = @(
    @{
        Name = "marketing"
        Description = "Marketing files"
        Container = "marketing"
    },
    @{
        Name = "programs"
        Description = "Account team activation programs"
        Container = "programs"
    },
    @{
        Name = "pitchdeck"
        Description = "Sales pitch decks for Azure technologies"
        Container = "pitchdeck"
    }
)

foreach ($ks in $knowledgeSources) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "Deploying: $($ks.Name) - $($ks.Description)" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    
    $ksDir = Join-Path $scriptDir $ks.Name
    
    # Build parameters for the main script
    $params = @{
        ResourceGroupName = $ResourceGroupName
        SearchServiceName = $SearchServiceName
        StorageAccountName = $StorageAccountName
        OpenAIResourceUri = $OpenAIResourceUri
        ChatCompletionDeploymentId = $ChatCompletionDeploymentId
        OpenAIDeploymentId = $EmbeddingDeploymentId
        OpenAIModelName = $EmbeddingModelName
        OpenAIEmbeddingDimensions = $EmbeddingDimensions
        StorageContainerName = $ks.Container
        IndexName = "businessbooster-$($ks.Name)-index"
        DataSourceName = "businessbooster-$($ks.Name)-datasource"
        SkillsetName = "businessbooster-$($ks.Name)-skillset"
        IndexerName = "businessbooster-$($ks.Name)-indexer"
        ApiVersion = $ApiVersion
    }
    
    if ($WhatIf) {
        $params.Add("WhatIf", $true)
    }
    
    # Check if custom artifacts exist in subfolder
    $customDataSource = Join-Path $ksDir "datasource.json"
    $customIndex = Join-Path $ksDir "index.json"
    $customSkillset = Join-Path $ksDir "skillset.json"
    $customIndexer = Join-Path $ksDir "indexer.json"
    
    if (Test-Path $customDataSource) {
        Write-Host "Using custom artifacts from: $ksDir" -ForegroundColor Green
        
        # Deploy using custom artifacts
        & "$scriptDir\deploy-single-ks.ps1" @params -ArtifactDir $ksDir
    } else {
        Write-Host "Using default artifacts with container: $($ks.Container)" -ForegroundColor Yellow
        
        # Deploy using main script
        & "$scriptDir\script.ps1" @params
    }
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to deploy $($ks.Name)" -ForegroundColor Red
    } else {
        Write-Host "Successfully deployed $($ks.Name)" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Deployment Complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Deployed indexes with ACL filtering:" -ForegroundColor Yellow
foreach ($ks in $knowledgeSources) {
    Write-Host "  - businessbooster-$($ks.Name)-index (permissionFilterOption: enabled)" -ForegroundColor Gray
}
Write-Host ""
Write-Host "ACL Query-Time Filtering:" -ForegroundColor Cyan
Write-Host "  - UserIds and GroupIds fields are indexed from blob metadata" -ForegroundColor Gray
Write-Host "  - Use x-ms-query-source-authorization header with user token for filtered results" -ForegroundColor Gray
Write-Host "  - API Version: $ApiVersion (preview required for ACL enforcement)" -ForegroundColor Gray
