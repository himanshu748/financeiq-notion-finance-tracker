import path from "node:path";
import process from "node:process";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

let _clientPromise = null;

function notionMcpCommand() {
  // Prefer local dependency binary for reproducibility.
  const bin = path.join(process.cwd(), "node_modules", ".bin", "notion-mcp-server");
  return { command: bin, args: [] };
}

export async function getNotionMcpClient() {
  if (_clientPromise) return _clientPromise;

  _clientPromise = (async () => {
    const { command, args } = notionMcpCommand();

    const transport = new StdioClientTransport({
      command,
      args,
      cwd: process.cwd(),
      env: {
        // The Notion MCP server uses NOTION_TOKEN to talk to Notion API.
        NOTION_TOKEN: process.env.NOTION_TOKEN ?? "",
        // Keep output clean (some MCP servers support this).
        LOG_LEVEL: process.env.LOG_LEVEL ?? "error"
      },
      stderr: "pipe"
    });

    const client = new Client(
      { name: "design-docs-ui", version: "1.0.0" },
      { capabilities: {} }
    );

    await client.connect(transport);
    await client.listTools();

    return client;
  })();

  return _clientPromise;
}

