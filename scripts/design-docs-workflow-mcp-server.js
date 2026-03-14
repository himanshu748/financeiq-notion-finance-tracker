import process from "node:process";
import { z } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import fs from "node:fs/promises";
import path from "node:path";
import { draftDesignDoc, updateDesignDoc } from "./design-docs-mcp.js";

const McpConfigSchema = z.object({
  notion: z.object({
    hubPageUrl: z.string().url(),
    designDocs: z.object({
      databaseUrl: z.string().url(),
      dataSourceId: z.string()
    }),
    sourceNotes: z.object({
      databaseUrl: z.string().url(),
      dataSourceId: z.string()
    })
  })
});

async function readMcpConfig() {
  const p = path.join(process.cwd(), "design-docs.mcp.config.json");
  const raw = await fs.readFile(p, "utf8");
  return McpConfigSchema.parse(JSON.parse(raw));
}

function toTextResult(text, structuredContent) {
  return {
    content: [{ type: "text", text }],
    structuredContent
  };
}

const DraftInput = z.object({
  feature: z.string().min(1),
  repos: z.string().optional().default("")
});

const DraftOutput = z.object({
  ok: z.boolean(),
  url: z.string().nullable().optional(),
  debug: z.any().optional(),
  error: z.string().optional()
});

const server = new McpServer({
  name: "design-docs-workflow",
  version: "1.0.0"
});

server.registerTool(
  "design_docs_status",
  {
    title: "Design Docs status",
    description:
      "Returns Notion URLs/IDs for the Design Docs system (used for demos). This MCP server calls the Notion MCP server under the hood for Draft/Update.",
    inputSchema: z.object({}),
    outputSchema: z.object({
      ok: z.literal(true),
      notion: z.object({
        hubPageUrl: z.string(),
        designDocsDatabaseUrl: z.string(),
        sourceNotesDatabaseUrl: z.string()
      })
    })
  },
  async () => {
    const cfg = await readMcpConfig();
    return toTextResult("OK", {
      ok: true,
      notion: {
        hubPageUrl: cfg.notion.hubPageUrl,
        designDocsDatabaseUrl: cfg.notion.designDocs.databaseUrl,
        sourceNotesDatabaseUrl: cfg.notion.sourceNotes.databaseUrl
      }
    });
  }
);

server.registerTool(
  "design_docs_draft",
  {
    title: "Draft design doc",
    description:
      "Draft a design doc for a feature using Notion MCP + GitHub search. Creates a new row/page in the Design Docs database and adds backlinks + a comment for unclear requirements.",
    inputSchema: DraftInput,
    outputSchema: DraftOutput
  },
  async ({ feature, repos }) => {
    const out = await draftDesignDoc({ feature, reposCsv: repos });
    const url = out.url ?? null;
    return toTextResult(url ? `Created: ${url}` : "Created", { ...out, url });
  }
);

server.registerTool(
  "design_docs_update",
  {
    title: "Update design doc",
    description:
      "Update an existing design doc for a feature using Notion MCP + GitHub search. Updates the existing doc and appends a Sync Log entry (no duplicate doc).",
    inputSchema: DraftInput,
    outputSchema: DraftOutput
  },
  async ({ feature, repos }) => {
    const out = await updateDesignDoc({ feature, reposCsv: repos });
    const url = out.url ?? null;
    return toTextResult(url ? `Updated: ${url}` : "Updated", { ...out, url });
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);

// Keep process alive for stdio transport.
process.stdin.resume();

