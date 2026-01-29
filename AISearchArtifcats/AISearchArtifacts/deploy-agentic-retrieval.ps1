# ==============================================================================
# BusinessBooster - Deploy Knowledge Sources and Knowledge Base
# ==============================================================================
# This script creates:
# - 3 Knowledge Sources (marketing, programs, pitchdeck)
# - 1 Knowledge Base combining all sources for agentic retrieval
# ==============================================================================

param(
    [Parameter(Mandatory = $false)]
    [string]$ResourceGroupName = "Rg-hack-26",
    
    [Parameter(Mandatory = $false)]
    [string]$SearchServiceName = "ais-hackathon",
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIResourceUri = "https://foundry-hackathon-gris.cognitiveservices.azure.com",
    
    [Parameter(Mandatory = $false)]
    [string]$ChatCompletionDeploymentId = "gpt-4.1",
    
    [Parameter(Mandatory = $false)]
    [string]$ChatCompletionModelName = "gpt-4.1",
    
    [Parameter(Mandatory = $false)]
    [string]$ApiVersion = "2025-11-01-preview",
    
    [Parameter(Mandatory = $false)]
    [switch]$WhatIf
)

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

function Invoke-SearchApi {
    param(
        [string]$Method,
        [string]$Endpoint,
        [string]$Body = $null,
        [hashtable]$Headers
    )
    
    try {
        $params = @{
            Uri = $Endpoint
            Headers = $Headers
            Method = $Method
            ContentType = "application/json"
        }
        if ($Body) {
            $params.Body = $Body
        }
        $response = Invoke-RestMethod @params
        return $response
    }
    catch {
        Write-Error "API call failed: $($_.Exception.Message)"
        if ($_.ErrorDetails) {
            Write-Host "Error details: $($_.ErrorDetails.Message)" -ForegroundColor Red
        }
        throw
    }
}

# ==============================================================================
# Main Script
# ==============================================================================

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "BusinessBooster - Agentic Retrieval Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Get Azure context
$azAccount = az account show | ConvertFrom-Json
$SubscriptionId = $azAccount.id
Write-Host "Subscription: $($azAccount.name)" -ForegroundColor Gray

# Get Search admin key
Write-Step "Retrieving Search Service admin key"
$token = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
$mgmtHeaders = @{
    'Authorization' = "Bearer $token"
    'Content-Type' = 'application/json'
}

$url = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Search/searchServices/$SearchServiceName/listAdminKeys?api-version=2022-09-01"
$response = Invoke-RestMethod -Uri $url -Headers $mgmtHeaders -Method POST
$primaryAdminKey = $response.primaryKey
Write-Success "Retrieved admin key"

# Prepare Search API headers
$searchHeaders = @{
    'api-key' = $primaryAdminKey
    'Content-Type' = 'application/json'
    'Accept' = 'application/json'
}

$baseUrl = "https://$SearchServiceName.search.windows.net"

# ==============================================================================
# Create Knowledge Sources
# ==============================================================================

Write-Step "Creating Knowledge Sources"

# Knowledge Source definitions with specific retrieval prompts
$knowledgeSources = @(
    @{
        name = "businessbooster-marketing-ks"
        description = "Source de connaissances pour les fichiers marketing. Contient les infographies, visuels produits, captures d'écran, logos et éléments de branding pour les campagnes marketing Azure."
        indexName = "businessbooster-marketing-index"
        semanticConfig = "businessbooster-marketing-index-semantic-configuration"
        sourceDataFields = @("title", "chunk")
    },
    @{
        name = "businessbooster-programs-ks"
        description = "Source de connaissances pour les programmes d'accompagnement client. Contient les différents programmes que les équipes account peuvent activer pour aider les clients: éligibilité, bénéfices, processus d'activation, ROI et success stories."
        indexName = "businessbooster-programs-index"
        semanticConfig = "businessbooster-programs-index-semantic-configuration"
        sourceDataFields = @("title", "chunk")
    },
    @{
        name = "businessbooster-pitchdeck-ks"
        description = "Source de connaissances pour les pitch decks commerciaux. Contient les présentations de vente Azure pour les équipes sales et account: architectures, propositions de valeur, cas d'usage, comparatifs concurrentiels et roadmaps."
        indexName = "businessbooster-pitchdeck-index"
        semanticConfig = "businessbooster-pitchdeck-index-semantic-configuration"
        sourceDataFields = @("title", "chunk")
    }
)

foreach ($ks in $knowledgeSources) {
    Write-Host "  Creating knowledge source: $($ks.name)" -ForegroundColor Yellow
    
    $ksBody = @{
        name = $ks.name
        kind = "searchIndex"
        description = $ks.description
        encryptionKey = $null
        searchIndexParameters = @{
            searchIndexName = $ks.indexName
            semanticConfigurationName = $ks.semanticConfig
            sourceDataFields = $ks.sourceDataFields | ForEach-Object { @{ name = $_ } }
            searchFields = @( @{ name = "*" } )
        }
    } | ConvertTo-Json -Depth 10
    
    if (-not $WhatIf) {
        $ksUrl = "$baseUrl/knowledgesources/$($ks.name)?api-version=$ApiVersion"
        try {
            Invoke-SearchApi -Method "PUT" -Endpoint $ksUrl -Body $ksBody -Headers $searchHeaders | Out-Null
            Write-Success "Knowledge source '$($ks.name)' created"
        }
        catch {
            Write-Error "Failed to create knowledge source '$($ks.name)'"
        }
    } else {
        Write-Host "Would create knowledge source: $($ks.name)" -ForegroundColor Gray
    }
}

