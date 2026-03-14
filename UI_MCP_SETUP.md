## UI + Notion MCP setup (for judge demo)

The UI’s **Run (UI + MCP)** tab runs your workflow by starting the official Notion MCP server locally (`notion-mcp-server`) and calling its tools.

### 1) Create a Notion integration token

1. Create a Notion integration (internal) and copy its token.
2. Share these Notion databases with the integration (so it can read/write):
   - `Design Docs`: `https://www.notion.so/f2b623eeb5e04de6a1da719812381c0a`
   - `Source Notes`: `https://www.notion.so/e8af6201df094e6cb791b457e2a55769`

### 2) Add secrets locally

```bash
cp .env.example .env
```

Set:
- `NOTION_TOKEN=...`
- optional `GITHUB_TOKEN=...` (or rely on `gh auth login`)

### 3) Run the UI

```bash
npm run ui
```

Open `http://localhost:5179` → choose **Run (UI + MCP)**.

### 4) Recommended demo flow

1. Type a feature (e.g. `Notion MCP Design Docs`)
2. Click **Run Draft**
3. Open the generated doc in Notion and show:
   - standard sections
   - References backlinks via Notion mentions
   - comment thread for unclear requirements
4. Add a new Source Note mentioning the same keyword
5. Click **Run Update**
6. Show the same doc updated (Sync Log appended)

