# FinanceIQ — AI Finance Tracker

> Powered by HuggingFace + Notion MCP — categorizes expenses, flags anomalies, generates financial reports directly in Notion.

`/api/health` reports `notion_transport` so MCP stdio and REST fallback are not confused. The primary Notion path is `npx -y @notionhq/notion-mcp-server` with `NOTION_TOKEN` passed to the server environment.

## Features

- **One-click Notion setup** — creates Expenses database + Reports page
- **CSV import** — upload bank exports (Chase, BoA, Wells Fargo, Mint)
- **AI categorization** — auto-categorizes transactions into 10 categories
- **Anomaly detection** — flags suspicious spending (3x average, duplicates, round amounts)
- **Monthly reports** — income vs expenses, category breakdown, AI recommendations
- **Budget alerts** — compares actuals vs limits, creates alert pages in Notion

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables
cp .env.example .env
# Edit .env with your keys

# 3. Run the server
uvicorn main:app --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `HF_API_KEY` | Yes | Your HuggingFace API key |
| `HF_MODEL` | No | Model ID (default: mistralai/Mistral-Small-3.1-24B-Instruct-2503) |
| `NOTION_TOKEN` | Yes | Notion integration token |
| `NOTION_PARENT_PAGE_ID` | No | Parent page for workspace (can set in UI) |

## How It Works

1. **Setup** — Creates a FinanceIQ workspace in Notion with an Expenses database and Reports page
2. **Import** — Upload a CSV or manually enter transactions. AI categorizes each one, detects anomalies, and adds them to Notion
3. **Report** — Generate a monthly financial report with income/expense analysis, category breakdown, and AI recommendations
4. **Budget** — Set monthly budget limits per category. AI compares actuals and creates an alert page

## Tech Stack

- **Backend**: FastAPI + Python
- **AI**: HuggingFace (MCPClient with Notion MCP)
- **Data**: Notion (via MCP connector)
- **Frontend**: Vanilla HTML/CSS/JS — newspaper-inspired design