# ==============================================================================
# Create Knowledge Base
# ==============================================================================

Write-Step "Creating Knowledge Base"

$knowledgeBaseName = "businessbooster-kb"

# Retrieval instructions for the LLM to select appropriate knowledge sources
$retrievalInstructions = @"
Vous êtes un assistant pour les équipes commerciales et account Microsoft. Utilisez les sources de connaissances suivantes selon le contexte de la question:

1. **businessbooster-marketing-ks**: Utilisez cette source pour les questions sur:
   - Les visuels et assets marketing
   - Les infographies et données de marché
   - Les éléments de branding et positionnement
   - Les campagnes et supports de communication

2. **businessbooster-programs-ks**: Utilisez cette source pour les questions sur:
   - Les programmes d'accompagnement client (ECIF, FastTrack, etc.)
   - Les conditions d'éligibilité et bénéfices des programmes
   - Les processus d'activation et les contacts
   - Les success stories et ROI des programmes

3. **businessbooster-pitchdeck-ks**: Utilisez cette source pour les questions sur:
   - Les présentations commerciales et pitch decks
   - Les architectures de référence Azure
   - Les propositions de valeur et différenciateurs
   - Les cas d'usage par industrie
   - Les comparatifs avec la concurrence

Si la question couvre plusieurs domaines, interrogez plusieurs sources pour fournir une réponse complète.
"@

# Answer instructions for synthesized responses
$answerInstructions = @"
Vous êtes BusinessBooster, un assistant intelligent pour les équipes commerciales Microsoft France.

Instructions pour vos réponses:
1. Répondez toujours en français
2. Soyez concis mais complet
3. Citez les sources avec [ref_id:X] quand vous utilisez des informations spécifiques
4. Structurez vos réponses avec des titres et listes quand approprié
5. Si vous ne trouvez pas l'information, dites-le clairement et suggérez des alternatives
6. Pour les questions techniques, incluez les détails d'architecture si disponibles
7. Pour les programmes, mentionnez les conditions d'éligibilité et les contacts si connus
"@

$kbBody = @{
    name = $knowledgeBaseName
    description = "Base de connaissances BusinessBooster pour les équipes commerciales Microsoft France. Combine marketing, programmes et pitch decks pour l'aide à la vente Azure."
    retrievalInstructions = $retrievalInstructions
    answerInstructions = $answerInstructions
    outputMode = "answerSynthesis"
    knowledgeSources = @(
        @{ name = "businessbooster-marketing-ks" },
        @{ name = "businessbooster-programs-ks" },
        @{ name = "businessbooster-pitchdeck-ks" }
    )
    models = @(
        @{
            kind = "azureOpenAI"
            azureOpenAIParameters = @{
                resourceUri = $OpenAIResourceUri
                deploymentId = $ChatCompletionDeploymentId
                modelName = $ChatCompletionModelName
            }
        }
    )
    encryptionKey = $null
    retrievalReasoningEffort = @{
        kind = "medium"
    }
} | ConvertTo-Json -Depth 10

if (-not $WhatIf) {
    $kbUrl = "$baseUrl/knowledgebases/$knowledgeBaseName`?api-version=$ApiVersion"
    try {
        Invoke-SearchApi -Method "PUT" -Endpoint $kbUrl -Body $kbBody -Headers $searchHeaders | Out-Null
        Write-Success "Knowledge base '$knowledgeBaseName' created"
    }
    catch {
        Write-Error "Failed to create knowledge base"
        throw
    }
} else {
    Write-Host "Would create knowledge base: $knowledgeBaseName" -ForegroundColor Gray
    Write-Host $kbBody -ForegroundColor Gray
}

# ==============================================================================
# Verify Deployment
# ==============================================================================

Write-Step "Verifying Deployment"

if (-not $WhatIf) {
    # List knowledge sources
    Write-Host "Knowledge Sources:" -ForegroundColor Yellow
    $ksListUrl = "$baseUrl/knowledgesources?api-version=$ApiVersion&`$select=name,kind"
    $ksList = Invoke-SearchApi -Method "GET" -Endpoint $ksListUrl -Headers $searchHeaders
    foreach ($ks in $ksList.value) {
        Write-Host "  - $($ks.name) ($($ks.kind))" -ForegroundColor Gray
    }
    
    # Get knowledge base
    Write-Host "Knowledge Base:" -ForegroundColor Yellow
    $kbGetUrl = "$baseUrl/knowledgebases/$knowledgeBaseName`?api-version=$ApiVersion"
    $kb = Invoke-SearchApi -Method "GET" -Endpoint $kbGetUrl -Headers $searchHeaders
    Write-Host "  - $($kb.name)" -ForegroundColor Gray
    Write-Host "    Sources: $($kb.knowledgeSources.name -join ', ')" -ForegroundColor Gray
    Write-Host "    Output Mode: $($kb.outputMode)" -ForegroundColor Gray
    Write-Host "    Reasoning Effort: $($kb.retrievalReasoningEffort.kind)" -ForegroundColor Gray
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Deployment Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Knowledge Base: $knowledgeBaseName" -ForegroundColor Yellow
Write-Host ""
Write-Host "Test with:" -ForegroundColor Yellow
Write-Host @"
POST https://$SearchServiceName.search.windows.net/knowledgebases/$knowledgeBaseName/retrieve?api-version=$ApiVersion
Content-Type: application/json
api-key: <your-admin-key>

{
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "text": "Quels programmes puis-je proposer à un client qui veut migrer vers Azure?",
                    "type": "text"
                }
            ]
        }
    ],
    "includeActivity": true
}
"@ -ForegroundColor Gray
