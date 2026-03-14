import process from "node:process";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

let _clientPromise = null;

export async function getWorkflowClient() {
  if (_clientPromise) return _clientPromise;

  _clientPromise = (async () => {
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [path.join(process.cwd(), "scripts", "design-docs-workflow-mcp-server.js")],
      cwd: process.cwd(),
      env: {
        NOTION_TOKEN: process.env.NOTION_TOKEN ?? "",
        GITHUB_TOKEN: process.env.GITHUB_TOKEN ?? ""
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

