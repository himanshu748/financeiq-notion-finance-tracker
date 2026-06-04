"""
FinanceIQ — AI Finance Tracker powered by HuggingFace + Notion MCP
Categorizes expenses, flags anomalies, generates reports in Notion.
Run: uvicorn main:app --reload

Architecture:
  HuggingFace Inference API  →  structured content generation
  Notion MCP (stdio server)  →  ALL Notion reads & writes via MCP protocol
  FastAPI backend             →  orchestrates both
"""

import os, json, csv, io, logging
from datetime import date, datetime
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from huggingface_hub import InferenceClient
import httpx

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ModuleNotFoundError:
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

log = logging.getLogger("financeiq")

load_dotenv()

app = FastAPI(title="FinanceIQ")
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def hf_api_key() -> str:
    return get_env("HF_API_KEY") or get_env("HF_TOKEN")


def notion_parent_page_id() -> str:
    return get_env("NOTION_PARENT_PAGE_ID")


def hf_model() -> str:
    return get_env("HF_MODEL", "Qwen/Qwen2.5-72B-Instruct")


def notion_token_value() -> str:
    return get_env("NOTION_TOKEN") or get_env("NOTION_API_KEY")


# ─── Constants ────────────────────────────────────────────────────────────────

EXPENSE_CATEGORIES = [
    "Food & Dining", "Transport", "Shopping", "Utilities",
    "Healthcare", "Entertainment", "Travel", "Subscriptions",
    "Salary/Income", "Other",
]
TRANSACTION_TYPES = ["Income", "Expense"]
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# ─── Notion transport layer (MCP primary, httpx fallback) ────────────────────

NOTION_API = "https://api.notion.com/v1"
NOTION_VER = "2022-06-28"


class NotionHTTPFallback:
    """Direct Notion REST client — used when MCP stdio is unavailable."""

    def _h(self):
        return {
            "Authorization": f"Bearer {notion_token_value()}",
            "Notion-Version": NOTION_VER,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _required(payload: dict, key: str, tool: str):
        try:
            return payload.pop(key)
        except KeyError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Notion REST fallback missing required argument '{key}' for {tool}.",
            ) from exc

    @staticmethod
    def _json_response(response: httpx.Response) -> dict:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise HTTPException(
                status_code=502,
                detail=f"Notion REST request failed with HTTP {status}.",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="Notion REST returned invalid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=502,
                detail="Notion REST returned an unexpected payload shape.",
            )
        return payload

    async def call_tool(self, tool: str, args: dict) -> dict:
        payload = dict(args)
        async with httpx.AsyncClient(timeout=30) as c:
            if tool == "API-post-page":
                r = await c.post(f"{NOTION_API}/pages", headers=self._h(), json=payload)
            elif tool == "API-post-search":
                r = await c.post(f"{NOTION_API}/search", headers=self._h(), json=payload)
            elif tool == "API-post-database":
                r = await c.post(f"{NOTION_API}/databases", headers=self._h(), json=payload)
            elif tool == "API-post-database-query":
                db_id = self._required(payload, "database_id", tool)
                r = await c.post(
                    f"{NOTION_API}/databases/{db_id}/query",
                    headers=self._h(), json=payload,
                )
            elif tool == "API-get-block-children":
                bid = self._required(payload, "block_id", tool)
                r = await c.get(
                    f"{NOTION_API}/blocks/{bid}/children",
                    headers=self._h(), params=payload,
                )
            elif tool == "API-get-self":
                r = await c.get(f"{NOTION_API}/users/me", headers=self._h())
            elif tool == "API-patch-page":
                pid = self._required(payload, "page_id", tool)
                r = await c.patch(
                    f"{NOTION_API}/pages/{pid}", headers=self._h(), json=payload,
                )
            elif tool == "API-retrieve-a-page":
                pid = self._required(payload, "page_id", tool)
                r = await c.get(f"{NOTION_API}/pages/{pid}", headers=self._h())
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f"Unknown Notion tool: {tool}.",
                )
            return self._json_response(r)


def mcp_package_available() -> bool:
    return ClientSession is not None and StdioServerParameters is not None and stdio_client is not None


