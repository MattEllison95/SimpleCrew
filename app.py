import requests
import sqlite3
import time
import functools
import os
import threading
from datetime import datetime, date, timedelta
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIGURATION ---
URL = "https://api.trycrew.com/willow/graphql"
# In app.py
DB_FILE = os.environ.get("DB_FILE", "savings_data.db")

# --- CACHING SYSTEM ---
class SimpleCache:
    def __init__(self, ttl_seconds=300):
        self.store = {}
        self.ttl = ttl_seconds

    def get(self, key):
        if key in self.store:
            timestamp, data = self.store[key]
            if time.time() - timestamp < self.ttl:
                return data
            else:
                del self.store[key]  # Expired
        return None

    def set(self, key, data):
        self.store[key] = (time.time(), data)

    def clear(self):
        self.store = {}

cache = SimpleCache(ttl_seconds=300)

def cached(key_prefix):
    """Decorator to cache function results. Supports force_refresh=True kwarg."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            force_refresh = kwargs.pop('force_refresh', False)
            key_parts = [key_prefix] + [str(arg) for arg in args] + [f"{k}={v}" for k, v in kwargs.items()]
            cache_key = ":".join(key_parts)
            
            if not force_refresh:
                cached_data = cache.get(cache_key)
                if cached_data:
                    print(f"⚡ Serving {key_prefix} from cache")
                    return cached_data
            
            print(f"🌐 Fetching {key_prefix} from API (Fresh)...")
            result = func(*args, **kwargs)
            
            if isinstance(result, dict) and "error" not in result:
                cache.set(cache_key, result)
            return result
        return wrapper
    return decorator


# 1. UPDATE DATABASE SCHEMA
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history (date TEXT PRIMARY KEY, balance REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)''')
    
    # Updated to include sort_order
    c.execute('''CREATE TABLE IF NOT EXISTS pocket_links (
        pocket_id TEXT PRIMARY KEY, 
        group_id INTEGER,
        sort_order INTEGER DEFAULT 0
    )''')
    
    # Migration helper: Check if sort_order exists, if not, add it (for existing DBs)
    try:
        c.execute("SELECT sort_order FROM pocket_links LIMIT 1")
    except sqlite3.OperationalError:
        print("Migrating DB: Adding sort_order column...")
        c.execute("ALTER TABLE pocket_links ADD COLUMN sort_order INTEGER DEFAULT 0")
    
    # Store credit card account selection from LunchFlow
    c.execute('''CREATE TABLE IF NOT EXISTS credit_card_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT UNIQUE NOT NULL,
        account_name TEXT,
        pocket_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Migration: Add pocket_id column if it doesn't exist
    try:
        c.execute("SELECT pocket_id FROM credit_card_config LIMIT 1")
    except sqlite3.OperationalError:
        print("Migrating DB: Adding pocket_id column to credit_card_config...")
        c.execute("ALTER TABLE credit_card_config ADD COLUMN pocket_id TEXT")
    
    # Store seen credit card transactions to avoid duplicates
    c.execute('''CREATE TABLE IF NOT EXISTS credit_card_transactions (
        transaction_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        amount REAL,
        date TEXT,
        merchant TEXT,
        description TEXT,
        is_pending INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    conn.close()

def log_balance(balance):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        c.execute("INSERT OR REPLACE INTO history (date, balance) VALUES (?, ?)", (today, balance))
        conn.commit()
    except Exception as e:
        print(f"DB Error: {e}")
    finally:
        conn.close()

def get_history():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT date, balance FROM history ORDER BY date ASC")
    data = c.fetchall()
    conn.close()
    return {
        "labels": [row[0] for row in data],
        "values": [row[1] for row in data]
    }

# --- API HELPERS ---
def get_crew_headers():
    # We now look for Environment Variables provided by Docker
    bearer_token = os.environ.get("BEARER_TOKEN")


    return {
        "accept": "*/*",
        "content-type": "application/json",
        "authorization": bearer_token,
        "user-agent": "Crew/1 CFNetwork/3860.300.31 Darwin/25.2.0",
    }

# --- DATA FETCHERS ---
@cached("primary_account_id")
def get_primary_account_id():
    try:
        headers = get_crew_headers()
        if not headers: return None
        query_string = """ query CurrentUser { currentUser { accounts { id displayName } } } """
        response = requests.post(URL, headers=headers, json={"operationName": "CurrentUser", "query": query_string})
        data = response.json()
        accounts = data.get("data", {}).get("currentUser", {}).get("accounts", [])
        for acc in accounts:
            if acc.get("displayName") == "Checking":
                return acc.get("id")
        if accounts: return accounts[0].get("id")
        return None
    except Exception as e:
        print(f"Error fetching Account ID: {e}")
        return None

@cached("financial_data")
def get_financial_data():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}

        # We fetch all accounts and subaccounts
        query_string = """ query CurrentUser { currentUser { accounts { subaccounts { id goal overallBalance name } } } } """
        response = requests.post(URL, headers=headers, json={"operationName": "CurrentUser", "query": query_string})
        data = response.json()

        results = {
            "checking": None,
            "total_goals": 0.0  # This will hold the sum of ALL non-checking pockets
        }

        print("--- DEBUG: CALCULATING POCKETS ---")
        for account in data.get("data", {}).get("currentUser", {}).get("accounts", []):
            for sub in account.get("subaccounts", []):
                name = sub.get("name")
                # Crew API returns balance in cents, so we divide by 100
                balance_raw = sub.get("overallBalance", 0) / 100.0

                if name == "Checking":
                    # This is your main Safe-to-Spend source
                    results["checking"] = {
                        "name": name,
                        "balance": f"${balance_raw:.2f}",
                        "raw_balance": balance_raw
                    }
                else:
                    # If it is NOT "Checking", we treat it as a Pocket and add it to the total
                    results["total_goals"] += balance_raw
                    print(f"Adding Pocket '{name}': ${balance_raw}")

        print(f"TOTAL POCKETS: ${results['total_goals']}")
        print("----------------------------------")

        if not results["checking"]:
            return {"error": "Checking account not found"}

        return results

    except Exception as e:
        print(f"Error in get_financial_data: {e}")
        return {"error": str(e)}

@cached("transactions")
def get_transactions_data(search_term=None, min_date=None, max_date=None, min_amount=None, max_amount=None):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        account_id = get_primary_account_id()
        if not account_id: return {"error": "Could not find Checking Account ID"}
        query_string = """ query RecentActivity($accountId: ID!, $cursor: String, $pageSize: Int = 100, $searchFilters: CashTransactionFilter) { account: node(id: $accountId) { ... on Account { id cashTransactions(first: $pageSize, after: $cursor, searchFilters: $searchFilters) { edges { node { id amount description occurredAt title type subaccount { id } } } } } } } """
        filters = {}
        if search_term: filters["fuzzySearch"] = search_term
        variables = {"pageSize": 100, "accountId": account_id, "searchFilters": filters}
        response = requests.post(URL, headers=headers, json={"operationName": "RecentActivity", "variables": variables, "query": query_string})
        if response.status_code != 200: return {"error": f"API Error: {response.text}"}
        data = response.json()
        if 'errors' in data: return {"error": data['errors'][0]['message']}
        txs = []
        try:
            edges = data.get('data', {}).get('account', {}).get('cashTransactions', {}).get('edges', [])
            for edge in edges:
                node = edge['node']
                amt = node['amount'] / 100.0
                date_str = node['occurredAt']
                sub_id = node.get('subaccount', {}).get('id') if node.get('subaccount') else None
                if min_date or max_date:
                    tx_date = date_str[:10]
                    if min_date and tx_date < min_date: continue
                    if max_date and tx_date > max_date: continue
                if min_amount or max_amount:
                    abs_amt = abs(amt)
                    if min_amount and abs_amt < float(min_amount): continue
                    if max_amount and abs_amt > float(max_amount): continue
                txs.append({"id": node['id'], "title": node['title'], "description": node['description'], "amount": amt, "date": date_str, "type": node['type'], "subaccountId": sub_id})
        except Exception as e:
            return {"error": f"Parse Error: {str(e)}"}
        return {"transactions": txs}
    except Exception as e:
        return {"error": str(e)}

@cached("user_profile_info")
def get_user_profile_info():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        # Updated Query to include imageUrl
        query_string = """ 
        query CurrentUser { 
            currentUser { 
                firstName 
                lastName
                imageUrl
            } 
        } 
        """
        
        response = requests.post(URL, headers=headers, json={
            "operationName": "CurrentUser", 
            "query": query_string
        })
        
        data = response.json()
        user = data.get("data", {}).get("currentUser", {})
        
        return {
            "firstName": user.get("firstName", ""),
            "lastName": user.get("lastName", ""),
            "imageUrl": user.get("imageUrl") # Can be None or a URL string
        }
    except Exception as e:
        return {"error": str(e)}


@cached("intercom_data")
def get_intercom_data():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        # GraphQL Query
        query_string = """
        query IntercomToken($platform: IntercomPlatform!) {
          currentUser {
            id
            intercomJwt(platform: $platform)
          }
        }
        """
        
        variables = {"platform": "WEB"}
        
        response = requests.post(URL, headers=headers, json={
            "operationName": "IntercomToken",
            "variables": variables,
            "query": query_string
        })
        
        data = response.json()
        user = data.get("data", {}).get("currentUser", {})
        
        if not user:
            return {"error": "User data not found"}

        # Return the exact keys requested
        return {
            "user_data": {
                "user_id": user.get("id"),
                "intercom_user_jwt": user.get("intercomJwt")
            }
        }
    except Exception as e:
        return {"error": str(e)}

@cached("tx_detail")
def get_transaction_detail(activity_id):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        query_string = """ query ActivityDetail($activityId: ID!, $isTransfer: Boolean = false) { cashTransaction: node(id: $activityId) @skip(if: $isTransfer) { ... on CashTransaction { ...CashTransactionActivity __typename } __typename } pendingTransfer: node(id: $activityId) @include(if: $isTransfer) { ... on Transfer { ...PendingTransferActivity __typename } __typename } } fragment CashTransactionFields on CashTransaction { id amount avatarFallbackColor currencyCode description externalMemo imageUrl isSplit note occurredAt quickCleanName ruleSuggestionString status title type __typename } fragment NameableAccount on Account { id displayName belongsToCurrentUser isChildAccount isExternalAccount avatarUrl icon type mask owner { displayName avatarUrl avatarColor __typename } __typename } fragment NameableSubaccount on Subaccount { id type belongsToCurrentUser isChildAccount isExternalAccount displayName avatarUrl icon piggyBanked isPrimary status account { id __typename } owner { displayName avatarUrl avatarColor __typename } primaryOwner { id __typename } __typename } fragment NameableCashTransaction on CashTransaction { __typename id amount description externalMemo avatarFallbackColor imageUrl quickCleanName title type account { ...NameableAccount __typename } subaccount { ...NameableSubaccount __typename } } fragment RelatedTransactions on CashTransaction { id status occurredAt relatedTransactions { id occurredAt __typename } transfer { id type status scheduledSettlement __typename } __typename } fragment TransferFields on Transfer { id amount formattedErrorCode isCancellable note occurredAt scheduledSettlement status type accountFrom { ...NameableAccount __typename } accountTo { ...NameableAccount __typename } subaccountFrom { ...NameableSubaccount __typename } subaccountTo { ...NameableSubaccount __typename } permittedActions { transferReassign __typename } __typename } fragment CashTransactionActivity on CashTransaction { ...CashTransactionFields ...NameableCashTransaction ...RelatedTransactions account { id subaccounts { id belongsToCurrentUser clearedBalance displayName isExternalAccount owner { displayName __typename } __typename } __typename } latestDebitCardTransactionDetail { id merchantAddress1 merchantCity merchantCountry merchantName merchantState merchantZip __typename } debitCard { id name type cardOwner: user { id displayedFirstName __typename } __typename } transfer { ...TransferFields accountTo { id primaryOwner { id displayedFirstName __typename } __typename } __typename } subaccount { id displayName __typename } permittedActions { cashTransactionReassign cashTransactionSplit cashTransactionUndo __typename } __typename } fragment PendingTransferActivity on Transfer { ...TransferFields __typename } """
        variables = {"isTransfer": False, "activityId": activity_id}
        response = requests.post(URL, headers=headers, json={"operationName": "ActivityDetail", "variables": variables, "query": query_string})
        data = response.json()
        node = data.get('data', {}).get('cashTransaction') or data.get('data', {}).get('pendingTransfer')
        if not node: return {"error": "Details not found"}
        merchant_info = node.get('latestDebitCardTransactionDetail') or {}
        return {"id": node.get('id'), "amount": node.get('amount', 0) / 100.0, "title": node.get('title'), "description": node.get('description'), "status": node.get('status'), "date": node.get('occurredAt'), "memo": node.get('externalMemo'), "merchant": {"name": merchant_info.get('merchantName'), "address": merchant_info.get('merchantAddress1'), "city": merchant_info.get('merchantCity'), "state": merchant_info.get('merchantState'), "zip": merchant_info.get('merchantZip')}}
    except Exception as e:
        return {"error": str(e)}

@cached("expenses")
def get_expenses_data():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        # Updated query to include funding settings
        query_string = """ 
        query CurrentUser { 
            currentUser { 
                accounts { 
                    billReserve { 
                        nextFundingDate 
                        totalReservedAmount 
                        estimatedNextFundingAmount 
                        settings { 
                            funding { 
                                subaccount { 
                                    displayName 
                                } 
                            } 
                        }
                        bills { 
                            amount 
                            anchorDate 
                            autoAdjustAmount 
                            dayOfMonth 
                            daysOverdue 
                            estimatedNextFundingAmount 
                            frequency 
                            frequencyInterval 
                            id 
                            name 
                            paused 
                            reservedAmount 
                            reservedBy 
                            status 
                        } 
                    } 
                } 
            } 
        } 
        """
        response = requests.post(URL, headers=headers, json={"operationName": "CurrentUser", "query": query_string})
        data = response.json()
        accounts = data.get("data", {}).get("currentUser", {}).get("accounts", [])
        
        all_bills = []
        summary = {}
        
        for acc in accounts:
            bill_reserve = acc.get("billReserve")
            if bill_reserve:
                # Extract funding source name safely
                funding_name = "Checking"
                try:
                    funding_name = bill_reserve["settings"]["funding"]["subaccount"]["displayName"]
                except (KeyError, TypeError):
                    pass

                summary = {
                    "totalReserved": (bill_reserve.get("totalReservedAmount") or 0) / 100.0, 
                    "nextFundingDate": bill_reserve.get("nextFundingDate"), 
                    "estimatedFunding": (bill_reserve.get("estimatedNextFundingAmount") or 0) / 100.0,
                    "fundingSource": funding_name # <--- Added this
                }
                
                bills = bill_reserve.get("bills", [])
                for b in bills:
                    amt = (b.get("amount") or 0) / 100.0
                    res = (b.get("reservedAmount") or 0) / 100.0
                    est_fund = (b.get("estimatedNextFundingAmount") or 0) / 100.0
                    all_bills.append({
                        "id": b.get("id"), 
                        "name": b.get("name"), 
                        "amount": amt, 
                        "reserved": res, 
                        "estimatedFunding": est_fund, 
                        "frequency": b.get("frequency"), 
                        "dueDay": b.get("dayOfMonth"), 
                        "paused": b.get("paused"), 
                        "reservedBy": b.get("reservedBy")
                    })
        
        all_bills.sort(key=lambda x: x['reservedBy'] or "9999-12-31")
        return {"expenses": all_bills, "summary": summary}
    except Exception as e:
        return {"error": str(e)}
        
# --- DATA FETCHERS (Update get_goals_data) ---
# 2. UPDATE GET_GOALS TO SORT
@cached("goals")
def get_goals_data():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        # 1. Fetch from API
        query_string = """ query CurrentUser { currentUser { accounts { subaccounts { goal overallBalance name id } } } } """
        response = requests.post(URL, headers=headers, json={"operationName": "CurrentUser", "query": query_string})
        data = response.json()
        
        # 2. Fetch Groups and Links from DB
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        c.execute("SELECT id, name FROM groups")
        group_rows = c.fetchall()
        groups_dict = {row[0]: row[1] for row in group_rows}
        
        # Get links with Sorting
        c.execute("SELECT pocket_id, group_id, sort_order FROM pocket_links")
        link_rows = c.fetchall()
        # Create lookups
        links_dict = {row[0]: row[1] for row in link_rows} 
        order_dict = {row[0]: row[2] for row in link_rows}
        
        # Get credit card pocket IDs
        c.execute("SELECT pocket_id FROM credit_card_config WHERE pocket_id IS NOT NULL")
        credit_card_pocket_ids = {row[0] for row in c.fetchall()}
        
        conn.close()

        goals = []
        for account in data.get("data", {}).get("currentUser", {}).get("accounts", []):
            for sub in account.get("subaccounts", []):
                name = sub.get("name")
                if name != "Checking":
                    balance = sub.get("overallBalance", 0) / 100.0
                    target = sub.get("goal", 0) / 100.0 if sub.get("goal") else 0
                    p_id = sub.get("id")
                    
                    g_id = links_dict.get(p_id)
                    g_name = groups_dict.get(g_id)
                    # Default sort order to 999 if not set, so new items appear at bottom
                    s_order = order_dict.get(p_id, 999)
                    
                    # Check if this is a credit card pocket
                    is_credit_card = p_id in credit_card_pocket_ids
                    
                    goals.append({
                        "id": p_id, 
                        "name": name, 
                        "balance": balance, 
                        "target": target, 
                        "status": "Active",
                        "groupId": g_id,
                        "groupName": g_name,
                        "sortOrder": s_order,
                        "isCreditCard": is_credit_card
                    })
        
        # Python-side sort based on the DB order
        goals.sort(key=lambda x: x['sortOrder'])
        
        return {"goals": goals, "all_groups": [{"id": k, "name": v} for k,v in groups_dict.items()]}
    except Exception as e:
        return {"error": str(e)}

@cached("trends")
def get_monthly_trends():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        account_id = get_primary_account_id()
        if not account_id: return {"error": "Could not find Checking Account ID"}
        today = date.today()
        start_of_month = date(today.year, today.month, 1).strftime("%Y-%m-%dT00:00:00Z")
        query_string = """ query RecentActivity($accountId: ID!, $cursor: String, $pageSize: Int = 100) { account: node(id: $accountId) { ... on Account { cashTransactions(first: $pageSize, after: $cursor) { edges { node { amount occurredAt } } } } } } """
        variables = {"pageSize": 100, "accountId": account_id}
        response = requests.post(URL, headers=headers, json={"operationName": "RecentActivity", "variables": variables, "query": query_string})
        data = response.json()
        edges = data.get('data', {}).get('account', {}).get('cashTransactions', {}).get('edges', [])
        earned = 0.0
        spent = 0.0
        for edge in edges:
            node = edge['node']
            tx_date = node['occurredAt']
            amount = node['amount'] / 100.0
            if tx_date >= start_of_month:
                if amount > 0:
                    earned += amount
                else:
                    spent += abs(amount)
        return {"earned": earned, "spent": spent}
    except Exception as e:
        return {"error": str(e)}

@cached("subaccounts")
def get_subaccounts_list():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        query_string = """ query CurrentUser { currentUser { accounts { subaccounts { id name overallBalance } } } } """
        response = requests.post(URL, headers=headers, json={"operationName": "CurrentUser", "query": query_string})
        data = response.json()
        subs = []
        for account in data.get("data", {}).get("currentUser", {}).get("accounts", []):
            for sub in account.get("subaccounts", []):
                balance = sub.get("overallBalance", 0) / 100.0
                subs.append({"id": sub.get("id"), "name": sub.get("name"), "balance": balance})
        return {"subaccounts": subs}
    except Exception as e:
        return {"error": str(e)}

def move_money(from_id, to_id, amount, note=""):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        query_string = """ mutation InitiateTransferScottie($input: InitiateTransferInput!) { initiateTransfer(input: $input) { result { id __typename } __typename } } """
        amount_cents = int(float(amount) * 100)
        variables = {"input": {"amount": amount_cents, "accountFromId": from_id, "accountToId": to_id, "note": note or "Transfer"}}
        response = requests.post(URL, headers=headers, json={"operationName": "InitiateTransferScottie", "variables": variables, "query": query_string})
        data = response.json()
        if 'errors' in data: return {"error": data['errors'][0]['message']}
        print("🧹 Clearing Cache after transaction...")
        cache.clear()
        return {"success": True, "result": data.get("data", {}).get("initiateTransfer", {})}
    except Exception as e:
        return {"error": str(e)}

@cached("family")
def get_family_data():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        query_string = """ query FamilyScreen { currentUser { id family { id children { id dob cardColor imageUrl displayedFirstName spendAccount { id overallBalance subaccounts { id displayName clearedBalance } } scheduledAllowance { id totalAmount } } parents { id isApplying cardColor imageUrl displayedFirstName } } } } """
        response = requests.post(URL, headers=headers, json={"operationName": "FamilyScreen", "query": query_string})
        data = response.json()
        family_node = data.get("data", {}).get("currentUser", {}).get("family", {})
        children = []
        for child in family_node.get("children", []):
            balance = child.get("spendAccount", {}).get("overallBalance", 0) / 100.0
            allowance = "Not set"
            if child.get("scheduledAllowance"):
                amt = child["scheduledAllowance"].get("totalAmount", 0) / 100.0
                allowance = f"${amt:.2f}/week"
            children.append({"id": child.get("id"), "name": child.get("displayedFirstName"), "image": child.get("imageUrl"), "color": child.get("cardColor"), "dob": child.get("dob"), "balance": balance, "allowance": allowance, "role": "Child"})
        parents = []
        for parent in family_node.get("parents", []):
            parents.append({"id": parent.get("id"), "name": parent.get("displayedFirstName"), "image": parent.get("imageUrl"), "color": parent.get("cardColor"), "role": "Parent"})
        return {"children": children, "parents": parents}
    except Exception as e:
        return {"error": str(e)}

def create_pocket(name, target_amount, initial_amount, note):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        # Get the main Account ID automatically
        account_id = get_primary_account_id()
        if not account_id: return {"error": "Could not find Checking Account ID"}

        query_string = """
        mutation CreateSubaccount($input: CreateSubaccountInput!) {
            createSubaccount(input: $input) {
                result {
                    id
                    name
                    balance
                    goal
                    status
                    subaccountType
                }
            }
        }
        """
        
        # Convert amounts to cents (assuming API expects cents based on your move_money logic)
        target_cents = int(float(target_amount) * 100)
        initial_cents = int(float(initial_amount) * 100)

        variables = {
            "input": {
                "type": "SAVINGS",           # Hardcoded per instructions
                "piggyBanked": False,        # Hardcoded per instructions
                "accountId": account_id,     # Auto-filled
                "name": name,
                "targetAmount": target_cents,
                "initialTransferAmount": initial_cents,
                "note": note
            }
        }

        response = requests.post(URL, headers=headers, json={
            "operationName": "CreateSubaccount",
            "variables": variables,
            "query": query_string
        })

        data = response.json()
        
        if 'errors' in data:
            return {"error": data['errors'][0]['message']}
            
        # Clear cache so the new pocket appears immediately
        print("🧹 Clearing Cache after pocket creation...")
        cache.clear()
        
        return {"success": True, "result": data.get("data", {}).get("createSubaccount", {}).get("result")}

    except Exception as e:
        return {"error": str(e)}

@cached("cards")
def get_cards_data():
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        # 1. New Query for Physical Cards (Parents & Children)
        query_phys = """ 
        query PhysicalCards {
          currentUser {
            id
            family {
              id
              parents {
                id
                activePhysicalDebitCard {
                  ...PhysicalDebitCardFields
                  __typename
                }
                issuingPhysicalDebitCard {
                  ...PhysicalDebitCardFields
                  __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
        }

        fragment PhysicalDebitCardFields on DebitCard {
          id
          color
          status
          lastFour
          user {
            id
            isChild
            firstName
            userSpendConfig {
              id
              selectedSpendSubaccount {
                id
                name
                __typename
              }
              __typename
            }
            __typename
          }
          __typename
        }
        """
        
        # We only execute the Physical card query for now as requested
        res_phys = requests.post(URL, headers=headers, json={"operationName": "PhysicalCards", "query": query_phys})
        data_phys = res_phys.json()
        
        all_cards = []
        
        # 2. Parse Parents Only (as requested)
        fam = data_phys.get("data", {}).get("currentUser", {}).get("family", {}) or {}
        parents = fam.get("parents") or []
        
        for parent in parents:
            # Active Card
            card = parent.get("activePhysicalDebitCard")
            if card:
                user_data = card.get("user", {})
                config = user_data.get("userSpendConfig")
                
                # Determine current spend source
                spend_source_id = "Checking"
                if config and config.get("selectedSpendSubaccount"):
                    spend_source_id = config["selectedSpendSubaccount"]["id"]
                
                all_cards.append({
                    "id": card.get("id"),
                    "userId": user_data.get("id"),
                    "type": "Physical",
                    "name": "Simple Visa® Card",
                    "holder": user_data.get("firstName"),
                    "last4": card.get("lastFour"),
                    "color": card.get("color"),
                    "status": card.get("status"),
                    "current_spend_id": spend_source_id 
                })

        return {"cards": all_cards}
    except Exception as e:
        print(f"Card Error: {e}")
        return {"error": str(e)}


def delete_subaccount_action(sub_id):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}

        # Crew API Mutation
        query_string = """
        mutation DeleteSubaccount($id: ID!) {
            deleteSubaccount(input: { subaccountId: $id }) {
                result {
                    id
                    name
                    status
                }
            }
        }
        """
        
        variables = {"id": sub_id}

        response = requests.post(URL, headers=headers, json={
            "operationName": "DeleteSubaccount",
            "variables": variables,
            "query": query_string
        })

        data = response.json()
        
        if 'errors' in data:
            return {"error": data['errors'][0]['message']}

        # --- NEW: Clean up local DB ---
        # This ensures the deleted pocket is removed from your local grouping table
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM pocket_groups WHERE pocket_id = ?", (sub_id,))
            conn.commit()
        except Exception as e:
            print(f"Warning: Failed to cleanup local DB group: {e}")
        finally:
            if conn: conn.close()
            
        print("🧹 Clearing Cache after deletion...")
        cache.clear()
        
        return {"success": True, "result": data.get("data", {}).get("deleteSubaccount", {}).get("result")}

    except Exception as e:
        return {"error": str(e)}

def delete_bill_action(bill_id):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}

        # Mutation based on your input
        query_string = """
        mutation DeleteBill($id: ID!) {
            deleteBill(input: { billId: $id }) {
                result {
                    id
                    status
                    name
                }
            }
        }
        """
        
        variables = {"id": bill_id}

        response = requests.post(URL, headers=headers, json={
            "operationName": "DeleteBill",
            "variables": variables,
            "query": query_string
        })

        data = response.json()
        
        if 'errors' in data:
            return {"error": data['errors'][0]['message']}
            
        print("🧹 Clearing Cache after bill deletion...")
        cache.clear()
        
        return {"success": True, "result": data.get("data", {}).get("deleteBill", {}).get("result")}

    except Exception as e:
        return {"error": str(e)}

# Add this helper function to fetch the specific funding source name
def get_bill_funding_source():
    try:
        headers = get_crew_headers()
        if not headers: return "Checking"

        query_string = """
        query CurrentUser {
            currentUser {
                accounts {
                    billReserve {
                        settings {
                            funding {
                                subaccount {
                                    displayName
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        response = requests.post(URL, headers=headers, json={
            "operationName": "CurrentUser",
            "query": query_string
        })

        data = response.json()
        
        # Parse logic to find the active billReserve
        # We ignore 'errors' regarding nullables and just look for valid data
        accounts = data.get("data", {}).get("currentUser", {}).get("accounts", [])
        
        for acc in accounts:
            # We look for the first account that has a non-null billReserve
            if acc and acc.get("billReserve"):
                try:
                    return acc["billReserve"]["settings"]["funding"]["subaccount"]["displayName"]
                except (KeyError, TypeError):
                    continue
                    
        return "Checking" # Default fallback
    except Exception:
        return "Checking"


def set_spend_pocket_action(user_id, pocket_id):
    try:
        # --- FIX START: Resolve "Checking" to a real ID ---
        if pocket_id == "Checking":
            # Fetch the list of subaccounts to find the ID for "Checking"
            all_subs = get_subaccounts_list()
            
            if "error" in all_subs:
                return {"error": "Could not resolve Checking ID"}
                
            found_id = None
            for sub in all_subs.get("subaccounts", []):
                if sub["name"] == "Checking":
                    found_id = sub["id"]
                    break
            
            if found_id:
                pocket_id = found_id
            else:
                return {"error": "Checking subaccount not found"}
        # --- FIX END ---

        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}

        query_string = """
        mutation SetActiveSpendPocketScottie($input: SetSpendSubaccountInput!) {
          setSpendSubaccount(input: $input) {
            result {
              id
              userSpendConfig {
                id
                selectedSpendSubaccount {
                  id
                  clearedBalance
                  __typename
                }
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """

        variables = {
            "input": {
                "userId": user_id,
                "selectedSpendSubaccountId": pocket_id
            }
        }

        response = requests.post(URL, headers=headers, json={
            "operationName": "SetActiveSpendPocketScottie",
            "variables": variables,
            "query": query_string
        })

        data = response.json()

        if 'errors' in data:
            return {"error": data['errors'][0]['message']}

        print("🧹 Clearing Cache after spend pocket update...")
        cache.clear()

        return {"success": True, "result": data.get("data", {}).get("setSpendSubaccount", {}).get("result")}

    except Exception as e:
        return {"error": str(e)}

# Update the main action to use the helper
def create_bill_action(name, amount, frequency_key, day_of_month, match_string=None, min_amt=None, max_amt=None, is_variable=False):
    try:
        headers = get_crew_headers()
        if not headers: return {"error": "Credentials not found"}
        
        account_id = get_primary_account_id()
        if not account_id: return {"error": "Main Account ID not found"}

        # --- 1. Map Frequency & Interval ---
        freq_map = {
            "WEEKLY":        ("WEEKLY", 1),
            "BIWEEKLY":      ("WEEKLY", 2),
            "MONTHLY":       ("MONTHLY", 1),
            "QUARTERLY":     ("MONTHLY", 3),
            "SEMI_ANNUALLY": ("MONTHLY", 6),
            "ANNUALLY":      ("YEARLY", 1)
        }
        
        if frequency_key not in freq_map:
            return {"error": "Invalid frequency selected"}
            
        final_freq, final_interval = freq_map[frequency_key]

        # --- 2. Calculate Anchor Date ---
        today = date.today()
        last_day_prev_month = today.replace(day=1) - timedelta(days=1)
        try:
            anchor_date_obj = last_day_prev_month.replace(day=int(day_of_month))
        except ValueError:
            anchor_date_obj = last_day_prev_month
            
        anchor_date_str = anchor_date_obj.strftime("%Y-%m-%d")

        # --- 3. Build Reassignment Rule ---
        reassignment_rule = None
        if match_string:
            rule = {"match": match_string}
            if min_amt: rule["minAmount"] = int(float(min_amt) * 100)
            if max_amt: rule["maxAmount"] = int(float(max_amt) * 100)
            reassignment_rule = rule

        # --- 4. Mutation (Simplified, as we fetch name separately now) ---
        query_string = """
        mutation CreateBill($input: CreateBillInput!) {
            createBill(input: $input) {
                result {
                    id
                    name
                    status
                    amount
                    reservedAmount
                }
            }
        }
        """
        
        variables = {
            "input": {
                "accountId": account_id,
                "amount": int(float(amount) * 100),
                "anchorDate": anchor_date_str,
                "frequency": final_freq,
                "frequencyInterval": final_interval,
                "autoAdjustAmount": is_variable,
                "paused": False,
                "name": name,
                "reassignmentRule": reassignment_rule
            }
        }

        response = requests.post(URL, headers=headers, json={
            "operationName": "CreateBill",
            "variables": variables,
            "query": query_string
        })

        data = response.json()
        
        if 'errors' in data:
            return {"error": data['errors'][0]['message']}
            
        print("🧹 Clearing Cache after bill creation...")
        cache.clear()
        
        # --- 5. Fetch Funding Name & Combine ---
        result = data.get("data", {}).get("createBill", {}).get("result", {})
        
        # Fetch the name from the separate query you provided
        funding_name = get_bill_funding_source()
        
        # Inject it into the result for the frontend
        result['fundingDisplayName'] = funding_name
        
        return {"success": True, "result": result}

    except Exception as e:
        return {"error": str(e)}

# --- ROUTES ---
@app.route('/')
def index(): return render_template('index.html')

# --- PWA ROUTES ---
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

# --- API ROUTES ---
@app.route('/api/family')
def api_family(): return jsonify(get_family_data())
@app.route('/api/cards')
def api_cards():
    # Allow forcing a refresh if ?refresh=true is passed
    refresh = request.args.get('refresh') == 'true'
    return jsonify(get_cards_data(force_refresh=refresh))

# 3. CREATE THE MISSING MOVE/REORDER ENDPOINT
@app.route('/api/groups/move-pocket', methods=['POST'])
def api_move_pocket():
    data = request.json
    
    # We expect: 
    # 1. targetGroupId (where it's going)
    # 2. orderedPocketIds (the full list of pocket IDs in that group, in order)
    
    target_group_id = data.get('targetGroupId') # Can be None (Ungrouped)
    ordered_ids = data.get('orderedPocketIds', [])
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        # Loop through the list provided by frontend and update both Group and Order
        for index, pocket_id in enumerate(ordered_ids):
            if target_group_id is None:
                # If ungrouped, we delete the link (or set group_id NULL if you prefer)
                # But to keep sorting in "Ungrouped" area, let's keep the row with NULL group_id
                # Check if exists
                c.execute("INSERT OR REPLACE INTO pocket_links (pocket_id, group_id, sort_order) VALUES (?, NULL, ?)", (pocket_id, index))
            else:
                c.execute("INSERT OR REPLACE INTO pocket_links (pocket_id, group_id, sort_order) VALUES (?, ?, ?)", (pocket_id, target_group_id, index))
        
        conn.commit()
        cache.clear()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()

@app.route('/api/groups/manage', methods=['POST'])
def api_manage_group():
    # Handles Create and Update
    data = request.json
    group_id = data.get('id') # None if creating
    name = data.get('name')
    pocket_ids = data.get('pockets', []) # List of pocket IDs to assign
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        if not group_id:
            # CREATE
            c.execute("INSERT INTO groups (name) VALUES (?)", (name,))
            group_id = c.lastrowid
        else:
            # UPDATE NAME
            c.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))
            
        # UPDATE POCKET LINKS
        # 1. Remove all pockets currently assigned to this group (to handle unchecking)
        c.execute("DELETE FROM pocket_links WHERE group_id = ?", (group_id,))
        
        # 2. Assign selected pockets (Moving them from other groups if necessary)
        for pid in pocket_ids:
            # Remove from any other group first (implicit via REPLACE if we used that, but safer to delete old link)
            c.execute("DELETE FROM pocket_links WHERE pocket_id = ?", (pid,))
            c.execute("INSERT INTO pocket_links (pocket_id, group_id) VALUES (?, ?)", (pid, group_id))
            
        conn.commit()
        cache.clear()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()

@app.route('/api/groups/delete', methods=['POST'])
def api_delete_group():
    data = request.json
    group_id = data.get('id')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        # Delete Group
        c.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        # Unlink pockets (they become ungrouped)
        c.execute("DELETE FROM pocket_links WHERE group_id = ?", (group_id,))
        conn.commit()
        cache.clear()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()

# --- NEW API ROUTE: Assign Group ---
@app.route('/api/assign-group', methods=['POST'])
def api_assign_group():
    data = request.json
    pocket_id = data.get('pocketId')
    group_name = data.get('groupName') # If empty string, we treat as ungroup
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        if not group_name or group_name.strip() == "":
            c.execute("DELETE FROM pocket_groups WHERE pocket_id = ?", (pocket_id,))
        else:
            c.execute("INSERT OR REPLACE INTO pocket_groups (pocket_id, group_name) VALUES (?, ?)", (pocket_id, group_name))
        conn.commit()
        
        # Clear cache to force UI update
        cache.clear()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)})
    finally:
        conn.close()

@app.route('/api/set-card-spend', methods=['POST'])
def api_set_card_spend():
    data = request.json
    return jsonify(set_spend_pocket_action(
        data.get('userId'),
        data.get('pocketId')
    ))

@app.route('/api/savings')
def api_savings():
    # Check if the frontend is asking for a forced refresh
    refresh = request.args.get('refresh') == 'true'
    return jsonify(get_financial_data(force_refresh=refresh))

@app.route('/api/history')
def api_history(): return jsonify(get_history())
@app.route('/api/transactions')
def api_transactions():
    q = request.args.get('q')
    min_date = request.args.get('minDate')
    max_date = request.args.get('maxDate')
    min_amt = request.args.get('minAmt')
    max_amt = request.args.get('maxAmt')
    
    # Get regular transactions
    result = get_transactions_data(q, min_date, max_date, min_amt, max_amt)
    
    # Get credit card transactions
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT transaction_id, amount, date, merchant, description, is_pending, created_at
                     FROM credit_card_transactions
                     ORDER BY date DESC, created_at DESC""")
        rows = c.fetchall()
        conn.close()
        
        credit_card_txs = []
        for row in rows:
            tx_date = row[2]  # date field
            amount = row[1]  # amount (already in dollars)
            
            # Apply filters if provided
            if min_date and tx_date < min_date:
                continue
            if max_date and tx_date > max_date:
                continue
            if min_amt and abs(amount) < float(min_amt):
                continue
            if max_amt and abs(amount) > float(max_amt):
                continue
            if q and q.lower() not in (row[3] or "").lower() and q.lower() not in (row[4] or "").lower():
                continue
            
            # Format as Crew transaction format
            credit_card_txs.append({
                "id": f"cc_{row[0]}",  # Prefix to avoid conflicts
                "title": row[3] or row[4] or "Credit Card Transaction",
                "description": row[4] or "",
                "amount": -abs(amount),  # Negative for expenses
                "date": tx_date,
                "type": "DEBIT",
                "subaccountId": None,
                "isCreditCard": True,
                "merchant": row[3],
                "isPending": bool(row[5])
            })
        
        # Merge and sort by date
        if "transactions" in result:
            all_txs = result["transactions"] + credit_card_txs
            all_txs.sort(key=lambda x: x.get("date", ""), reverse=True)
            result["transactions"] = all_txs
    
    except Exception as e:
        print(f"Error loading credit card transactions: {e}")
    
    return jsonify(result)
@app.route('/api/transaction/<path:tx_id>')
def api_transaction_detail(tx_id): return jsonify(get_transaction_detail(tx_id))

@app.route('/api/expenses')
def api_expenses():
    refresh = request.args.get('refresh') == 'true'
    return jsonify(get_expenses_data(force_refresh=refresh))

@app.route('/api/goals')
def api_goals():
    refresh = request.args.get('refresh') == 'true'
    return jsonify(get_goals_data(force_refresh=refresh))

@app.route('/api/trends')
def api_trends(): return jsonify(get_monthly_trends())

@app.route('/api/subaccounts')
def api_subaccounts():
    refresh = request.args.get('refresh') == 'true'
    return jsonify(get_subaccounts_list(force_refresh=refresh))

@app.route('/api/move-money', methods=['POST'])
def api_move_money():
    data = request.json
    return jsonify(move_money(data.get('fromId'), data.get('toId'), data.get('amount'), data.get('note')))

@app.route('/api/delete-pocket', methods=['POST'])
def api_delete_pocket():
    data = request.json
    return jsonify(delete_subaccount_action(data.get('id')))


@app.route('/api/create-pocket', methods=['POST'])
def api_create_pocket():
    data = request.json
    result = create_pocket(
        data.get('name'), 
        data.get('amount'), 
        data.get('initial'), 
        data.get('note')
    )
    
    # If pocket creation was successful and groupId is provided, assign to group
    if result.get('success') and data.get('groupId'):
        pocket_id = result['result']['id']
        group_id = data.get('groupId')
        
        # Assign to group in database
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("INSERT OR REPLACE INTO pocket_links (pocket_id, group_id, sort_order) VALUES (?, ?, ?)", 
                     (pocket_id, group_id, 0))
            conn.commit()
        except Exception as e:
            print(f"Warning: Failed to assign pocket to group: {e}")
        finally:
            conn.close()
    
    return jsonify(result)

@app.route('/api/delete-bill', methods=['POST'])
def api_delete_bill():
    data = request.json
    return jsonify(delete_bill_action(data.get('id')))


@app.route('/api/create-bill', methods=['POST'])
def api_create_bill():
    data = request.json
    return jsonify(create_bill_action(
        data.get('name'),
        data.get('amount'),
        data.get('frequency'),
        data.get('dayOfMonth'),
        data.get('matchString'),
        data.get('minAmount'),
        data.get('maxAmount'),
        data.get('variable')
    ))

@app.route('/api/user')
def api_user():
    return jsonify(get_user_profile_info())

@app.route('/api/intercom')
def api_intercom():
    return jsonify(get_intercom_data())

# --- LUNCHFLOW API ENDPOINTS ---
@app.route('/api/lunchflow/accounts')
def api_lunchflow_accounts():
    """List all accounts from LunchFlow"""
    api_key = os.environ.get("LUNCHFLOW_API_KEY")
    if not api_key:
        return jsonify({"error": "LunchFlow API key not configured. Please set LUNCHFLOW_API_KEY in docker-compose.yml"}), 400
    
    try:
        headers = {
            "x-api-key": api_key,
            "accept": "application/json"
        }
        # Use www.lunchflow.app as per documentation
        response = requests.get("https://www.lunchflow.app/api/v1/accounts", headers=headers, timeout=30)
        
        if response.status_code != 200:
            return jsonify({"error": f"LunchFlow API error: {response.status_code} - {response.text}"}), response.status_code
        
        data = response.json()
        # Return the data in the expected format with accounts array
        return jsonify(data)
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": f"Connection error: Unable to connect to LunchFlow API. Please check your internet connection and try again. ({str(e)})"}), 500
    except requests.exceptions.Timeout as e:
        return jsonify({"error": f"Request timeout: LunchFlow API took too long to respond. ({str(e)})"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Request error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

@app.route('/api/lunchflow/set-credit-card', methods=['POST'])
def api_set_credit_card():
    """Store the selected credit card account ID (without creating pocket yet)"""
    data = request.json
    account_id = data.get('accountId')
    account_name = data.get('accountName', '')
    
    if not account_id:
        return jsonify({"error": "accountId is required"}), 400
    
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Store the account info without pocket_id yet (pocket will be created after balance sync decision)
        c.execute("INSERT OR REPLACE INTO credit_card_config (account_id, account_name, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)", 
                  (account_id, account_name))
        conn.commit()
        conn.close()
        
        cache.clear()
        return jsonify({"success": True, "message": "Credit card account saved", "needsBalanceSync": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lunchflow/get-balance/<account_id>')
def api_get_balance(account_id):
    """Get the balance for a specific LunchFlow account"""
    api_key = os.environ.get("LUNCHFLOW_API_KEY")
    if not api_key:
        return jsonify({"error": "LunchFlow API key not configured"}), 400
    
    try:
        headers = {
            "x-api-key": api_key,
            "accept": "application/json"
        }
        response = requests.get(f"https://www.lunchflow.app/api/v1/accounts/{account_id}/balance", headers=headers, timeout=30)
        
        if response.status_code != 200:
            return jsonify({"error": f"LunchFlow API error: {response.status_code} - {response.text}"}), response.status_code
        
        data = response.json()
        return jsonify(data)
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": f"Connection error: {str(e)}"}), 500
    except requests.exceptions.Timeout as e:
        return jsonify({"error": f"Request timeout: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lunchflow/create-pocket-with-balance', methods=['POST'])
def api_create_pocket_with_balance():
    """Create the credit card pocket and optionally sync balance"""
    data = request.json
    account_id = data.get('accountId')
    sync_balance = data.get('syncBalance', False)
    
    if not account_id:
        return jsonify({"error": "accountId is required"}), 400
    
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get account name
        c.execute("SELECT account_name FROM credit_card_config WHERE account_id = ?", (account_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Account not found. Please select an account first."}), 400
        
        account_name = row[0]
        
        # Get current balance from LunchFlow if sync requested
        initial_amount = "0"
        if sync_balance:
            api_key = os.environ.get("LUNCHFLOW_API_KEY")
            if api_key:
                try:
                    headers = {"x-api-key": api_key, "accept": "application/json"}
                    response = requests.get(f"https://www.lunchflow.app/api/v1/accounts/{account_id}/balance", headers=headers, timeout=30)
                    if response.status_code == 200:
                        balance_data = response.json()
                        # Balance is already in dollars
                        balance_amount = balance_data.get("balance", {}).get("amount", 0)
                        initial_amount = str(abs(balance_amount))  # Use absolute value
                except Exception as e:
                    print(f"Warning: Could not fetch balance: {e}")
        
        # Create the pocket
        pocket_name = f"Credit Card - {account_name}"
        pocket_result = create_pocket(pocket_name, "0", initial_amount, f"Credit card tracking pocket for {account_name}")
        
        if "error" in pocket_result:
            conn.close()
            return jsonify({"error": f"Failed to create pocket: {pocket_result['error']}"}), 500
        
        pocket_id = pocket_result.get("result", {}).get("id")
        if not pocket_id:
            conn.close()
            return jsonify({"error": "Pocket was created but no ID was returned"}), 500
        
        # Update the config with pocket_id
        c.execute("UPDATE credit_card_config SET pocket_id = ? WHERE account_id = ?", (pocket_id, account_id))
        conn.commit()
        conn.close()
        
        cache.clear()
        return jsonify({"success": True, "message": "Credit card pocket created", "pocketId": pocket_id, "syncedBalance": sync_balance})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lunchflow/credit-card-status')
def api_credit_card_status():
    """Get the current credit card account configuration"""
    api_key = os.environ.get("LUNCHFLOW_API_KEY")
    
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT account_id, account_name, pocket_id, created_at FROM credit_card_config LIMIT 1")
        row = c.fetchone()
        conn.close()
        
        result = {
            "hasApiKey": bool(api_key),
            "configured": False,
            "pocketCreated": False,
            "accountId": None,
            "accountName": None,
            "pocketId": None,
            "createdAt": None
        }
        
        if row:
            result["configured"] = True
            result["accountId"] = row[0]
            result["accountName"] = row[1]
            result["pocketId"] = row[2]
            result["pocketCreated"] = bool(row[2])
            result["createdAt"] = row[3]
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lunchflow/sync-balance', methods=['POST'])
def api_sync_balance():
    """Sync the pocket balance to match the credit card balance"""
    data = request.json
    account_id = data.get('accountId')
    
    if not account_id:
        return jsonify({"error": "accountId is required"}), 400
    
    try:
        # Get pocket_id from database
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT pocket_id FROM credit_card_config WHERE account_id = ?", (account_id,))
        row = c.fetchone()
        conn.close()
        
        if not row or not row[0]:
            return jsonify({"error": "No pocket found for this account"}), 400
        
        pocket_id = row[0]
        
        # Get balance from LunchFlow
        api_key = os.environ.get("LUNCHFLOW_API_KEY")
        if not api_key:
            return jsonify({"error": "LunchFlow API key not configured"}), 400
        
        headers = {"x-api-key": api_key, "accept": "application/json"}
        response = requests.get(f"https://www.lunchflow.app/api/v1/accounts/{account_id}/balance", headers=headers, timeout=30)
        
        if response.status_code != 200:
            return jsonify({"error": f"Failed to get balance: {response.status_code}"}), response.status_code
        
        balance_data = response.json()
        # Balance is already in dollars
        balance_amount = balance_data.get("balance", {}).get("amount", 0)
        target_balance = abs(balance_amount)
        
        # Get current pocket balance
        headers_crew = get_crew_headers()
        if not headers_crew:
            return jsonify({"error": "Crew credentials not found"}), 400
        
        query_string = """query GetSubaccount($id: ID!) { node(id: $id) { ... on Subaccount { id overallBalance } } }"""
        response_crew = requests.post(URL, headers=headers_crew, json={
            "operationName": "GetSubaccount",
            "variables": {"id": pocket_id},
            "query": query_string
        })
        
        crew_data = response_crew.json()
        current_balance = 0
        try:
            current_balance = crew_data.get("data", {}).get("node", {}).get("overallBalance", 0) / 100.0
        except:
            pass
        
        # Calculate difference
        difference = target_balance - current_balance
        
        # Get Checking subaccount ID (not Account ID)
        all_subs = get_subaccounts_list()
        if "error" in all_subs:
            return jsonify({"error": "Could not get subaccounts list"}), 400
        
        checking_subaccount_id = None
        for sub in all_subs.get("subaccounts", []):
            if sub["name"] == "Checking":
                checking_subaccount_id = sub["id"]
                break
        
        if not checking_subaccount_id:
            return jsonify({"error": "Could not find Checking subaccount"}), 400
        
        # Transfer money to/from pocket
        if abs(difference) > 0.01:  # Only transfer if difference is significant
            if difference > 0:
                # Need to move money from Checking to Pocket
                result = move_money(checking_subaccount_id, pocket_id, str(difference), f"Sync credit card balance")
            else:
                # Need to move money from Pocket to Checking
                result = move_money(pocket_id, checking_subaccount_id, str(abs(difference)), f"Sync credit card balance")
            
            if "error" in result:
                return jsonify({"error": f"Failed to sync balance: {result['error']}"}), 500
        
        cache.clear()
        return jsonify({"success": True, "message": "Balance synced", "targetBalance": target_balance, "previousBalance": current_balance})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lunchflow/change-account', methods=['POST'])
def api_change_account():
    """Delete the credit card pocket, return money to safe-to-spend, and clear config"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get current config - find any configured account with a pocket
        c.execute("SELECT account_id, pocket_id FROM credit_card_config WHERE pocket_id IS NOT NULL LIMIT 1")
        row = c.fetchone()
        
        if not row:
            # Check if there's any config at all (even without pocket)
            c.execute("SELECT account_id, pocket_id FROM credit_card_config LIMIT 1")
            row = c.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "No credit card account configured"}), 400
            # Get account_id even if pocket_id is NULL
            account_id = row[0]
            pocket_id = row[1] if len(row) > 1 else None
        else:
            account_id, pocket_id = row[0], row[1]
        
        # Get current pocket balance and return it to Checking
        headers_crew = get_crew_headers()
        if headers_crew and pocket_id:
            try:
                query_string = """query GetSubaccount($id: ID!) { node(id: $id) { ... on Subaccount { id overallBalance } } }"""
                response_crew = requests.post(URL, headers=headers_crew, json={
                    "operationName": "GetSubaccount",
                    "variables": {"id": pocket_id},
                    "query": query_string
                })
                
                crew_data = response_crew.json()
                current_balance = 0
                try:
                    current_balance = crew_data.get("data", {}).get("node", {}).get("overallBalance", 0) / 100.0
                except:
                    pass
                
                # Return money to Checking if there's a balance
                all_subs = get_subaccounts_list()
                if "error" not in all_subs:
                    checking_subaccount_id = None
                    for sub in all_subs.get("subaccounts", []):
                        if sub["name"] == "Checking":
                            checking_subaccount_id = sub["id"]
                            break
                    
                    if checking_subaccount_id and current_balance > 0.01:
                        move_money(pocket_id, checking_subaccount_id, str(current_balance), "Returning credit card pocket funds to Safe-to-Spend")
                
                # Delete the pocket
                delete_subaccount_action(pocket_id)
            except Exception as e:
                print(f"Warning: Error deleting pocket: {e}")
        
        # Delete ALL config rows for this account and transaction history (user will select a new account)
        # Delete all rows regardless of pocket_id status to ensure clean state
        c.execute("DELETE FROM credit_card_config WHERE account_id = ?", (account_id,))
        c.execute("DELETE FROM credit_card_transactions WHERE account_id = ?", (account_id,))
        conn.commit()
        conn.close()
        
        cache.clear()
        return jsonify({"success": True, "message": "Account changed. Pocket deleted and funds returned to Safe-to-Spend."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lunchflow/stop-tracking', methods=['POST'])
def api_stop_tracking():
    """Delete the credit card pocket, return money to safe-to-spend, and delete all config"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get current config
        c.execute("SELECT account_id, pocket_id FROM credit_card_config WHERE pocket_id IS NOT NULL LIMIT 1")
        row = c.fetchone()
        
        if not row:
            conn.close()
            return jsonify({"error": "No credit card account configured"}), 400
        
        account_id, pocket_id = row[0], row[1]
        
        # Get current pocket balance and return it to Checking
        headers_crew = get_crew_headers()
        if headers_crew and pocket_id:
            try:
                query_string = """query GetSubaccount($id: ID!) { node(id: $id) { ... on Subaccount { id overallBalance } } }"""
                response_crew = requests.post(URL, headers=headers_crew, json={
                    "operationName": "GetSubaccount",
                    "variables": {"id": pocket_id},
                    "query": query_string
                })
                
                crew_data = response_crew.json()
                current_balance = 0
                try:
                    current_balance = crew_data.get("data", {}).get("node", {}).get("overallBalance", 0) / 100.0
                except:
                    pass
                
                # Return money to Checking if there's a balance
                all_subs = get_subaccounts_list()
                if "error" not in all_subs:
                    checking_subaccount_id = None
                    for sub in all_subs.get("subaccounts", []):
                        if sub["name"] == "Checking":
                            checking_subaccount_id = sub["id"]
                            break
                    
                    if checking_subaccount_id and current_balance > 0.01:
                        move_money(pocket_id, checking_subaccount_id, str(current_balance), "Returning credit card pocket funds to Safe-to-Spend")
                
                # Delete the pocket
                delete_subaccount_action(pocket_id)
            except Exception as e:
                print(f"Warning: Error deleting pocket: {e}")
        
        # Delete all credit card config and transactions
        c.execute("DELETE FROM credit_card_config WHERE account_id = ?", (account_id,))
        c.execute("DELETE FROM credit_card_transactions WHERE account_id = ?", (account_id,))
        conn.commit()
        conn.close()
        
        cache.clear()
        return jsonify({"success": True, "message": "Tracking stopped. Pocket deleted and funds returned to Safe-to-Spend."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- CREDIT CARD TRANSACTION SYNCING ---
def check_credit_card_transactions():
    """Check for new credit card transactions and update balance"""
    try:
        api_key = os.environ.get("LUNCHFLOW_API_KEY")
        if not api_key:
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get credit card account config
        c.execute("SELECT account_id, pocket_id FROM credit_card_config WHERE pocket_id IS NOT NULL LIMIT 1")
        row = c.fetchone()
        
        if not row:
            conn.close()
            return
        
        account_id, pocket_id = row
        
        # Get when credit card was added (only get transactions after this date)
        c.execute("SELECT created_at FROM credit_card_config WHERE account_id = ?", (account_id,))
        config_row = c.fetchone()
        added_date = config_row[0] if config_row else None
        
        # Fetch transactions from LunchFlow
        headers = {"x-api-key": api_key, "accept": "application/json"}
        # Try both URL formats
        try:
            response = requests.get(f"https://www.lunchflow.app/api/v1/accounts/{account_id}/transactions", headers=headers, timeout=30)
        except:
            response = requests.get(f"https://lunchflow.com/api/v1/accounts/{account_id}/transactions", headers=headers, timeout=30)
        
        if response.status_code != 200:
            conn.close()
            return
        
        data = response.json()
        transactions = data.get("transactions", [])
        
        # Get list of already seen transaction IDs
        c.execute("SELECT transaction_id FROM credit_card_transactions WHERE account_id = ?", (account_id,))
        seen_ids = {row[0] for row in c.fetchall()}
        
        # TESTING MODE: Allow all past transactions (not just those after credit card was added)
        # TODO: Revert this for production - uncomment the date filter below
        # Filter transactions to only include those after credit card was added
        # if added_date:
        #     try:
        #         added_datetime = datetime.fromisoformat(added_date.replace('Z', '+00:00'))
        #         transactions = [tx for tx in transactions if tx.get("date") and datetime.fromisoformat(tx["date"].replace('Z', '+00:00')) >= added_datetime]
        #     except:
        #         pass  # If date parsing fails, include all transactions
        
        new_transactions = []
        total_amount = 0.0
        
        for tx in transactions:
            tx_id = tx.get("id")
            if not tx_id or tx_id in seen_ids:
                continue
            
            # Store new transaction
            amount = tx.get("amount", 0)  # LunchFlow already returns dollars, not cents
            total_amount += amount
            
            # Use INSERT OR IGNORE to prevent duplicates at database level (safety net)
            c.execute("""INSERT OR IGNORE INTO credit_card_transactions 
                         (transaction_id, account_id, amount, date, merchant, description, is_pending)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (tx_id, account_id, amount, tx.get("date"), tx.get("merchant"), 
                      tx.get("description"), 1 if tx.get("isPending") else 0))
            
            # Only add to new_transactions if the row was actually inserted
            if c.rowcount > 0:
                new_transactions.append(tx)
        
        conn.commit()
        
        # Always update the pocket balance (every 30 seconds), not just when there are new transactions
        if pocket_id:
            # Get current balance from LunchFlow
            balance_headers = {"x-api-key": api_key, "accept": "application/json"}
            balance_response = requests.get(f"https://www.lunchflow.app/api/v1/accounts/{account_id}/balance", headers=balance_headers, timeout=30)
            if balance_response.status_code == 200:
                balance_data = balance_response.json()
                # Balance is already in dollars
                balance_amount = balance_data.get("balance", {}).get("amount", 0)
                target_balance = abs(balance_amount)
                
                # Get current pocket balance
                headers_crew = get_crew_headers()
                if headers_crew:
                    query_string = """query GetSubaccount($id: ID!) { node(id: $id) { ... on Subaccount { id overallBalance } } }"""
                    response_crew = requests.post(URL, headers=headers_crew, json={
                        "operationName": "GetSubaccount",
                        "variables": {"id": pocket_id},
                        "query": query_string
                    })
                    
                    crew_data = response_crew.json()
                    current_balance = 0
                    try:
                        current_balance = crew_data.get("data", {}).get("node", {}).get("overallBalance", 0) / 100.0
                    except:
                        pass
                    
                    # Calculate difference and move money
                    difference = target_balance - current_balance
                    all_subs = get_subaccounts_list()
                    if "error" not in all_subs:
                        checking_subaccount_id = None
                        for sub in all_subs.get("subaccounts", []):
                            if sub["name"] == "Checking":
                                checking_subaccount_id = sub["id"]
                                break
                        
                        if checking_subaccount_id and abs(difference) > 0.01:
                            if difference > 0:
                                move_money(checking_subaccount_id, pocket_id, str(difference), f"Credit card transaction sync")
                            else:
                                move_money(pocket_id, checking_subaccount_id, str(abs(difference)), f"Credit card transaction sync")
                    
                    cache.clear()
        
        conn.close()
        
        if new_transactions:
            print(f"✅ Found {len(new_transactions)} new credit card transactions")
        else:
            print(f"🔄 Credit card balance checked (no new transactions)")
        
    except Exception as e:
        print(f"Error checking credit card transactions: {e}")

@app.route('/api/lunchflow/last-check-time')
def api_last_check_time():
    """Get the last time credit card transactions were checked"""
    try:
        # Store last check time in a simple way - we'll use a file or just return current time minus some offset
        # For now, return a timestamp that represents "30 seconds ago" so countdown starts at 30
        import time as time_module
        return jsonify({
            "lastCheckTime": time_module.time(),
            "checkInterval": 30  # seconds
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def background_transaction_checker():
    """Background thread that checks for new transactions every 30 seconds"""
    while True:
        try:
            check_credit_card_transactions()
        except Exception as e:
            print(f"Error in background transaction checker: {e}")
        time.sleep(30)  # Check every 30 seconds

@app.route('/api/lunchflow/transactions')
def api_get_credit_card_transactions():
    """Get credit card transactions that have been synced"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        c.execute("""SELECT transaction_id, amount, date, merchant, description, is_pending, created_at
                     FROM credit_card_transactions
                     ORDER BY date DESC, created_at DESC
                     LIMIT 100""")
        
        rows = c.fetchall()
        conn.close()
        
        transactions = []
        for row in rows:
            transactions.append({
                "id": row[0],
                "amount": row[1],
                "date": row[2],
                "merchant": row[3],
                "description": row[4],
                "isPending": bool(row[5]),
                "syncedAt": row[6],
                "isCreditCard": True  # Flag to identify credit card transactions
            })
        
        return jsonify({"transactions": transactions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    init_db()
    
    # Start background thread for transaction checking
    transaction_thread = threading.Thread(target=background_transaction_checker, daemon=True)
    transaction_thread.start()
    print("🔄 Credit card transaction checker started (checks every 30 seconds)")
    
    print("Server running on http://127.0.0.1:8080")
    app.run(host='0.0.0.0', debug=True, port=8080)
