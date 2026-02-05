# ==============================================================================
# Azure AI Search Deployment Script
# ==============================================================================
# This script deploys AI Search components based on existing JSON artifacts:
# - Data Source (Azure Blob Storage)
# - Index with vector search capabilities
# - Skillset with OCR, text splitting, and embeddings
# - Indexer to process documents
# Uses Managed Identity for authentication (no API keys required)
# ==============================================================================

param(
    [Parameter(Mandatory = $false)]
    [string]$TenantId,
    
    [Parameter(Mandatory = $false)]
    [string]$SubscriptionId,
    
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName,
    
    [Parameter(Mandatory = $true)]
    [string]$SearchServiceName,
    
    [Parameter(Mandatory = $true)]
    [string]$StorageAccountName,
    
    [Parameter(Mandatory = $false)]
    [string]$SearchAdminKey,
    
    # OpenAI Configuration
    [Parameter(Mandatory = $false)]
    [string]$OpenAIResourceUri,
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIDeploymentId = "text-embedding-3-large",
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIModelName = "text-embedding-3-large",
    
    [Parameter(Mandatory = $false)]
    [int]$OpenAIEmbeddingDimensions = 3072,
    
    # Chat Completion Model (for image verbalization)
    [Parameter(Mandatory = $false)]
    [string]$ChatCompletionDeploymentId = "gpt-4o",
    
    # OpenAI API Key (use when managed identity is not applicable)
    [Parameter(Mandatory = $false)]
    [string]$OpenAIApiKey,
    
    # Cognitive Services Configuration (for Vision skills)
    [Parameter(Mandatory = $false)]
    [string]$CognitiveServicesResourceUri,
    
    # Cognitive Services API Key (use when managed identity is not applicable)
    [Parameter(Mandatory = $false)]
    [string]$CognitiveServicesApiKey,
    
    # Storage Configuration
    [Parameter(Mandatory = $false)]
    [string]$StorageResourceGroupName,  # Resource group for storage account (defaults to ResourceGroupName if not specified)
    
    [Parameter(Mandatory = $false)]
    [string]$StorageContainerName = "inputdata",
    
    # AI Search Component Names
    [Parameter(Mandatory = $false)]
    [string]$IndexName = "vector-jt-poc",
    
    [Parameter(Mandatory = $false)]
    [string]$DataSourceName = "vector-jt-poc-datasource",
    
    [Parameter(Mandatory = $false)]
    [string]$SkillsetName = "vector-jt-poc-skillset",
    
    [Parameter(Mandatory = $false)]
    [string]$IndexerName = "vector-jt-poc-indexer",
    
    # Vector Search Configuration Names
    [Parameter(Mandatory = $false)]
    [string]$VectorSearchProfile = "vector-jt-poc-azureOpenAi-text-profile",
    
    [Parameter(Mandatory = $false)]
    [string]$VectorizerName = "vector-jt-poc-azureOpenAi-text-vectorizer",
    
    [Parameter(Mandatory = $false)]
    [string]$AlgorithmName = "vector-jt-poc-algorithm",
    
    [Parameter(Mandatory = $false)]
    [string]$SemanticConfigName = "vector-jt-poc-semantic-configuration",
    
    [Parameter(Mandatory = $false)]
    [string]$ApiVersion = "2025-11-01-preview",
    
    [Parameter(Mandatory = $false)]
    [switch]$WhatIf,

    [Parameter(Mandatory = $false)]
    [switch]$DebugAuth
)

# Management API version for ARM operations (listAdminKeys)
$managementApiVersion = '2022-09-01'

# Get script directory for resolving JSON file paths
$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
Write-Host "Script directory: $scriptDir" -ForegroundColor Gray

# ==============================================================================
# Functions
# ==============================================================================

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Error {
    param([string]$Message)
    Write-Host "✗ $Message" -ForegroundColor Red
}

