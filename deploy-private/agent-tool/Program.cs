using Azure.AI.Projects;
using Azure.AI.Projects.OpenAI;
using Azure.Identity;
using System;
using System.Linq;
using System.Threading.Tasks;

// ---------------------------------------------------------------------------
// Foundry Agent v2 creation tool.
//
// Uses Azure.AI.Projects SDK 2.0 (v2 API) with PromptAgentDefinition.
//
// Modes:
//   Create: dotnet run -- --endpoint <url> --model gpt-4o
//                         --search-connection conn-search --index-name idx
//   Test:   dotnet run -- --endpoint <url> --test "query"
// ---------------------------------------------------------------------------

static string GetArg(string[] a, string name, string def = "")
{
    for (int i = 0; i < a.Length - 1; i++)
        if (a[i] == name) return a[i + 1];
    return def;
}

var endpoint       = GetArg(args, "--endpoint");
var model          = GetArg(args, "--model", "gpt-4o");
var searchConn     = GetArg(args, "--search-connection");
var indexName      = GetArg(args, "--index-name", "sharepoint-index");
var embeddingModel = GetArg(args, "--embedding-model", "text-embedding-3-large");
var agentName      = GetArg(args, "--agent-name", "sharepoint-knowledge-agent");
var testQuery      = GetArg(args, "--test");

if (string.IsNullOrEmpty(endpoint))
{
    Console.Error.WriteLine("Usage: dotnet run -- --endpoint <project-endpoint> [options]");
    Console.Error.WriteLine();
    Console.Error.WriteLine("  --endpoint <url>            Foundry project endpoint (required)");
    Console.Error.WriteLine("  --model <name>              Model deployment (default: gpt-4o)");
    Console.Error.WriteLine("  --search-connection <name>  AI Search connection name");
    Console.Error.WriteLine("  --index-name <name>         AI Search index (default: sharepoint-index)");
    Console.Error.WriteLine("  --agent-name <name>         Agent name (default: sharepoint-knowledge-agent)");
    Console.Error.WriteLine("  --test <query>              Query an existing agent");
    return 1;
}

var credential = new DefaultAzureCredential();
var client = new AIProjectClient(new Uri(endpoint), credential);

// -- Test mode: query an existing agent -----------------------------------
if (!string.IsNullOrEmpty(testQuery))
{
    Console.WriteLine($"[TEST] Querying agent '{agentName}' with: {testQuery}");
    try
    {
        var agentRef = new AgentReference(agentName, "latest");
        var responsesClient = client.OpenAI.GetProjectResponsesClientForAgent(agentRef);
        var response = await responsesClient.CreateResponseAsync(testQuery);
        Console.WriteLine($"[RESPONSE] {response.Value.GetOutputText()}");
        return 0;
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"[ERROR] Test failed: {ex.Message}");
        return 1;
    }
}

// -- Create mode ----------------------------------------------------------
Console.WriteLine($"[INFO] Creating agent '{agentName}' on {endpoint}");
Console.WriteLine($"[INFO] Model: {model}");

var agentDef = new PromptAgentDefinition(model)
{
    Instructions =
        "You are a helpful assistant that answers questions using the SharePoint " +
        "knowledge base. Always cite the source document name when referencing " +
        "information. If the user asks about documents or files, search the " +
        "knowledge base. Keep answers concise and accurate."
};

// Add Azure AI Search tool if connection is provided
if (!string.IsNullOrEmpty(searchConn))
{
    Console.WriteLine($"[INFO] Resolving search connection: {searchConn}");

    string connectionId = searchConn;
    try
    {
        var conn = client.Connections.GetConnection(searchConn);
        connectionId = conn.Id;
        Console.WriteLine($"[INFO] Connection resolved → ID: {connectionId}");
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[WARN] Could not resolve connection ({ex.Message}), using name as-is");
    }

    var searchIndex = new AzureAISearchToolIndex()
    {
        ProjectConnectionId = connectionId,
        IndexName = indexName,
        TopK = 5,
        QueryType = AzureAISearchQueryType.VectorSemanticHybrid
    };

    var searchTool = new AzureAISearchTool(new AzureAISearchToolOptions(indexes: [searchIndex]));
    agentDef.Tools.Add(searchTool);
    Console.WriteLine($"[INFO] AI Search tool added (index: {indexName}, hybrid search)");
}

// Create agent version
try
{
    var agentVersion = await client.Agents
        .CreateAgentVersionAsync(agentName, new AgentVersionCreationOptions(agentDef));

    Console.WriteLine();
    Console.WriteLine($"[OK] Agent created successfully!");
    Console.WriteLine($"  Name:    {agentVersion.Value.Name}");
    Console.WriteLine($"  Version: {agentVersion.Value.Version}");
    Console.WriteLine($"  ID:      {agentVersion.Value.Id}");
    Console.WriteLine();
    Console.WriteLine($"Test with:");
    Console.WriteLine($"  dotnet run -- --endpoint \"{endpoint}\" --test \"What documents are available?\"");
    return 0;
}
catch (Exception ex)
{
    Console.Error.WriteLine($"[ERROR] Agent creation failed: {ex.Message}");
    Console.Error.WriteLine(ex.ToString());
    return 1;
}
