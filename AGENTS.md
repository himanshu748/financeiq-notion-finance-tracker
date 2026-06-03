# FinanceIQ Notes

## Project Shape
- Single-file FastAPI app in `main.py` with a vanilla static UI.
- Hugging Face categorizes/summarizes finance data; Notion stores workspaces, transactions, reports, and alerts.

## Commands
- Run tests with `python -m pytest`.
- Check syntax with `python -m compileall main.py tests`.
- Start locally with `uvicorn main:app --reload`.

## Conventions
- Notion MCP stdio is the primary path: `npx -y @notionhq/notion-mcp-server` with `NOTION_TOKEN`.
- Keep the REST client as fallback only when the Python MCP package is unavailable.
- `/api/health` must report `notion_transport` so MCP stdio and REST fallback are not confused.
- Do not commit `.env`, caches, generated output, or Notion page IDs/tokens.