function Use-AzAuthentication {
    param(
        [string]$Tenant,
        [string]$Subscription
    )

    # Try current Az context
    try {
        $ctx = Get-AzContext -ErrorAction SilentlyContinue
        if ($ctx -and $ctx.Account -and ($null -eq $Subscription -or $ctx.Subscription.Id -eq $Subscription)) {
            return $true
        }
    } catch {}

    # Try Service Principal from env
    if ($env:AZURE_CLIENT_ID -and $env:AZURE_CLIENT_SECRET -and ($Tenant -or $env:AZURE_TENANT_ID)) {
        Write-Host "Attempting service principal authentication using environment variables" -ForegroundColor Yellow
        try {
            $spTenant = $Tenant; if (-not $spTenant) { $spTenant = $env:AZURE_TENANT_ID }
            Connect-AzAccount -ServicePrincipal -Tenant $spTenant -ApplicationId $env:AZURE_CLIENT_ID -Credential (New-Object System.Management.Automation.PSCredential($env:AZURE_CLIENT_ID,(ConvertTo-SecureString $env:AZURE_CLIENT_SECRET -AsPlainText -Force))) -ErrorAction Stop
            if ($Subscription) { Set-AzContext -Subscription $Subscription -ErrorAction SilentlyContinue }
            $global:AuthMethod = 'ServicePrincipal'
            if ($DebugAuth) { Write-Host "Auth method: ServicePrincipal" -ForegroundColor Cyan }
            Write-Success "Authenticated with service principal"
            return $true
        }
        catch {
            Write-Host "Service principal auth failed: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    # Try Azure CLI token -> Az context
    try {
        if (Get-Command az -ErrorAction SilentlyContinue) {
            $azAccount = az account show 2>$null | ConvertFrom-Json
            if ($azAccount) {
                Write-Host "Found Azure CLI login, importing to Az context" -ForegroundColor Yellow
                $token = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv 2>$null
                if ($token) {
                    # Record CLI token for later REST fallback
                    $global:AzCliAccessToken = $token
                    $global:AuthMethod = 'AzureCLI'
                    if ($DebugAuth) { Write-Host "Auth method: AzureCLI (token acquired)" -ForegroundColor Cyan }
                    Write-Success "Using Azure CLI token for REST operations"
                    return $true
                }
            }
        }
    }
    catch { Write-Host "Azure CLI auth attempt failed: $($_.Exception.Message)" -ForegroundColor Yellow }

    # Last resort: interactive login
    try {
        Write-Host "Falling back to interactive login (you may be prompted)" -ForegroundColor Yellow
        if ($Tenant -and $Subscription) {
            Connect-AzAccount -Tenant $Tenant -Subscription $Subscription -ErrorAction Stop
        } else {
            Connect-AzAccount -ErrorAction Stop
        }
            $global:AuthMethod = 'Interactive'
            if ($DebugAuth) { Write-Host "Auth method: Interactive" -ForegroundColor Cyan }
            Write-Success "Authenticated interactively"
        return $true
    }
    catch {
        Write-Error "Interactive login failed: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-SearchApi {
    param(
        [string]$Method,
        [string]$Endpoint,
        [string]$Body = $null,
        [hashtable]$Headers
    )
    
    try {
        if ($Body) {
            $response = Invoke-RestMethod -Uri $Endpoint -Headers $Headers -Method $Method -Body $Body
        } else {
            $response = Invoke-RestMethod -Uri $Endpoint -Headers $Headers -Method $Method
        }
        return $response
    }
    catch {
        Write-Error "API call failed: $($_.Exception.Message)"
        
        # Try to get more detailed error information
        if ($_.Exception.Response) {
            try {
                $statusCode = $_.Exception.Response.StatusCode
                $reasonPhrase = $_.Exception.Response.ReasonPhrase
                Write-Host "HTTP Status: $statusCode - $reasonPhrase" -ForegroundColor Red
                
                # Try to read response content for more details
                if ($_.ErrorDetails) {
                    Write-Host "Error details: $($_.ErrorDetails.Message)" -ForegroundColor Red
                }
            }
            catch {
                Write-Host "Could not retrieve detailed error information" -ForegroundColor Yellow
            }
        }
        throw
    }
}

function Update-JsonContent {
    param(
        [string]$JsonContent,
        [hashtable]$Replacements
    )
    
    $updatedContent = $JsonContent
    
    foreach ($key in $Replacements.Keys) {
        $value = $Replacements[$key]
        $updatedContent = $updatedContent -replace [regex]::Escape($key), $value
    }
    
    return $updatedContent
}

function Validate-StorageConnectionString {
    param([string]$ConnectionString)
    if (-not $ConnectionString) {
        throw "Storage connection string is empty or not set. Ensure storage key retrieval succeeded."
    }

    $valid = $false
    if ($ConnectionString -match 'ResourceId=.*;') { $valid = $true }
    if ($ConnectionString -match 'DefaultEndpointsProtocol=.*AccountName=.*;AccountKey=.*') { $valid = $true }
    if ($ConnectionString -match 'BlobEndpoint=.*;SharedAccessSignature=.*') { $valid = $true }
    if ($ConnectionString -match 'ContainerSharedAccessUri=.*') { $valid = $true }

    if (-not $valid) {
        $masked = $ConnectionString
        if ($masked.Length -gt 20) { $masked = $masked.Substring(0,10) + '...'+ $masked.Substring($masked.Length-7) }
        throw "Storage connection string does not match expected formats. Masked value: $masked"
    }
}

# ==============================================================================
# Main Script
# ==============================================================================

Write-Host "Starting Azure AI Search deployment..." -ForegroundColor Yellow
Write-Host "Resource Group: $ResourceGroupName" -ForegroundColor Gray
Write-Host "Search Service: $SearchServiceName" -ForegroundColor Gray
Write-Host "Storage Account: $StorageAccountName" -ForegroundColor Gray

# Get Azure account information from CLI if not provided
if (-not $TenantId -or -not $SubscriptionId) {
    Write-Step "Retrieving Azure account information from CLI"
    try {
        $azAccount = az account show | ConvertFrom-Json -ErrorAction Stop
        if (-not $azAccount) {
            throw "No Azure account found. Please run 'az login' first."
        }
        
        if (-not $TenantId) {
            $TenantId = $azAccount.tenantId
            Write-Host "  Using Tenant ID from CLI: $TenantId" -ForegroundColor Gray
        }
        
        if (-not $SubscriptionId) {
            $SubscriptionId = $azAccount.id
            Write-Host "  Using Subscription ID from CLI: $SubscriptionId" -ForegroundColor Gray
        }
        
        Write-Host "  Subscription Name: $($azAccount.name)" -ForegroundColor Gray
        Write-Success "Retrieved Azure account information from CLI"
    }
    catch {
        Write-Error "Failed to get Azure account information from CLI: $($_.Exception.Message)"
        Write-Host "Please ensure you are logged in to Azure CLI with 'az login'" -ForegroundColor Yellow
        exit 1
    }
}

# Connect to Azure
Write-Step "Ensuring Azure authentication"
if (-not (Use-AzAuthentication -Tenant $TenantId -Subscription $SubscriptionId)) {
    Write-Error "Could not authenticate to Azure. Ensure Azure CLI or Az module is configured, or set AZURE_CLIENT_ID/AZURE_CLIENT_SECRET/AZURE_TENANT_ID for service principal auth."
    exit 1
}

# Get Search Service Admin Key
Write-Step "Retrieving Search Service admin key"

if ($SearchAdminKey) {
    $primaryAdminKey = $SearchAdminKey
    Write-Success "Using provided Search admin key"
} else {
    try {
        # First try using Azure PowerShell module
        try {
            $adminKeyPair = Get-AzSearchAdminKeyPair -ResourceGroupName $ResourceGroupName -ServiceName $SearchServiceName -ErrorAction Stop
            $primaryAdminKey = $adminKeyPair.Primary
            Write-Success "Retrieved admin key using Azure PowerShell module"
        }
        catch {
            Write-Host "Azure Search PowerShell module not available, using REST API..." -ForegroundColor Yellow

            # Prefer Az CLI token if we acquired it earlier
            if ($global:AzCliAccessToken) {
                $token = $global:AzCliAccessToken
            }
            else {
                # Try to get an access token from the Az context (best-effort)
                try {
                    $context = Get-AzContext
                    $token = [Microsoft.Azure.Commands.Common.Authentication.AzureSession]::Instance.AuthenticationFactory.Authenticate($context.Account, $context.Environment, $context.Tenant.Id, $null, "Never", $null, "https://management.azure.com/").AccessToken
                }
                catch {
                    $token = $null
                }
            }

            if (-not $token) {
                throw "Could not obtain an access token for REST API calls"
            }

            # Use REST API to get admin keys
            $headers = @{
                'Authorization' = "Bearer $token"
                'Content-Type' = 'application/json'
            }

            $url = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Search/searchServices/$SearchServiceName/listAdminKeys?api-version=$managementApiVersion"
            $response = Invoke-RestMethod -Uri $url -Headers $headers -Method POST
            $primaryAdminKey = $response.primaryKey
            Write-Success "Retrieved admin key using REST API"
        }
    }
    catch {
        Write-Error "Failed to retrieve admin key: $($_.Exception.Message)"
        Write-Host "You can provide the Search admin key directly using -SearchAdminKey parameter" -ForegroundColor Yellow
        exit 1
    }
}

# Get Storage Account Resource ID for managed identity connection
Write-Step "Building Storage Account Resource ID"
# Use StorageResourceGroupName if provided, otherwise fall back to ResourceGroupName
if (-not $StorageResourceGroupName) {
    $StorageResourceGroupName = $ResourceGroupName
}
$storageResourceId = "/subscriptions/$SubscriptionId/resourceGroups/$StorageResourceGroupName/providers/Microsoft.Storage/storageAccounts/$StorageAccountName"
$storageConnectionString = "ResourceId=$storageResourceId;"
Write-Success "Storage Resource ID built: $storageResourceId"

# Prepare headers for REST API calls
$headers = @{
    'api-key' = $primaryAdminKey
    'Content-Type' = 'application/json'
    'Accept' = 'application/json'
}

$baseUrl = "https://$SearchServiceName.search.windows.net"

# ==============================================================================
# Parameter Setup and Validation
# ==============================================================================

Write-Step "Setting up deployment parameters"

# Set default OpenAI Resource URI if not provided (skip if not needed)
if (-not $OpenAIResourceUri) {
    Write-Host "OpenAI Resource URI not provided - will use placeholder in JSON files" -ForegroundColor Yellow
}

# Set default Cognitive Services URI if not provided
if (-not $CognitiveServicesResourceUri) {
    Write-Host "Cognitive Services Resource URI not provided - will use placeholder in JSON files" -ForegroundColor Yellow
}

# Determine authentication method for OpenAI
$useOpenAIApiKey = $false
if ($OpenAIApiKey) {
    $useOpenAIApiKey = $true
    Write-Host "  Using OpenAI API Key authentication" -ForegroundColor Yellow
} else {
    Write-Host "  Using Managed Identity for OpenAI" -ForegroundColor Green
}

# Determine authentication method for Cognitive Services
$useCognitiveServicesApiKey = $false
if ($CognitiveServicesApiKey) {
    $useCognitiveServicesApiKey = $true
    Write-Host "  Using Cognitive Services API Key authentication" -ForegroundColor Yellow
} else {
    Write-Host "  Using Managed Identity for Cognitive Services" -ForegroundColor Green
}

Write-Host "Configuration:" -ForegroundColor Gray
Write-Host "  Index Name: $IndexName" -ForegroundColor Gray
Write-Host "  Data Source: $DataSourceName" -ForegroundColor Gray
Write-Host "  Skillset: $SkillsetName" -ForegroundColor Gray
Write-Host "  Indexer: $IndexerName" -ForegroundColor Gray
Write-Host "  OpenAI URI: $OpenAIResourceUri" -ForegroundColor Gray
Write-Host "  Embedding Model: $OpenAIDeploymentId (Dimensions: $OpenAIEmbeddingDimensions)" -ForegroundColor Gray
Write-Host "  Chat Completion Model: $ChatCompletionDeploymentId" -ForegroundColor Gray
Write-Host "  OpenAI Auth: $(if ($useOpenAIApiKey) { 'API Key' } else { 'Managed Identity' })" -ForegroundColor Gray
Write-Host "  Cognitive Services URI: $CognitiveServicesResourceUri" -ForegroundColor Gray
Write-Host "  Storage Container: $StorageContainerName" -ForegroundColor Gray
Write-Host "  Using System-Assigned Managed Identity for Storage" -ForegroundColor Green

# ==============================================================================
# Deploy Data Source
# ==============================================================================

Write-Step "Deploying Data Source"

# Load datasource.json and replace template placeholders
$dataSourceJsonPath = Join-Path $scriptDir "datasource.json"
$rawDataSource = Get-Content -Path $dataSourceJsonPath -Raw

# Replace template placeholders
$dataSourceReplacements = @{
    '${dataSourceName}' = "$DataSourceName"
    '${containerName}' = "$StorageContainerName"
    '${subscriptionId}' = "$SubscriptionId"
    '${resourceGroup}' = "$StorageResourceGroupName"
    '${storageAccount}' = "$StorageAccountName"
}
$rawDataSource = Update-JsonContent -JsonContent $rawDataSource -Replacements $dataSourceReplacements

try {
    $dataSourceObj = $rawDataSource | ConvertFrom-Json -ErrorAction Stop
}
catch {
    Write-Error "Failed to parse datasource.json as JSON: $($_.Exception.Message)"
    throw
}

# Ensure basic properties are set from parameters (already done via placeholders)
if ($dataSourceObj.container) { $dataSourceObj.container.name = $StorageContainerName } else { $dataSourceObj | Add-Member -MemberType NoteProperty -Name container -Value @{ name = $StorageContainerName } }

# Set the credentials.connectionString to use ResourceId format for managed identity
$dataSourceObj.credentials.connectionString = $storageConnectionString

# Use system-assigned managed identity (null means system-assigned)
$dataSourceObj.identity = $null

# Re-serialize payload for the REST call
$dataSourceDefinition = $dataSourceObj | ConvertTo-Json -Depth 10

    if ($WhatIf) {
        Write-Host "Would deploy data source with definition:" -ForegroundColor Yellow
        Write-Host $dataSourceDefinition -ForegroundColor Gray
    } else {
        try {
            $dsApiVersion = $ApiVersion

            $dataSourceUrl = "$baseUrl/datasources/$DataSourceName" + "?api-version=$dsApiVersion"
            $response = Invoke-SearchApi -Method "PUT" -Endpoint $dataSourceUrl -Body $dataSourceDefinition -Headers $headers
            Write-Success "Data source deployed successfully (using managed identity)"
        }
        catch {
            Write-Error "Failed to deploy data source"
            throw
        }
}

# ==============================================================================
# Deploy Index
# ==============================================================================

Write-Step "Deploying Index"

$indexJsonPath = Join-Path $scriptDir "index.json"
$indexDefinition = Get-Content -Path $indexJsonPath -Raw

# Define parameter replacements for index (using template placeholders)
$indexReplacements = @{
    '${indexName}' = "$IndexName"
    '${embeddingDimensions}' = "$OpenAIEmbeddingDimensions"
    '${openAIResourceUri}' = "$OpenAIResourceUri"
    '${embeddingDeploymentId}' = "$OpenAIDeploymentId"
    '${embeddingModelName}' = "$OpenAIModelName"
    '${cognitiveServicesResourceUri}' = "$CognitiveServicesResourceUri"
}

# Apply parameter replacements
$indexDefinition = Update-JsonContent -JsonContent $indexDefinition -Replacements $indexReplacements

# Handle API Key authentication for vectorizers in index
if ($useOpenAIApiKey -and $OpenAIApiKey) {
    # Parse JSON, add apiKey to azureOpenAIParameters, re-serialize
    $indexObj = $indexDefinition | ConvertFrom-Json
    foreach ($vectorizer in $indexObj.vectorSearch.vectorizers) {
        if ($vectorizer.kind -eq "azureOpenAI" -and $vectorizer.azureOpenAIParameters) {
            $vectorizer.azureOpenAIParameters | Add-Member -MemberType NoteProperty -Name "apiKey" -Value $OpenAIApiKey -Force
        }
    }
    $indexDefinition = $indexObj | ConvertTo-Json -Depth 20
}

if ($useCognitiveServicesApiKey -and $CognitiveServicesApiKey) {
    # Parse JSON, add apiKey to aiServicesVisionParameters, re-serialize
    $indexObj = $indexDefinition | ConvertFrom-Json
    foreach ($vectorizer in $indexObj.vectorSearch.vectorizers) {
        if ($vectorizer.kind -eq "aiServicesVision" -and $vectorizer.aiServicesVisionParameters) {
            $vectorizer.aiServicesVisionParameters | Add-Member -MemberType NoteProperty -Name "apiKey" -Value $CognitiveServicesApiKey -Force
        }
    }
    $indexDefinition = $indexObj | ConvertTo-Json -Depth 20
}

if ($WhatIf) {
    Write-Host "Would deploy index with definition:" -ForegroundColor Yellow
    Write-Host $indexDefinition -ForegroundColor Gray
} else {
    try {
        $indexUrl = "$baseUrl/indexes/$IndexName" + "?api-version=$ApiVersion"
        $response = Invoke-SearchApi -Method "PUT" -Endpoint $indexUrl -Body $indexDefinition -Headers $headers
        Write-Success "Index deployed successfully (using managed identity)"
    }
    catch {
        Write-Error "Failed to deploy index"
        throw
    }
}

# ==============================================================================
# Deploy Skillset
# ==============================================================================

Write-Step "Deploying Skillset"

$skillsetJsonPath = Join-Path $scriptDir "skillset.json"
$skillsetDefinition = Get-Content -Path $skillsetJsonPath -Raw

# Define parameter replacements for skillset (using managed identity with template placeholders)
$skillsetReplacements = @{
    '${skillsetName}' = "$SkillsetName"
    '${indexName}' = "$IndexName"
    '${openAIResourceUri}' = "$OpenAIResourceUri"
    '${chatCompletionDeploymentId}' = "$ChatCompletionDeploymentId"
    '${embeddingDeploymentId}' = "$OpenAIDeploymentId"
    '${embeddingDimensions}' = "$OpenAIEmbeddingDimensions"
    '${embeddingModelName}' = "$OpenAIModelName"
    '${cognitiveServicesResourceUri}' = "$CognitiveServicesResourceUri"
    '${subscriptionId}' = "$SubscriptionId"
    '${resourceGroup}' = "$ResourceGroupName"
    '${storageAccount}' = "$StorageAccountName"
}

$skillsetDefinition = Update-JsonContent -JsonContent $skillsetDefinition -Replacements $skillsetReplacements

# Handle API Key authentication for skills in skillset (only when NOT using managed identity)
if ($useOpenAIApiKey -and $OpenAIApiKey) {
    # Parse JSON, add apiKey to embedding and chat completion skills, re-serialize
    $skillsetObj = $skillsetDefinition | ConvertFrom-Json
    foreach ($skill in $skillsetObj.skills) {
        if ($skill.'@odata.type' -eq "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill") {
            $skill | Add-Member -MemberType NoteProperty -Name "apiKey" -Value $OpenAIApiKey -Force
        }
        if ($skill.'@odata.type' -eq "#Microsoft.Skills.Custom.ChatCompletionSkill") {
            $skill | Add-Member -MemberType NoteProperty -Name "apiKey" -Value $OpenAIApiKey -Force
        }
    }
    $skillsetDefinition = $skillsetObj | ConvertTo-Json -Depth 20
} else {
    Write-Host "  Using Managed Identity for OpenAI skills (no apiKey set)" -ForegroundColor Green
}

# Handle Cognitive Services API Key for Vision skills
if ($useCognitiveServicesApiKey -and $CognitiveServicesApiKey) {
    $skillsetObj = $skillsetDefinition | ConvertFrom-Json
    # Update cognitiveServices section with API key
    $skillsetObj.cognitiveServices = @{
        '@odata.type' = '#Microsoft.Azure.Search.CognitiveServicesByKey'
        'key' = $CognitiveServicesApiKey
    }
    $skillsetDefinition = $skillsetObj | ConvertTo-Json -Depth 20
}

if ($WhatIf) {
    Write-Host "Would deploy skillset with definition:" -ForegroundColor Yellow
    Write-Host $skillsetDefinition -ForegroundColor Gray
} else {
    try {
    $skillsetUrl = "$baseUrl/skillsets/$SkillsetName" + "?api-version=$ApiVersion"
        $response = Invoke-SearchApi -Method "PUT" -Endpoint $skillsetUrl -Body $skillsetDefinition -Headers $headers
        Write-Success "Skillset deployed successfully (using managed identity)"
    }
    catch {
        Write-Error "Failed to deploy skillset"
        throw
    }
}

# ==============================================================================
# Deploy Indexer
# ==============================================================================

Write-Step "Deploying Indexer"

$indexerJsonPath = Join-Path $scriptDir "indexer.json"
$indexerDefinition = Get-Content -Path $indexerJsonPath -Raw

# Define parameter replacements for indexer (using template placeholders)
$indexerReplacements = @{
    '${indexerName}' = "$IndexerName"
    '${dataSourceName}' = "$DataSourceName"
    '${skillsetName}' = "$SkillsetName"
    '${indexName}' = "$IndexName"
}

$indexerDefinition = Update-JsonContent -JsonContent $indexerDefinition -Replacements $indexerReplacements

if ($WhatIf) {
    Write-Host "Would deploy indexer with definition:" -ForegroundColor Yellow
    Write-Host $indexerDefinition -ForegroundColor Gray
} else {
    try {
    $indexerUrl = "$baseUrl/indexers/$IndexerName" + "?api-version=$ApiVersion"
        $response = Invoke-SearchApi -Method "PUT" -Endpoint $indexerUrl -Body $indexerDefinition -Headers $headers
        Write-Success "Indexer deployed successfully"
    }
    catch {
        Write-Error "Failed to deploy indexer"
        throw
    }
}

# ==============================================================================
# Run Indexer
# ==============================================================================

Write-Step "Running Indexer"

if ($WhatIf) {
    Write-Host "Would run indexer: faq-vector-db-indexer" -ForegroundColor Yellow
} else {
    try {
    $runIndexerUrl = "$baseUrl/indexers/$IndexerName/run?api-version=$ApiVersion"
        $response = Invoke-SearchApi -Method "POST" -Endpoint $runIndexerUrl -Headers $headers
        Write-Success "Indexer started successfully"
    }
    catch {
        Write-Error "Failed to run indexer"
        throw
    }
}

# ==============================================================================
# Verify Deployment
# ==============================================================================

Write-Step "Verifying Deployment"

if (-not $WhatIf) {
    try {
        # Check data source
        $dataSourceUrl = "$baseUrl/datasources/$DataSourceName?api-version=$ApiVersion"
        Invoke-SearchApi -Method "GET" -Endpoint $dataSourceUrl -Headers $headers | Out-Null
        Write-Success "Data source verified" | Out-Null

        # Check index
        $indexUrl = "$baseUrl/indexes/$IndexName?api-version=$ApiVersion"
        Invoke-SearchApi -Method "GET" -Endpoint $indexUrl -Headers $headers | Out-Null
        Write-Success "Index verified"

        # Check skillset
        $skillsetUrl = "$baseUrl/skillsets/$SkillsetName?api-version=$ApiVersion"
        Invoke-SearchApi -Method "GET" -Endpoint $skillsetUrl -Headers $headers | Out-Null
        Write-Success "Skillset verified" | Out-Null

        # Check indexer
        $indexerUrl = "$baseUrl/indexers/$IndexerName?api-version=$ApiVersion"
        Invoke-SearchApi -Method "GET" -Endpoint $indexerUrl -Headers $headers | Out-Null
        Write-Success "Indexer verified" | Out-Null

        # Check indexer status
        $indexerStatusUrl = "$baseUrl/indexers/$IndexerName/status?api-version=$ApiVersion"
        $indexerStatus = Invoke-SearchApi -Method "GET" -Endpoint $indexerStatusUrl -Headers $headers
        Write-Host "Indexer Status: $($indexerStatus.status)" -ForegroundColor Yellow
        if ($indexerStatus.lastResult) {
            Write-Host "Last execution: $($indexerStatus.lastResult.status) - $($indexerStatus.lastResult.startTime)" -ForegroundColor Gray
        }
    }
    catch {
        Write-Error "Failed to verify deployment"
        throw
    }
}

Write-Host "Deployment completed successfully!" -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Upload documents to the '$StorageContainerName' container in storage account '$StorageAccountName'" -ForegroundColor Gray
Write-Host "2. Monitor indexer execution in the Azure portal" -ForegroundColor Gray
Write-Host "3. Test search queries against the index" -ForegroundColor Gray

# ==============================================================================
# Usage Examples
# ==============================================================================

Write-Host ""
Write-Host "Usage Examples:" -ForegroundColor Yellow
Write-Host ""
Write-Host "# Deploy with Managed Identity (recommended):" -ForegroundColor Gray
Write-Host "./script.ps1 -ResourceGroupName 'your-rg' -SearchServiceName 'your-search' -StorageAccountName 'yourstorage' ``" -ForegroundColor Gray
Write-Host "    -OpenAIResourceUri 'https://your-openai.openai.azure.com' ``" -ForegroundColor Gray
Write-Host "    -CognitiveServicesResourceUri 'https://your-cognitive.cognitiveservices.azure.com' ``" -ForegroundColor Gray
Write-Host "    -ChatCompletionDeploymentId 'gpt-4o' -OpenAIDeploymentId 'text-embedding-3-large'" -ForegroundColor Gray
Write-Host ""
Write-Host "# Deploy with custom index name and dimensions:" -ForegroundColor Gray
Write-Host "./script.ps1 -ResourceGroupName 'your-rg' -SearchServiceName 'your-search' -StorageAccountName 'yourstorage' ``" -ForegroundColor Gray
Write-Host "    -IndexName 'my-custom-index' -OpenAIEmbeddingDimensions 1536" -ForegroundColor Gray
Write-Host ""
Write-Host "# Deploy with API Keys (when managed identity is not applicable):" -ForegroundColor Gray
Write-Host "./script.ps1 -ResourceGroupName 'your-rg' -SearchServiceName 'your-search' -StorageAccountName 'yourstorage' ``" -ForegroundColor Gray
Write-Host "    -OpenAIResourceUri 'https://your-openai.openai.azure.com' -OpenAIApiKey 'your-openai-key' ``" -ForegroundColor Gray
Write-Host "    -CognitiveServicesResourceUri 'https://your-cognitive.cognitiveservices.azure.com' -CognitiveServicesApiKey 'your-cognitive-key'" -ForegroundColor Gray
Write-Host ""
Write-Host "# Test deployment (WhatIf mode):" -ForegroundColor Gray
Write-Host "./script.ps1 -ResourceGroupName 'your-rg' -SearchServiceName 'your-search' -StorageAccountName 'yourstorage' -WhatIf" -ForegroundColor Gray