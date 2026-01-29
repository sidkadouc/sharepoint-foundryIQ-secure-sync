# Reset AI Search Index and Rerun Indexer
# This script clears the index and reruns the indexer from scratch

param(
    [string]$ResourceGroup = "rg-jurisimple-dev",
    [string]$SearchServiceName = "srch-jurisimple-dev-001",
    [string]$IndexName = "vector-jt-poc",
    [string]$IndexerName = "vector-jt-poc-indexer",
    [switch]$SkipConfirmation
)

$ErrorActionPreference = "Stop"

Write-Host "=== AI Search Index Reset and Reindex Script ===" -ForegroundColor Cyan
Write-Host ""

# Get Search Service admin key
Write-Host "Getting Search Service admin key..." -ForegroundColor Yellow
$adminKey = az search admin-key show `
    --resource-group $ResourceGroup `
    --service-name $SearchServiceName `
    --query "primaryKey" -o tsv

if (-not $adminKey) {
    Write-Error "Failed to get admin key. Make sure you're logged into Azure CLI."
    exit 1
}

$headers = @{
    "api-key" = $adminKey
    "Content-Type" = "application/json"
}

$searchEndpoint = "https://$SearchServiceName.search.windows.net"

# Step 1: Get current document count
Write-Host ""
Write-Host "Step 1: Checking current index status..." -ForegroundColor Yellow
$indexStatsUrl = "$searchEndpoint/indexes/$IndexName/stats?api-version=2024-07-01"
try {
    $stats = Invoke-RestMethod -Uri $indexStatsUrl -Headers $headers -Method Get
    Write-Host "  Current document count: $($stats.documentCount)" -ForegroundColor White
    Write-Host "  Storage size: $([math]::Round($stats.storageSize / 1MB, 2)) MB" -ForegroundColor White
} catch {
    Write-Host "  Could not get index stats: $($_.Exception.Message)" -ForegroundColor Red
}

# Confirmation
if (-not $SkipConfirmation) {
    Write-Host ""
    Write-Host "This will DELETE all documents from the index '$IndexName' and reindex from scratch." -ForegroundColor Red
    $confirm = Read-Host "Are you sure you want to continue? (y/N)"
    if ($confirm -ne "y" -and $confirm -ne "Y") {
        Write-Host "Operation cancelled." -ForegroundColor Yellow
        exit 0
    }
}

# Step 2: Delete all documents from the index
Write-Host ""
Write-Host "Step 2: Deleting all documents from index..." -ForegroundColor Yellow

# First, get all document IDs
$searchUrl = "$searchEndpoint/indexes/$IndexName/docs/search?api-version=2024-07-01"
$allDocIds = @()
$skip = 0
$batchSize = 1000

do {
    $searchBody = @{
        search = "*"
        select = "chunk_id"
        top = $batchSize
        skip = $skip
    } | ConvertTo-Json

    try {
        $results = Invoke-RestMethod -Uri $searchUrl -Headers $headers -Method Post -Body $searchBody
        $docIds = $results.value | ForEach-Object { $_.chunk_id }
        $allDocIds += $docIds
        $skip += $batchSize
        Write-Host "  Found $($allDocIds.Count) documents so far..." -ForegroundColor Gray
    } catch {
        Write-Host "  Error searching documents: $($_.Exception.Message)" -ForegroundColor Red
        break
    }
} while ($results.value.Count -eq $batchSize)

Write-Host "  Total documents to delete: $($allDocIds.Count)" -ForegroundColor White

if ($allDocIds.Count -gt 0) {
    # Delete in batches of 1000
    $deleteUrl = "$searchEndpoint/indexes/$IndexName/docs/index?api-version=2024-07-01"
    $deletedCount = 0
    
    for ($i = 0; $i -lt $allDocIds.Count; $i += 1000) {
        $batch = $allDocIds[$i..([Math]::Min($i + 999, $allDocIds.Count - 1))]
        $deleteActions = $batch | ForEach-Object {
            @{
                "@search.action" = "delete"
                "chunk_id" = $_
            }
        }
        
        $deleteBody = @{
            value = $deleteActions
        } | ConvertTo-Json -Depth 10
        
        try {
            $deleteResult = Invoke-RestMethod -Uri $deleteUrl -Headers $headers -Method Post -Body $deleteBody
            $deletedCount += $batch.Count
            Write-Host "  Deleted $deletedCount / $($allDocIds.Count) documents..." -ForegroundColor Gray
        } catch {
            Write-Host "  Error deleting batch: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    
    Write-Host "  Deleted $deletedCount documents from index." -ForegroundColor Green
} else {
    Write-Host "  No documents to delete." -ForegroundColor Gray
}

# Step 3: Reset the indexer
Write-Host ""
Write-Host "Step 3: Resetting indexer state..." -ForegroundColor Yellow
$resetUrl = "$searchEndpoint/indexers/$IndexerName/reset?api-version=2024-07-01"
try {
    Invoke-RestMethod -Uri $resetUrl -Headers $headers -Method Post
    Write-Host "  Indexer reset successfully." -ForegroundColor Green
} catch {
    Write-Host "  Error resetting indexer: $($_.Exception.Message)" -ForegroundColor Red
}

# Step 4: Run the indexer
Write-Host ""
Write-Host "Step 4: Running indexer..." -ForegroundColor Yellow
$runUrl = "$searchEndpoint/indexers/$IndexerName/run?api-version=2024-07-01"
try {
    Invoke-RestMethod -Uri $runUrl -Headers $headers -Method Post
    Write-Host "  Indexer started successfully." -ForegroundColor Green
} catch {
    Write-Host "  Error running indexer: $($_.Exception.Message)" -ForegroundColor Red
}

# Step 5: Monitor indexer status
Write-Host ""
Write-Host "Step 5: Monitoring indexer status..." -ForegroundColor Yellow
$statusUrl = "$searchEndpoint/indexers/$IndexerName/status?api-version=2024-07-01"

$maxWaitSeconds = 300
$elapsed = 0
$checkInterval = 10

while ($elapsed -lt $maxWaitSeconds) {
    Start-Sleep -Seconds $checkInterval
    $elapsed += $checkInterval
    
    try {
        $status = Invoke-RestMethod -Uri $statusUrl -Headers $headers -Method Get
        $lastResult = $status.lastResult
        
        if ($lastResult) {
            $runStatus = $lastResult.status
            $itemsProcessed = $lastResult.itemsProcessed
            $itemsFailed = $lastResult.itemsFailed
            
            Write-Host "  [$elapsed s] Status: $runStatus | Processed: $itemsProcessed | Failed: $itemsFailed" -ForegroundColor White
            
            if ($runStatus -eq "success" -or $runStatus -eq "transientFailure") {
                break
            }
        } else {
            Write-Host "  [$elapsed s] Indexer still initializing..." -ForegroundColor Gray
        }
    } catch {
        Write-Host "  Error checking status: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# Step 6: Final verification
Write-Host ""
Write-Host "Step 6: Verifying new index stats..." -ForegroundColor Yellow
Start-Sleep -Seconds 5  # Give time for stats to update

try {
    $finalStats = Invoke-RestMethod -Uri $indexStatsUrl -Headers $headers -Method Get
    Write-Host "  New document count: $($finalStats.documentCount)" -ForegroundColor Green
    Write-Host "  Storage size: $([math]::Round($finalStats.storageSize / 1MB, 2)) MB" -ForegroundColor Green
} catch {
    Write-Host "  Could not get final stats." -ForegroundColor Red
}

# Step 7: Sample a document to verify field mapping
Write-Host ""
Write-Host "Step 7: Verifying field mappings (sample document)..." -ForegroundColor Yellow
$sampleSearchBody = @{
    search = "*"
    select = "chunk_id,title,original_file_name,parent_id"
    top = 3
} | ConvertTo-Json

try {
    $sampleResults = Invoke-RestMethod -Uri $searchUrl -Headers $headers -Method Post -Body $sampleSearchBody
    
    if ($sampleResults.value.Count -gt 0) {
        Write-Host "  Sample documents:" -ForegroundColor White
        foreach ($doc in $sampleResults.value) {
            Write-Host "    - chunk_id: $($doc.chunk_id)" -ForegroundColor Gray
            Write-Host "      title: $($doc.title)" -ForegroundColor Gray
            Write-Host "      original_file_name: $($doc.original_file_name)" -ForegroundColor $(if ($doc.original_file_name) { "Green" } else { "Red" })
            Write-Host ""
        }
    } else {
        Write-Host "  No documents found in index yet." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Error sampling documents: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