def notion_transport_name() -> str:
    return "mcp-stdio" if mcp_package_available() else "rest-fallback"


@asynccontextmanager
async def notion_mcp():
    """Spin up Notion MCP stdio server and yield a ClientSession."""
    token = notion_token_value()
    if not token:
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")
    if not mcp_package_available():
        log.warning("mcp package is not installed; using Notion REST fallback.")
        yield NotionHTTPFallback()
        return
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={**os.environ, "NOTION_TOKEN": token},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


notion_session = notion_mcp


async def mcp_call(session, tool: str, args: dict) -> dict:
    if isinstance(session, NotionHTTPFallback):
        return await session.call_tool(tool, args)
    result = await session.call_tool(tool, args)
    text = result.content[0].text if result.content else "{}"
    return json.loads(text)


# ─── Block builders ──────────────────────────────────────────────────────────

def _rt(content: str) -> list:
    return [{"text": {"content": content}}]


def _heading(text: str, level: int = 2) -> dict:
    k = f"heading_{level}"
    return {"object": "block", "type": k, k: {"rich_text": _rt(text)}}


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rt(text)},
    }


# ─── MCP helpers ─────────────────────────────────────────────────────────────

async def mcp_create_page(session, parent_id: str, title: str, children: list,
                          parent_type: str = "page_id") -> dict:
    """Create a Notion page via MCP API-post-page tool."""
    result = await mcp_call(session, "API-post-page", {
        "parent": {parent_type: parent_id},
        "properties": {"title": {"title": _rt(title)}},
        "children": children[:100],
    })
    if result.get("status") and result["status"] >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion MCP error: {result.get('message', str(result)[:200])}",
        )
    return result


async def mcp_search(session, query: str = "") -> list:
    result = await mcp_call(session, "API-post-search", {"query": query, "page_size": 50})
    return result.get("results", [])


async def mcp_get_children(session, block_id: str) -> list:
    result = await mcp_call(
        session, "API-get-block-children",
        {"block_id": block_id, "page_size": 100},
    )
    return result.get("results", [])


async def mcp_add_transaction(session, parent_id: str, tx: dict) -> dict:
    """Add a transaction as a sub-page under the expenses page."""
    cat = tx.get("category", "Other")
    amt = tx.get("amount", 0)
    anom = tx.get("anomaly", False)
    blocks = [
        _para(f'Date: {tx.get("date", "N/A")}'),
        _para(f'Amount: ${abs(amt):.2f}'),
        _para(f'Category: {cat}'),
        _para(f'Type: {"Income" if amt > 0 else "Expense"}'),
        _para(f'Anomaly: {"⚠️ Yes" if anom else "No"}'),
    ]
    if tx.get("notes"):
        blocks.append(_para(f'Notes: {tx["notes"]}'))
    title = f'{"⚠️ " if anom else ""}{tx.get("description", "Transaction")} — ${abs(amt):.2f}'
    return await mcp_create_page(session, parent_id, title, blocks)


async def mcp_read_transactions(session, query: str = "Expenses") -> list:
    """Read transaction pages from workspace via MCP search."""
    pages = await mcp_search(session, query)
    return pages


async def _legacy_mcp_add_db_entry(session, db_id: str, properties: dict) -> dict:
    """Add a row to a Notion database (if DB already exists)."""
    result = await mcp_call(session, "API-post-page", {
        "parent": {"database_id": db_id},
        "properties": properties,
    })
    return result


# ─── HuggingFace ─────────────────────────────────────────────────────────────

async def generate_text(system: str, user_msg: str) -> str:
    hf = InferenceClient(model=hf_model(), token=hf_api_key())
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    out = ""
    for chunk in hf.chat_completion(messages=messages, max_tokens=4096, stream=True):
        if chunk.choices:
            d = chunk.choices[0].delta
            if d.content:
                out += d.content
    return out


def _parse_json(raw: str) -> dict:
    s, e = raw.find("{"), raw.rfind("}") + 1
    if s == -1 or e <= s:
        raise HTTPException(status_code=502, detail="Model did not return valid JSON")
    try:
        payload = json.loads(raw[s:e])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Model did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Model did not return a JSON object")
    return payload


