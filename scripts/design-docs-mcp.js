import { getNotionMcpClient } from "./notion-mcp-client.js";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const MCP_CONFIG_PATH = path.join(process.cwd(), "design-docs.mcp.config.json");

async function readMcpConfig() {
  const raw = await fs.readFile(MCP_CONFIG_PATH, "utf8");
  return JSON.parse(raw);
}

function nowIsoDate() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function escapeMd(s) {
  // Minimal escaping for Notion-flavored markdown outside code blocks.
  return s.replace(/\\/g, "\\\\").replace(/\*/g, "\\*").replace(/\{/g, "\\{").replace(/\}/g, "\\}");
}

function mentionPage(url) {
  return `- <mention-page url="${url}"/>`;
}

async function ghSearchIssues({ query, reposCsv, limit = 15 }) {
  const base = `"${query}" in:title,body`;
  const scoped = reposCsv?.trim()
    ? reposCsv
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean)
        .map((r) => `${base} repo:${r}`)
        .join(" OR ")
    : base;

  const { stdout } = await execFileAsync(
    "gh",
    [
      "search",
      "issues",
      scoped,
      "--limit",
      String(limit),
      "--json",
      "title,url,number,repository,isPullRequest"
    ],
    { cwd: process.cwd() }
  );
  return JSON.parse(stdout);
}

async function callTool(client, name, args) {
  const res = await client.callTool({ name, arguments: args });
  // Notion MCP tools usually return { content: [{type:'text', text:'...'}] }
  return res;
}

function extractTextContent(toolResult) {
  const blocks = toolResult?.content ?? [];
  const texts = blocks
    .filter((b) => b && typeof b === "object" && b.type === "text")
    .map((b) => b.text ?? "")
    .filter(Boolean);
  return texts.join("\n");
}

function parseSearchResults(toolResult) {
  // Some servers return structuredContent; others only text.
  if (toolResult?.structuredContent?.results) return toolResult.structuredContent.results;
  if (toolResult?.structuredContent?.data?.results) return toolResult.structuredContent.data.results;

  const text = extractTextContent(toolResult);
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed?.results)) return parsed.results;
  } catch {
    // ignore
  }
  return [];
}

function buildDraftMarkdown({ feature, hubUrl, pages, sourceNotes, ghItems }) {
  const githubLinks = ghItems.map((i) => i.url);

  return [
    `## Problem`,
    `TODO(clarify): Summarize the user/customer problem for **${escapeMd(feature)}**.`,
    ``,
    `## Goals`,
    `- TODO(clarify): Goal 1`,
    `- TODO(clarify): Goal 2`,
    ``,
    `## Non‑Goals`,
    `- TODO(clarify): Non-goal 1`,
    ``,
    `## API Design`,
    `TODO(clarify): Proposed API endpoints/events/data model.`,
    ``,
    `## Risks`,
    `- TODO(clarify): Risk 1`,
    ``,
    `## Open Questions`,
    `- TODO(clarify): What’s unclear or conflicting in current notes?`,
    ``,
    `## GitHub Links`,
    ...(githubLinks.length ? githubLinks.map((u) => `- ${u}`) : [`- (none found)`]),
    ``,
    `## References`,
    `**Hub:**`,
    mentionPage(hubUrl),
    ``,
    `**Notion pages:**`,
    ...(pages.length ? pages.map((p) => mentionPage(p.url)) : [`- (none found)`]),
    ``,
    `**Source Notes:**`,
    ...(sourceNotes.length ? sourceNotes.map((p) => mentionPage(p.url)) : [`- (none found)`]),
    ``,
    `::: callout {icon="💬" color="yellow_bg"}`,
    `COMMENT: Requirements are incomplete. Please review the sources above and replace TODO(clarify) with confirmed decisions.`,
    `:::`,
    ``,
    `## Sync Log`,
    `- ${nowIsoDate()}: draft created via Notion MCP + GitHub search`
  ].join("\n");
}

function selectBestTitleMatch(results, title) {
  const t = title.trim().toLowerCase();
  const exact = results.find((r) => String(r.title ?? "").trim().toLowerCase() === t);
  return exact ?? results[0] ?? null;
}

