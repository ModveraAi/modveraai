# --------------------------- Modvera AI - Single File App ---------------------------
import os
import stripe
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from lead_engine import router as lead_router
from automation_engine import router as automation_router, start_worker

app = FastAPI()
app.include_router(lead_router)
app.include_router(automation_router)
start_worker()

# ====== Environment (set these in Render → Settings → Environment) ======
PUBLIC_URL           = os.getenv("PUBLIC_URL", "https://modveraai.onrender.com")
BUSINESS_NAME        = os.getenv("BUSINESS_NAME", "Modvera AI")

# Stripe keys
STRIPE_PUBLISHABLE   = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_SECRET        = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET= os.getenv("STRIPE_WEBHOOK_SECRET", "")  # optional for now

# Prices (Stripe Price IDs)
PRICE_MONTHLY_ID     = os.getenv("PRICE_MONTHLY_ID", "")  # e.g. price_abc123
PRICE_ANNUAL_ID      = os.getenv("PRICE_ANNUAL_ID", "")   # e.g. price_def456

# Owner free access (auto-applies your 100% promo to owner email)
DEV_FREE             = os.getenv("DEV_FREE", "1") in ("1", "true", "True")
DEV_FREE_EMAIL       = os.getenv("DEV_FREE_EMAIL", "modverashop@gmail.com").lower()
DEV_PROMO_CODE_ID    = os.getenv("DEV_PROMO_CODE_ID", "")  # e.g. promo_abc123

# Configure Stripe
if not STRIPE_SECRET:
    print("WARNING: STRIPE_SECRET_KEY is not set.")
stripe.api_key = STRIPE_SECRET

# ====== Helpers ======
def has_active_subscription(email: str) -> bool:
    try:
        customers = stripe.Customer.search(query=f'email:"{email}"')
        for c in customers.auto_paging_iter():
            subs = stripe.Subscription.list(customer=c.id, status="all")
            for s in subs.auto_paging_iter():
                if s.status in ("active", "trialing", "past_due"):
                    return True
        return False
    except Exception:
        return False

# ====== Health & Test ======
@app.get("/health", response_class=JSONResponse)
def health():
    return {"ok": True}

@app.get("/test-checkout", response_class=JSONResponse)
def test_checkout(plan: str = "monthly", email: str = "modverashop@gmail.com"):
    price_id = PRICE_MONTHLY_ID if plan == "monthly" else PRICE_ANNUAL_ID
    return {
        "plan": plan,
        "email": email,
        "price_id": price_id,
        "have_secret": bool(STRIPE_SECRET),
        "public_url": PUBLIC_URL,
        "dev_free": DEV_FREE,
        "promo": bool(DEV_PROMO_CODE_ID),
    }

# ====== Checkout ======
@app.post("/create-checkout-session", response_class=JSONResponse)
async def create_checkout_session(req: Request):
    data = await req.json()
    plan = (data.get("plan") or "monthly").lower().strip()
    email = (data.get("email") or "").lower().strip()
    price_id = PRICE_MONTHLY_ID if plan == "monthly" else PRICE_ANNUAL_ID
    if not price_id:
        raise HTTPException(status_code=400, detail="Price not configured.")
    params = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{PUBLIC_URL}/auth/complete?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{PUBLIC_URL}/dashboard#Billing",
        "customer_email": email or None,
    }
    if DEV_FREE and DEV_PROMO_CODE_ID and email == DEV_FREE_EMAIL:
        params["discounts"] = [{"promotion_code": DEV_PROMO_CODE_ID}]
    else:
        params["allow_promotion_codes"] = True
    try:
        session = stripe.checkout.Session.create(**params)
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ====== Stripe Webhook ======
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"ok": True})
    try:
        stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")
    return JSONResponse({"received": True})

# ====== Auth helpers ======
@app.get("/whoami", response_class=JSONResponse)
def whoami(request: Request):
    return {"email": request.cookies.get("auth_email")}

@app.get("/logout")
def logout():
    resp = RedirectResponse(url=f"{PUBLIC_URL}/dashboard?logged_out=1", status_code=302)
    resp.delete_cookie("auth_email")
    return resp

@app.get("/unlock")
def unlock(email: str = None):
    email = (email or DEV_FREE_EMAIL or "owner@example.com").strip().lower()
    resp = RedirectResponse(url=f"{PUBLIC_URL}/dashboard?welcome=1", status_code=302)
    resp.set_cookie("auth_email", email, httponly=False, samesite="Lax", max_age=60*60*24*30)
    return resp

@app.get("/auth/complete")
def auth_complete(session_id: str):
    try:
        sess = stripe.checkout.Session.retrieve(session_id, expand=["customer", "customer_details"])
        email = (sess.get("customer_details") or {}).get("email") or (sess.get("customer") or {}).get("email")
        if not email:
            return RedirectResponse(url=f"{PUBLIC_URL}/dashboard#Billing", status_code=302)
        _ = has_active_subscription(email)
        resp = RedirectResponse(url=f"{PUBLIC_URL}/dashboard?welcome=1", status_code=302)
        resp.set_cookie("auth_email", email, httponly=False, samesite="Lax", max_age=60*60*24*30)
        return resp
    except Exception:
        return RedirectResponse(url=f"{PUBLIC_URL}/dashboard#Billing", status_code=302)