# ─── System Prompts ──────────────────────────────────────────────────────────

SETUP_SYSTEM = """You are FinanceIQ, an AI finance tracker.
Generate welcoming content for a new finance tracking workspace in JSON (no markdown fences):
{
  "welcome": "A warm 1-2 sentence welcome message for the user",
  "features": ["feature 1", "feature 2", "feature 3", "feature 4"],
  "getting_started": "Brief getting-started paragraph"
}
Keep it concise, friendly, and finance-focused."""

CATEGORIZE_SYSTEM = """You are FinanceIQ, an AI finance analyst.
Given a list of financial transactions, categorize each one and return a JSON object (no markdown fences):
{
  "transactions": [
    {
      "description": "original description",
      "date": "YYYY-MM-DD",
      "amount": 123.45,
      "category": "Food & Dining|Transport|Shopping|Utilities|Healthcare|Entertainment|Travel|Subscriptions|Salary/Income|Other",
      "type": "Income|Expense",
      "anomaly": true or false,
      "notes": "Brief AI insight about this transaction",
      "month": "January|February|...|December"
    }
  ],
  "summary": {
    "added": 0,
    "anomalies": 0,
    "categories": {"Food & Dining": 0}
  }
}

For each transaction:
1. Determine Category from: Food & Dining, Transport, Shopping, Utilities, Healthcare, Entertainment, Travel, Subscriptions, Salary/Income, Other
2. Determine Type: Income (if amount is positive/credit) or Expense (if negative/debit)
3. Detect anomalies — flag as true if:
   - Amount > 3x the average for that category
   - Duplicate-looking transaction within same day
   - Unusual merchant for the person's spending pattern
   - Round number > $500 (possible fraud)
4. Write a brief 1-sentence Note with your insight
5. Extract the Month from the date

Be thorough with categorization and insightful with notes."""

REPORT_SYSTEM = """You are FinanceIQ, an AI finance analyst.
Given expense transaction data, generate a financial report in JSON (no markdown fences):
{
  "executive_summary": "2-3 sentence overview referencing specific numbers",
  "total_income": 0.00,
  "total_expenses": 0.00,
  "net_savings": 0.00,
  "savings_rate": 0.0,
  "categories": [{"name": "Category", "amount": 0.00, "percent": 0.0}],
  "anomalies": [{"description": "...", "amount": 0.00, "reason": "why flagged"}],
  "biggest_expense": {"description": "...", "amount": 0.00},
  "most_frequent": "most frequent category or merchant",
  "recommendations": ["specific actionable tip 1", "specific tip 2", "specific tip 3"]
}

Compute:
- Total income and total expenses
- Net savings (income - expenses)
- Spending by category sorted highest to lowest
- Anomaly count
- Biggest single expense
- Most frequent merchant/category
- Savings rate percentage
- 3 specific, actionable tips based on the actual data

Make it genuinely insightful — reference specific numbers, not generic advice."""

BUDGET_SYSTEM = """You are FinanceIQ, an AI finance analyst.
Given expense data and budget limits, generate a budget analysis in JSON (no markdown fences):
{
  "summary": "1-2 sentence overview with key numbers",
  "under_budget": [{"category": "...", "spent": 0.00, "budget": 0.00, "remaining": 0.00}],
  "near_limit": [{"category": "...", "spent": 0.00, "budget": 0.00, "percent_used": 0.0}],
  "over_budget": [{"category": "...", "spent": 0.00, "budget": 0.00, "overage": 0.00}],
  "projected_total": 0.00,
  "suggestions": ["specific suggestion 1", "specific suggestion 2", "specific suggestion 3"]
}

Compare actual spending vs budget limits:
- Under budget categories with $ remaining
- Near limit categories (>80% used) with percent
- Over budget categories with overage amount
- Projected end-of-month spend based on current daily rate
- 3 specific suggestions to get back on track

Reference specific dollar amounts."""


# ─── Notion page builders ───────────────────────────────────────────────────