export async function draftDesignDoc({ feature, reposCsv }) {
  const cfg = await readMcpConfig();
  const client = await getNotionMcpClient();

  // Notion search: workspace + Source Notes data source
  const [wsRes, snRes] = await Promise.all([
    callTool(client, "notion-search", {
      query: feature,
      query_type: "internal",
      content_search_mode: "workspace_search"
    }),
    callTool(client, "notion-search", {
      query: feature,
      query_type: "internal",
      content_search_mode: "workspace_search",
      data_source_url: `collection://${cfg.notion.sourceNotes.dataSourceId}`
    })
  ]);

  const wsResults = parseSearchResults(wsRes)
    .filter((r) => r.type === "page")
    .slice(0, 6);
  const snResults = parseSearchResults(snRes)
    .filter((r) => r.type === "page")
    .slice(0, 10);

  const ghItems = await ghSearchIssues({ query: feature, reposCsv, limit: 15 });

  const md = buildDraftMarkdown({
    feature,
    hubUrl: cfg.notion.hubPageUrl,
    pages: wsResults.map((r) => ({ url: r.url, title: r.title })),
    sourceNotes: snResults.map((r) => ({ url: r.url, title: r.title })),
    ghItems
  });

  const ghLinks = ghItems.map((i) => i.url).join("\n");

  const created = await callTool(client, "notion-create-pages", {
    parent: { type: "data_source_id", data_source_id: cfg.notion.designDocs.dataSourceId },
    pages: [
      {
        properties: {
          Title: feature,
          Status: "Draft",
          Project: "Unassigned",
          "Review Status": "Not Requested",
          "GitHub Links": ghLinks
        },
        content: md
      }
    ]
  });

  const createdText = extractTextContent(created);
  const createdUrl =
    created?.structuredContent?.pages?.[0]?.url ??
    created?.structuredContent?.pages?.[0]?.id ??
    createdText.match(/https:\/\/www\.notion\.so\/\S+/)?.[0] ??
    null;

  // Add a page-level comment for ambiguity (required by spec)
  const createdId = created?.structuredContent?.pages?.[0]?.id ?? created?.pages?.[0]?.id ?? null;
  if (createdId) {
    await callTool(client, "notion-create-comment", {
      page_id: createdId,
      rich_text: [
        {
          type: "text",
          text: {
            content:
              "COMMENT: Requirements unclear. Please confirm Problem/Goals/Non‑Goals/API Design/Risks/Open Questions. If GitHub links look noisy, scope search to your repos."
          }
        }
      ]
    });
  }

  return {
    ok: true,
    url: createdUrl,
    debug: {
      notionSources: {
        workspacePages: wsResults.map((r) => r.url),
        sourceNotes: snResults.map((r) => r.url)
      },
      githubLinks: ghItems.map((i) => i.url)
    }
  };
}

export async function updateDesignDoc({ feature, reposCsv }) {
  const cfg = await readMcpConfig();
  const client = await getNotionMcpClient();

  const findRes = await callTool(client, "notion-search", {
    query: feature,
    query_type: "internal",
    content_search_mode: "workspace_search",
    data_source_url: `collection://${cfg.notion.designDocs.dataSourceId}`
  });
  const candidates = parseSearchResults(findRes).filter((r) => r.type === "page");
  const page = selectBestTitleMatch(candidates, feature);
  if (!page) {
    return { ok: false, error: "No existing design doc found in Design Docs DB for that feature." };
  }

  const [wsRes, snRes, ghItems, fetched] = await Promise.all([
    callTool(client, "notion-search", {
      query: feature,
      query_type: "internal",
      content_search_mode: "workspace_search"
    }),
    callTool(client, "notion-search", {
      query: feature,
      query_type: "internal",
      content_search_mode: "workspace_search",
      data_source_url: `collection://${cfg.notion.sourceNotes.dataSourceId}`
    }),
    ghSearchIssues({ query: feature, reposCsv, limit: 15 }),
    callTool(client, "notion-fetch", { id: page.id ?? page.url })
  ]);

  const wsResults = parseSearchResults(wsRes).filter((r) => r.type === "page").slice(0, 6);
  const snResults = parseSearchResults(snRes).filter((r) => r.type === "page").slice(0, 10);

  const fetchedText = extractTextContent(fetched);
  const contentMatch = fetchedText.match(/<content>\n([\s\S]*?)\n<\/content>/);
  const currentContent = contentMatch?.[1] ?? "";
  const syncLine = `- ${nowIsoDate()}: update run — refreshed sources + GitHub links`;

  const nextContent = currentContent.includes("## Sync Log")
    ? `${currentContent}\n${syncLine}\n`
    : `${currentContent}\n\n## Sync Log\n${syncLine}\n`;

  const ghLinks = ghItems.map((i) => i.url).join("\n");

  await Promise.all([
    callTool(client, "notion-update-page", {
      page_id: page.id,
      command: "replace_content",
      new_str: nextContent
    }),
    callTool(client, "notion-update-page", {
      page_id: page.id,
      command: "update_properties",
      properties: {
        "GitHub Links": ghLinks
      }
    })
  ]);

  // Add comment if requirements still unclear (always safe).
  await callTool(client, "notion-create-comment", {
    page_id: page.id,
    rich_text: [
      {
        type: "text",
        text: {
          content:
            "COMMENT: Update complete. Review whether new sources change Problem/Goals/API Design/Risks/Open Questions."
        }
      }
    ]
  });

  return {
    ok: true,
    url: page.url,
    debug: {
      notionSources: {
        workspacePages: wsResults.map((r) => r.url),
        sourceNotes: snResults.map((r) => r.url)
      },
      githubLinks: ghItems.map((i) => i.url)
    }
  };
}

