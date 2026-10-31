## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- If the graphify MCP server is active, utilize tools like `query_graph`, `get_node`, and `shortest_path` for precise architecture navigation instead of falling back to `grep`
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
# Graphify Context Rule
Before answering any complex architectural or codebase cross-reference questions regarding `reversebot`, you must use the `graphify` MCP server tools to query the context graph located at `/Users/rahulkumar/Documents/reversebot/graphify-out/graph.json`. 

Always prioritize structural dependencies returned by the graph tool over raw file searches.
# Rule: Mandatory Knowledge Graph Lookup

- **Context:** This project uses an MCP-powered architecture graph located at `/Users/rahulkumar/Documents/reversebot/graphify-out/graph.json`.
- **Instruction:** Before responding to any CLI queries regarding codebase structure, directory architecture, or cross-file dependencies inside `reversebot`, you MUST automatically execute the `graphify` MCP server tools.
- **Fallbacks:** Do not attempt to guess structural dependencies via standard file searches if the data can be requested from the `graphify` schema.