def build_report_blocks(data: dict, month: str, year: int) -> list:
    """Build Notion blocks for a financial report page."""
    b = []
    b.append(_heading("Executive Summary"))
    b.append(_para(data.get("executive_summary", "")))

    b.append(_heading("Income vs Expenses"))
    b.append(_bullet(f'Total Income: ${data.get("total_income", 0):,.2f}'))
    b.append(_bullet(f'Total Expenses: ${data.get("total_expenses", 0):,.2f}'))
    b.append(_bullet(f'Net Savings: ${data.get("net_savings", 0):,.2f}'))
    b.append(_bullet(f'Savings Rate: {data.get("savings_rate", 0):.1f}%'))

    b.append(_heading("Spending by Category"))
    for cat in data.get("categories", []):
        b.append(_bullet(
            f'{cat.get("name", "")}: ${cat.get("amount", 0):,.2f} '
            f'({cat.get("percent", 0):.1f}% of total)'
        ))

    if data.get("biggest_expense"):
        b.append(_heading("Biggest Single Expense"))
        be = data["biggest_expense"]
        b.append(_para(f'{be.get("description", "")}: ${be.get("amount", 0):,.2f}'))

    b.append(_heading("Most Frequent"))
    b.append(_para(data.get("most_frequent", "N/A")))

    anomalies = data.get("anomalies", [])
    if anomalies:
        b.append(_heading("⚠️ Anomalies"))
        for a in anomalies:
            b.append(_bullet(
                f'{a.get("description", "")}: ${a.get("amount", 0):,.2f} — '
                f'{a.get("reason", "")}'
            ))

    b.append(_heading("AI Recommendations"))
    for rec in data.get("recommendations", []):
        b.append(_bullet(rec))

    return b


def build_budget_blocks(data: dict, month: str) -> list:
    """Build Notion blocks for a budget alert page."""
    b = []
    b.append(_heading("Budget Overview"))
    b.append(_para(data.get("summary", "")))

    if data.get("over_budget"):
        b.append(_heading("🔴 Over Budget"))
        for item in data["over_budget"]:
            b.append(_bullet(
                f'{item.get("category", "")}: spent ${item.get("spent", 0):,.2f} '
                f'/ budget ${item.get("budget", 0):,.2f} '
                f'(over by ${item.get("overage", 0):,.2f})'
            ))

    if data.get("near_limit"):
        b.append(_heading("🟡 Near Limit (>80%)"))
        for item in data["near_limit"]:
            b.append(_bullet(
                f'{item.get("category", "")}: spent ${item.get("spent", 0):,.2f} '
                f'/ budget ${item.get("budget", 0):,.2f} '
                f'({item.get("percent_used", 0):.0f}% used)'
            ))

    if data.get("under_budget"):
        b.append(_heading("🟢 Under Budget"))
        for item in data["under_budget"]:
            b.append(_bullet(
                f'{item.get("category", "")}: spent ${item.get("spent", 0):,.2f} '
                f'/ budget ${item.get("budget", 0):,.2f} '
                f'(${item.get("remaining", 0):,.2f} remaining)'
            ))

    if data.get("projected_total"):
        b.append(_heading("Projected End-of-Month"))
        b.append(_para(f'Projected total spend: ${data["projected_total"]:,.2f}'))

    if data.get("suggestions"):
        b.append(_heading("💡 Suggestions"))
        for s in data["suggestions"]:
            b.append(_bullet(s))

    return b


# ─── Transaction helpers ────────────────────────────────────────────────────

def build_tx_properties(tx: dict) -> dict:
    """Build Notion database properties for a single transaction."""
    properties = {
        "Name": {"title": _rt(tx.get("description", "Unknown"))},
        "Amount": {"number": abs(tx.get("amount", 0))},
        "Category": {"select": {"name": tx.get("category", "Other")}},
        "Type": {"select": {"name": tx.get("type", "Expense")}},
        "Anomaly": {"checkbox": bool(tx.get("anomaly", False))},
        "Notes": {"rich_text": _rt(tx.get("notes", ""))},
    }
    if tx.get("date"):
        properties["Date"] = {"date": {"start": tx["date"]}}
    if tx.get("month"):
        properties["Month"] = {"select": {"name": tx["month"]}}
    return properties


