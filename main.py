"""
FinanceIQ — AI Finance Tracker powered by Claude + Notion MCP
Categorizes expenses, flags anomalies, generates reports in Notion.
Run: uvicorn main:app --reload
"""

import os
import json
import csv
import io
import httpx
from datetime import date, datetime
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI(title="FinanceIQ")
app.mount("/static", StaticFiles(directory="static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "")

NOTION_MCP_SERVER = {
    "type": "url",
    "url": "https://mcp.notion.com/sse",
    "name": "notion",
    "authorization_token": NOTION_TOKEN,
}

# ─── System Prompts ────────────────────────────────────────────────────────────

SETUP_PROMPT = """You are FinanceIQ, an AI finance tracker. 

Use Notion MCP tools to create a finance workspace under parent page ID: {parent_page_id}

Create these pages/databases:
1. A page titled "💰 FinanceIQ Workspace" as the hub
2. A database titled "📊 Expenses" with these properties:
   - Name (title) — transaction description
   - Date (date)
   - Amount (number, format: dollar)
   - Category (select): Food & Dining, Transport, Shopping, Utilities, Healthcare, Entertainment, Travel, Subscriptions, Salary/Income, Other
   - Type (select): Income, Expense
   - Anomaly (checkbox) — flagged as unusual
   - Notes (rich_text) — AI insight about this transaction
   - Month (select): January through December
3. A page titled "📈 Reports" — empty, ready for monthly reports

After creating, return JSON:
{{"workspace_url": "...", "expenses_db_url": "...", "expenses_db_id": "...", "reports_page_id": "..."}}

The expenses_db_id is critical — it's needed for adding transactions later.
"""

CATEGORIZE_PROMPT = """You are FinanceIQ, an AI finance analyst.

You have been given a list of financial transactions to process.

Use Notion MCP tools to add each transaction to the Expenses database (ID: {db_id}).

For each transaction:
1. Determine Category from: Food & Dining, Transport, Shopping, Utilities, Healthcare, Entertainment, Travel, Subscriptions, Salary/Income, Other
2. Determine Type: Income (if amount is positive/credit) or Expense (if negative/debit)
3. Detect anomalies — flag as true if:
   - Amount > 3x the average for that category
   - Duplicate-looking transaction within same day
   - Unusual merchant for the person's spending pattern
   - Round number > $500 (possible fraud)
4. Write a brief 1-sentence Note with your insight (e.g. "Unusually high restaurant spend — 4x your typical dining amount")
5. Extract the Month from the date

Transactions to process:
{transactions}

Add ALL of them to Notion. After finishing, return JSON:
{{"added": N, "anomalies": N, "categories": {{"Food & Dining": N, ...}}}}
"""

REPORT_PROMPT = """You are FinanceIQ, an AI finance analyst.

Use Notion MCP tools to:
1. Query the Expenses database (ID: {db_id}) and read ALL transactions for month: {month}
2. Compute:
   - Total income
   - Total expenses  
   - Net savings (income - expenses)
   - Spending by category (sorted highest to lowest)
   - Anomaly count
   - Biggest single expense
   - Most frequent merchant/category
3. Create a report page titled "📊 {month} {year} Financial Report" under the Reports page (ID: {reports_page_id}) with:
   - Executive summary paragraph (2-3 sentences, conversational)
   - Income vs Expenses table
   - Category breakdown table with % of total spend
   - ⚠️ Anomalies section — list each flagged transaction with why it's suspicious
   - 💡 AI Recommendations — 3 specific, actionable tips based on THIS person's actual data
   - Savings rate and trend commentary

Make it genuinely insightful — reference specific numbers, not generic advice.

Return JSON: {{"report_url": "...", "total_income": N, "total_expenses": N, "net_savings": N, "anomalies": N}}
"""

BUDGET_PROMPT = """You are FinanceIQ, an AI finance analyst.

Use Notion MCP tools to:
1. Query the Expenses database (ID: {db_id}) for all expense transactions this month ({month})
2. Compare actual spending vs these budget limits: {budgets}
3. Create a page titled "🚨 Budget Alert — {month}" under parent page (ID: {parent_page_id}) with:
   - ✅ Under budget categories (with $ remaining)
   - ⚠️ Near limit categories (>80% used)
   - 🔴 Over budget categories (exceeded, by how much)
   - Projected end-of-month spend based on current daily rate
   - 3 specific suggestions to get back on track

Return JSON: {{"alert_url": "...", "over_budget": N, "near_limit": N, "under_budget": N}}
"""


# ─── Core API Call ─────────────────────────────────────────────────────────────

async def call_claude_with_mcp(system: str, user_message: str) -> dict:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set")
    if not NOTION_TOKEN:
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
        "mcp_servers": [NOTION_MCP_SERVER],
        "betas": ["mcp-client-2025-04-04"],
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "mcp-client-2025-04-04",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        data = response.json()

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    full_text = "\n".join(text_blocks)

    try:
        start = full_text.rfind("{")
        end = full_text.rfind("}") + 1
        summary = json.loads(full_text[start:end]) if start != -1 and end > start else {}
    except Exception:
        summary = {}

    return {"text": full_text, "summary": summary}


# ─── CSV Parsing ───────────────────────────────────────────────────────────────

def parse_csv(content: str) -> List[dict]:
    """
    Parse expense CSV. Accepts common formats:
    date, description, amount
    date, description, debit, credit
    Date, Merchant, Amount, Category (bank exports)
    """
    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    if not rows:
        return []

    headers = [h.lower().strip() for h in rows[0].keys()]
    transactions = []

    for row in rows:
        r = {k.lower().strip(): v.strip() for k, v in row.items()}

        # Find date
        date_val = r.get("date") or r.get("transaction date") or r.get("posted date") or ""

        # Find description
        desc = r.get("description") or r.get("merchant") or r.get("name") or r.get("payee") or ""

        # Find amount — handle debit/credit columns
        amount = 0.0
        if "amount" in r:
            try:
                amount = float(r["amount"].replace("$", "").replace(",", ""))
            except ValueError:
                pass
        elif "debit" in r or "credit" in r:
            debit = float(r.get("debit", "0").replace("$", "").replace(",", "") or "0")
            credit = float(r.get("credit", "0").replace("$", "").replace(",", "") or "0")
            amount = credit - debit  # positive = income

        if desc:
            transactions.append({
                "date": date_val,
                "description": desc,
                "amount": amount,
            })

    return transactions


# ─── Request Models ────────────────────────────────────────────────────────────

class SetupRequest(BaseModel):
    parent_page_id: Optional[str] = None

class ManualTransactionRequest(BaseModel):
    transactions: List[dict]
    db_id: str

class ReportRequest(BaseModel):
    db_id: str
    reports_page_id: str
    month: str  # e.g. "March"
    year: Optional[int] = None

class BudgetRequest(BaseModel):
    db_id: str
    parent_page_id: Optional[str] = None
    month: str
    budgets: dict  # {"Food & Dining": 400, "Transport": 150, ...}


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html") as f:
        return f.read()


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "anthropic_key": bool(ANTHROPIC_API_KEY),
        "notion_token": bool(NOTION_TOKEN),
        "parent_page_id": bool(NOTION_PARENT_PAGE_ID),
    }


@app.post("/api/setup")
async def setup_workspace(req: SetupRequest):
    """Create the FinanceIQ workspace, Expenses DB, and Reports page in Notion."""
    parent_id = req.parent_page_id or NOTION_PARENT_PAGE_ID
    if not parent_id:
        raise HTTPException(status_code=400, detail="parent_page_id required")

    system = SETUP_PROMPT.replace("{parent_page_id}", parent_id)
    result = await call_claude_with_mcp(system, "Set up my FinanceIQ workspace in Notion now.")
    return {"status": "success", **result}


@app.post("/api/upload-csv")
async def upload_csv(
    file: UploadFile = File(...),
    db_id: str = Form(...),
):
    """Upload a bank CSV, AI categorizes and loads into Notion."""
    content = (await file.read()).decode("utf-8", errors="ignore")
    transactions = parse_csv(content)

    if not transactions:
        raise HTTPException(status_code=400, detail="No transactions found in CSV")

    tx_json = json.dumps(transactions, indent=2)
    system = CATEGORIZE_PROMPT.replace("{db_id}", db_id).replace("{transactions}", tx_json)
    result = await call_claude_with_mcp(
        system,
        f"Categorize and add these {len(transactions)} transactions to Notion."
    )
    return {"status": "success", "parsed_count": len(transactions), **result}


@app.post("/api/add-manual")
async def add_manual(req: ManualTransactionRequest):
    """Add manually-entered transactions."""
    tx_json = json.dumps(req.transactions, indent=2)
    system = CATEGORIZE_PROMPT.replace("{db_id}", req.db_id).replace("{transactions}", tx_json)
    result = await call_claude_with_mcp(
        system,
        f"Categorize and add these {len(req.transactions)} transactions to Notion."
    )
    return {"status": "success", **result}


@app.post("/api/generate-report")
async def generate_report(req: ReportRequest):
    """Generate a monthly financial report page in Notion."""
    year = req.year or date.today().year
    system = (
        REPORT_PROMPT
        .replace("{db_id}", req.db_id)
        .replace("{month}", req.month)
        .replace("{year}", str(year))
        .replace("{reports_page_id}", req.reports_page_id)
    )
    result = await call_claude_with_mcp(system, f"Generate the {req.month} {year} financial report.")
    return {"status": "success", **result}


@app.post("/api/budget-check")
async def budget_check(req: BudgetRequest):
    """Compare actual spend vs budget and create alert page."""
    parent_id = req.parent_page_id or NOTION_PARENT_PAGE_ID
    system = (
        BUDGET_PROMPT
        .replace("{db_id}", req.db_id)
        .replace("{month}", req.month)
        .replace("{budgets}", json.dumps(req.budgets))
        .replace("{parent_page_id}", parent_id)
    )
    result = await call_claude_with_mcp(
        system,
        f"Check my {req.month} spending against my budget limits."
    )
    return {"status": "success", **result}
