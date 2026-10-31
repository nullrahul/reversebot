## graphify

This project has a knowledge graph exposed via the `graphify` MCP server.

Rules:
- For codebase questions, navigation, or tracing, you MUST first call the `graphify` MCP tools (like `query_graph`, `get_node`, `get_neighbors`, `get_community`, `shortest_path`) rather than performing broad full-text greps or viewing whole source files.
- Use `query_graph` with a natural language query/concept to obtain a token-efficient subgraph context.
- Use `shortest_path` or `get_neighbors` to understand relationships between files, classes, and methods.
- After modifying code, run the terminal command `graphify update .` to keep the AST database current.