def extract_transaction_from_page(page: dict) -> dict:
    """Extract transaction data from a Notion database page object."""
    props = page.get("properties", {})

    name = ""
    if "Name" in props and props["Name"].get("title"):
        name = "".join(t.get("plain_text", "") for t in props["Name"]["title"])

    amount = props.get("Amount", {}).get("number", 0) or 0

    category = ""
    if "Category" in props and props["Category"].get("select"):
        category = props["Category"]["select"].get("name", "")

    tx_type = ""
    if "Type" in props and props["Type"].get("select"):
        tx_type = props["Type"]["select"].get("name", "")

    anomaly = props.get("Anomaly", {}).get("checkbox", False)

    notes = ""
    if "Notes" in props and props["Notes"].get("rich_text"):
        notes = "".join(t.get("plain_text", "") for t in props["Notes"]["rich_text"])

    date_val = ""
    if "Date" in props and props["Date"].get("date"):
        date_val = props["Date"]["date"].get("start", "")

    month = ""
    if "Month" in props and props["Month"].get("select"):
        month = props["Month"]["select"].get("name", "")

    return {
        "description": name,
        "amount": amount,
        "category": category,
        "type": tx_type,
        "anomaly": anomaly,
        "notes": notes,
        "date": date_val,
        "month": month,
    }


# ─── CSV Parsing ─────────────────────────────────────────────────────────────

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

    transactions = []

    for row in rows:
        r = {k.lower().strip(): v.strip() for k, v in row.items()}

        date_val = (
            r.get("date") or r.get("transaction date") or r.get("posted date") or ""
        )
        desc = (
            r.get("description") or r.get("merchant") or r.get("name")
            or r.get("payee") or ""
        )

        amount = 0.0
        if "amount" in r:
            try:
                amount = float(r["amount"].replace("$", "").replace(",", ""))
            except ValueError:
                pass
        elif "debit" in r or "credit" in r:
            debit = float(
                r.get("debit", "0").replace("$", "").replace(",", "") or "0"
            )
            credit = float(
                r.get("credit", "0").replace("$", "").replace(",", "") or "0"
            )
            amount = credit - debit

        if desc:
            transactions.append({
                "date": date_val,
                "description": desc,
                "amount": amount,
            })

    return transactions


# ─── Request Models ──────────────────────────────────────────────────────────

class SetupRequest(BaseModel):
    parent_page_id: Optional[str] = None


class ManualTransactionRequest(BaseModel):
    transactions: List[dict]
    db_id: str


class ReportRequest(BaseModel):
    db_id: str
    reports_page_id: str
    month: str
    year: Optional[int] = None


class BudgetRequest(BaseModel):
    db_id: str
    parent_page_id: Optional[str] = None
    month: str
    budgets: dict


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html") as f:
        return f.read()


@app.get("/api/health")
async def health():
    """Health check — verifies MCP connectivity with API-get-self."""
    mcp_ok = False
    try:
        async with notion_session() as mcp:
            me = await mcp_call(mcp, "API-get-self", {})
            mcp_ok = notion_transport_name() == "mcp-stdio" and bool(me.get("id"))
    except Exception:
        pass
    return {
        "status": "ok",
        "hf_key": bool(hf_api_key()),
        "notion_token": bool(notion_token_value()),
        "parent_page_id": bool(notion_parent_page_id()),
        "mcp_connected": mcp_ok,
        "notion_transport": notion_transport_name(),
    }


