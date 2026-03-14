# FinanceIQ — AI Finance Tracker

> Powered by Claude + Notion MCP — categorizes expenses, flags anomalies, generates financial reports directly in Notion.

## Features

- **One-click Notion setup** — creates Expenses database + Reports page
- **CSV import** — upload bank exports (Chase, BoA, Wells Fargo, Mint)
- **AI categorization** — Claude auto-categorizes transactions into 10 categories
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
| `ANTHROPIC_API_KEY` | ✅ | Your Anthropic API key |
| `NOTION_TOKEN` | ✅ | Notion integration token |
| `NOTION_PARENT_PAGE_ID` | Optional | Parent page for workspace (can set in UI) |

## How It Works

1. **Setup** — Creates a FinanceIQ workspace in Notion with an Expenses database and Reports page
2. **Import** — Upload a CSV or manually enter transactions. Claude categorizes each one, detects anomalies, and adds them to Notion
3. **Report** — Generate a monthly financial report with income/expense analysis, category breakdown, and AI recommendations
4. **Budget** — Set monthly budget limits per category. Claude compares actuals and creates an alert page

## Tech Stack

- **Backend**: FastAPI + Python
- **AI**: Claude (Anthropic API with MCP)
- **Data**: Notion (via MCP connector)
- **Frontend**: Vanilla HTML/CSS/JS — newspaper-inspired design