# ====== Dashboard ======
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    email = request.cookies.get("auth_email")
    unlocked = bool(email)
    status_text  = f"Signed in as {email}" if unlocked else "Locked"
    right_button = '<a class="btn" href="/logout">Log out</a>' if unlocked else '<a class="btn" href="/dashboard#Billing">Subscribe</a>'
    lock_class   = "" if unlocked else "locked"
    unlocked_js  = "true" if unlocked else "false"
    html = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{biz} — Dashboard</title>
<style>
body {{margin:0;background:#0b0b0f;color:#fff;font-family:Poppins,system-ui,sans-serif}}
.top {{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-bottom:1px solid #1e1e25;background:#0d0d12cc}}
.badge {{font-size:12px;padding:6px 10px;border-radius:12px;border:1px solid #222;background:#111}}
.grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;padding:20px}}
.card {{background:#141419;border:1px solid #222;border-radius:16px;padding:18px;min-height:120px;cursor:pointer;transition:.2s}}
.card:hover {{transform:translateY(-3px);border-color:#5c6efb;box-shadow:0 0 12px #5c6efb33}}
.locked {{opacity:.5;cursor:not-allowed}}
.btn {{color:#fff;text-decoration:none;border:1px solid #222;padding:8px 12px;border-radius:10px}}
h2 {{margin:20px 20px 6px}} p.sub {{margin:0 20px 10px;color:#9aa}}
.billing {{margin:20px;padding:16px;border:1px dashed #2a2a35;border-radius:12px}}
.billing button {{background:#1a1a22;color:#fff;border:1px solid #2a2a35;padding:10px 14px;border-radius:10px;margin-right:8px}}
</style></head><body>
<div class="top"><div><strong>{biz}</strong> <span class="badge">{status}</span></div><div>{right_btn}</div></div>
<h2>{headline}</h2><p class="sub">{subhead}</p>
<div class="grid">
<div class="card {lc}" data-path="/tools/ai-receptionist">AI Receptionist</div>
<div class="card {lc}" data-path="/tools/website-builder">Website Builder</div>
<div class="card {lc}" data-path="/tools/lead-generator">Lead Generator</div>
<div class="card {lc}" data-path="/tools/cold-call-trainer">Cold Call Trainer</div>
<div class="card {lc}" data-path="/tools/custom-gpts">Custom GPTs</div>
<div class="card {lc}" data-path="/tools/social-ads">Social Ads Creator</div>
</div>{billing}
<script>
const unlocked={unlocked_js};document.querySelectorAll('.card').forEach(card=>{{card.addEventListener('click',()=>{{const path=card.getAttribute('data-path');if(!unlocked){{alert('Please subscribe to unlock these tools.');location.href='/dashboard#Billing';return;}}location.href=path;}});}});
async function subscribe(plan){{const email=prompt('Enter your email to continue:');if(!email)return;const res=await fetch('/create-checkout-session',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan,email}})}});const text=await res.text();let data;try{{data=JSON.parse(text)}}catch(e){{}}if(res.ok&&data&&data.url)location.href=data.url;else alert('Checkout error:\\n'+(data?.detail||text||'Unknown'));}}
</script></body></html>
""".format(
        biz=BUSINESS_NAME,status=status_text,right_btn=right_button,
        headline=("Your Tools" if unlocked else "Unlock your tools"),
        subhead=("Click a card to open a tool." if unlocked else "Subscribe with your email to unlock all tools."),
        lc=lock_class,
        billing=("" if unlocked else f"""<div class="billing" id="Billing"><h3>Billing</h3><p>Choose Monthly or Annual to unlock all tools. Owner email <code>{DEV_FREE_EMAIL}</code> uses your 100% code automatically.</p><button onclick="subscribe('monthly')">Subscribe Monthly</button><button onclick="subscribe('annual')">Subscribe Annual</button></div>"""),
        unlocked_js=unlocked_js,
    )
    return HTMLResponse(html)

# ====== Generic placeholder for remaining tools ======
@app.get("/tools/{tool_name}", response_class=HTMLResponse)
def tool_page(tool_name: str, request: Request):
    email = request.cookies.get("auth_email")
    if not email:
        return RedirectResponse(url=f"{PUBLIC_URL}/dashboard#Billing", status_code=302)
    title = tool_name.replace("-", " ").title()
    return HTMLResponse(f"""<html><body style="background:#0b0b0f;color:#fff;font-family:Poppins,sans-serif"><div style="padding:18px;border-bottom:1px solid #222"><a href="/dashboard" style="color:#9aa;text-decoration:none;">&larr; Back</a><span style="float:right;color:#7ee787;">{email}</span></div><div style="padding:28px;"><h1>{title}</h1><p style="color:#aaa">Placeholder screen. Your functional UI for <b>{title}</b> goes here.</p></div></body></html>""")

# --------------------------- End Modvera AI - Single File App ---------------------------