@app.post("/api/setup")
async def setup_workspace(req: SetupRequest):
    """Create the FinanceIQ workspace, Expenses DB, and Reports page in Notion."""
    if not hf_api_key():
        raise HTTPException(status_code=500, detail="HF_API_KEY not set")
    if not notion_token_value():
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")
    parent_id = req.parent_page_id or notion_parent_page_id()
    if not parent_id:
        raise HTTPException(status_code=400, detail="parent_page_id required")

    # Step 1: Use HF to generate workspace welcome content
    raw = await generate_text(
        SETUP_SYSTEM,
        "Generate welcoming content for my new FinanceIQ workspace.",
    )
    content = _parse_json(raw)

    async with notion_session() as mcp:
        # Step 2: Create workspace hub page via MCP
        hub_blocks = [
            _heading("Welcome to FinanceIQ 💰"),
            _para(content.get("welcome", "Welcome to your AI-powered finance tracker!")),
            _heading("Features", 3),
        ]
        for feat in content.get("features", [
            "Upload bank CSVs for automatic categorization",
            "AI-powered anomaly detection",
            "Monthly financial reports",
            "Budget tracking and alerts",
        ]):
            hub_blocks.append(_bullet(feat))
        hub_blocks.append(_heading("Getting Started", 3))
        hub_blocks.append(_para(content.get(
            "getting_started",
            "Start by uploading a CSV or adding transactions manually.",
        )))

        hub = await mcp_create_page(mcp, parent_id, "💰 FinanceIQ Workspace", hub_blocks)
        hub_id = hub.get("id", "")

        # Step 3: Create Expenses page under the hub via MCP
        expenses_blocks = [
            _heading("Expense Tracker"),
            _para("Transactions will be added as sub-pages below."),
            _heading("Categories", 3),
        ]
        for c in EXPENSE_CATEGORIES:
            expenses_blocks.append(_bullet(c))
        expenses_page = await mcp_create_page(mcp, hub_id, "📊 Expenses", expenses_blocks)
        expenses_page_id = expenses_page.get("id", "")

        # Step 4: Create Reports page under the hub via MCP
        reports_blocks = [
            _heading("Financial Reports"),
            _para("Monthly reports will appear as sub-pages here."),
        ]
        reports_page = await mcp_create_page(
            mcp, hub_id, "📈 Reports", reports_blocks,
        )
        reports_page_id = reports_page.get("id", "")

    return {
        "status": "success",
        "text": "FinanceIQ workspace created successfully.",
        "summary": {
            "workspace_url": hub.get("url", ""),
            "expenses_db_url": expenses_page.get("url", ""),
            "expenses_db_id": expenses_page_id,
            "reports_page_id": reports_page_id,
        },
    }


@app.post("/api/upload-csv")
async def upload_csv(
    file: UploadFile = File(...),
    db_id: str = Form(...),
):
    """Upload a bank CSV, AI categorizes and loads into Notion."""
    if not hf_api_key():
        raise HTTPException(status_code=500, detail="HF_API_KEY not set")
    if not notion_token_value():
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")

    content = (await file.read()).decode("utf-8", errors="ignore")
    transactions = parse_csv(content)
    if not transactions:
        raise HTTPException(status_code=400, detail="No transactions found in CSV")

    # Step 1: Use HF to categorize transactions into JSON
    tx_json = json.dumps(transactions, indent=2)
    raw = await generate_text(
        CATEGORIZE_SYSTEM,
        f"Categorize these {len(transactions)} transactions:\n{tx_json}",
    )
    data = _parse_json(raw)
    categorized = data.get("transactions", [])

    # Step 2: Write each categorized transaction to Notion via MCP
    added = 0
    anomaly_count = 0
    async with notion_session() as mcp:
        for tx in categorized:
            await mcp_add_transaction(mcp, db_id, tx)
            added += 1
            if tx.get("anomaly"):
                anomaly_count += 1

    summary = data.get("summary", {
        "added": added,
        "anomalies": anomaly_count,
        "categories": {},
    })
    summary["added"] = added

    return {
        "status": "success",
        "parsed_count": len(transactions),
        "text": f"Categorized and added {added} transactions.",
        "summary": summary,
    }


@app.post("/api/add-manual")
async def add_manual(req: ManualTransactionRequest):
    """Add manually-entered transactions."""
    if not hf_api_key():
        raise HTTPException(status_code=500, detail="HF_API_KEY not set")
    if not notion_token_value():
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")

    # Step 1: Use HF to categorize transactions into JSON
    tx_json = json.dumps(req.transactions, indent=2)
    raw = await generate_text(
        CATEGORIZE_SYSTEM,
        f"Categorize these {len(req.transactions)} transactions:\n{tx_json}",
    )
    data = _parse_json(raw)
    categorized = data.get("transactions", [])

    # Step 2: Write each categorized transaction to Notion via MCP
    added = 0
    anomaly_count = 0
    async with notion_session() as mcp:
        for tx in categorized:
            await mcp_add_transaction(mcp, req.db_id, tx)
            added += 1
            if tx.get("anomaly"):
                anomaly_count += 1

    summary = data.get("summary", {
        "added": added,
        "anomalies": anomaly_count,
        "categories": {},
    })
    summary["added"] = added

    return {
        "status": "success",
        "text": f"Categorized and added {added} transactions.",
        "summary": summary,
    }


@app.post("/api/generate-report")
async def generate_report(req: ReportRequest):
    """Generate a monthly financial report page in Notion."""
    if not hf_api_key():
        raise HTTPException(status_code=500, detail="HF_API_KEY not set")
    if not notion_token_value():
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")

    year = req.year or date.today().year

    async with notion_session() as mcp:
        # Step 1: Read expenses from Notion via MCP search
        pages = await mcp_read_transactions(mcp, req.month)
        expenses = [extract_transaction_from_page(p) for p in pages]

        # Step 2: Use HF to generate report analysis JSON
        expense_text = (
            json.dumps(expenses, indent=2) if expenses
            else "No transactions found for this month."
        )
        raw = await generate_text(
            REPORT_SYSTEM,
            f"Analyze these {req.month} {year} transactions and generate a report:\n"
            f"{expense_text}",
        )
        report = _parse_json(raw)

        # Step 3: Create report page in Notion via MCP (API-post-page)
        blocks = build_report_blocks(report, req.month, year)
        page = await mcp_create_page(
            mcp, req.reports_page_id,
            f"📊 {req.month} {year} Financial Report",
            blocks,
        )

    return {
        "status": "success",
        "text": f"{req.month} {year} financial report generated.",
        "summary": {
            "report_url": page.get("url", ""),
            "total_income": report.get("total_income", 0),
            "total_expenses": report.get("total_expenses", 0),
            "net_savings": report.get("net_savings", 0),
            "anomalies": len(report.get("anomalies", [])),
        },
    }


@app.post("/api/budget-check")
async def budget_check(req: BudgetRequest):
    """Compare actual spend vs budget and create alert page."""
    if not hf_api_key():
        raise HTTPException(status_code=500, detail="HF_API_KEY not set")
    if not notion_token_value():
        raise HTTPException(status_code=500, detail="NOTION_TOKEN not set")

    parent_id = req.parent_page_id or notion_parent_page_id()

    async with notion_session() as mcp:
        # Step 1: Read expenses from Notion via MCP (API-post-database-query)
        pages = await mcp_read_transactions(mcp, req.month)
        expenses = [extract_transaction_from_page(p) for p in pages]

        # Step 2: Use HF to analyze spending vs budget JSON
        expense_text = (
            json.dumps(expenses, indent=2) if expenses
            else "No transactions found for this month."
        )
        budget_text = json.dumps(req.budgets, indent=2)
        raw = await generate_text(
            BUDGET_SYSTEM,
            f"Compare {req.month} spending against budgets.\n"
            f"Budget limits:\n{budget_text}\n"
            f"Transactions:\n{expense_text}",
        )
        analysis = _parse_json(raw)

        # Step 3: Create budget alert page in Notion via MCP (API-post-page)
        blocks = build_budget_blocks(analysis, req.month)
        page = await mcp_create_page(
            mcp, parent_id,
            f"🚨 Budget Alert — {req.month}",
            blocks,
        )

    return {
        "status": "success",
        "text": f"Budget analysis for {req.month} complete.",
        "summary": {
            "alert_url": page.get("url", ""),
            "over_budget": len(analysis.get("over_budget", [])),
            "near_limit": len(analysis.get("near_limit", [])),
            "under_budget": len(analysis.get("under_budget", [])),
        },
    }
