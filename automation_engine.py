import email
import imaplib
import os
import re
import secrets
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from html import escape

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse

from lead_engine import db, now, require_owner

router = APIRouter()

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://modveraai.onrender.com").rstrip("/")
AUTOMATION_ENABLED = os.getenv("AUTOMATION_ENABLED", "0").lower() in ("1", "true", "yes")
AUTO_DEMO_ON_POSITIVE_REPLY = os.getenv("AUTO_DEMO_ON_POSITIVE_REPLY", "1").lower() in ("1", "true", "yes")
MAIL_POLL_SECONDS = max(60, int(os.getenv("MAIL_POLL_SECONDS", "300")))
OUTREACH_FROM_EMAIL = os.getenv("OUTREACH_FROM_EMAIL", "")
OUTREACH_FROM_NAME = os.getenv("OUTREACH_FROM_NAME", "Joe at Modvera")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "1").lower() in ("1", "true", "yes")
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", SMTP_USER)
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", SMTP_PASSWORD)

AUTO_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 lead_id INTEGER NOT NULL,
 recipient TEXT NOT NULL,
 subject TEXT NOT NULL,
 message_id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL DEFAULT 'initial',
 sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_replies (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 lead_id INTEGER,
 sender TEXT NOT NULL,
 subject TEXT DEFAULT '',
 body TEXT DEFAULT '',
 message_id TEXT UNIQUE,
 in_reply_to TEXT DEFAULT '',
 classification TEXT DEFAULT 'unknown',
 processed INTEGER DEFAULT 0,
 received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demos (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 lead_id INTEGER NOT NULL,
 token TEXT NOT NULL UNIQUE,
 html TEXT NOT NULL,
 created_at TEXT NOT NULL,
 emailed_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS suppressions (
 email TEXT PRIMARY KEY,
 reason TEXT DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_type TEXT NOT NULL,
 lead_id INTEGER,
 detail TEXT DEFAULT '',
 created_at TEXT NOT NULL
);
"""

def ensure_schema():
    conn = db(); conn.executescript(AUTO_SCHEMA); conn.commit(); conn.close()

ensure_schema()

POSITIVE_PATTERNS = [
    r"\byes\b", r"\bsure\b", r"send (it|that|one) over", r"send me", r"i'?d like to see",
    r"interested", r"show me", r"sounds good", r"take a look", r"let me see", r"go ahead",
    r"what would it look like", r"how much", r"pricing", r"price"
]
NEGATIVE_PATTERNS = [
    r"not interested", r"no thanks", r"no thank you", r"don'?t contact", r"do not contact",
    r"remove me", r"unsubscribe", r"stop emailing", r"take me off"
]

def log_event(event_type, lead_id=None, detail=""):
    conn = db(); conn.execute("INSERT INTO automation_events(event_type,lead_id,detail,created_at) VALUES(?,?,?,?)", (event_type,lead_id,detail[:1000],now())); conn.commit(); conn.close()

def classify_reply(text):
    t = (text or "").lower()
    if any(re.search(p, t) for p in NEGATIVE_PATTERNS): return "negative"
    if any(re.search(p, t) for p in POSITIVE_PATTERNS): return "positive"
    return "unknown"

def decode_mime(value):
    if not value: return ""
    out=[]
    for part, enc in decode_header(value):
        if isinstance(part, bytes): out.append(part.decode(enc or "utf-8", errors="ignore"))
        else: out.append(part)
    return "".join(out)

def get_plain_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                try: return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception: pass
        return ""
    try: return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception: return str(msg.get_payload() or "")

def smtp_ready():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and OUTREACH_FROM_EMAIL)

def imap_ready():
    return bool(IMAP_HOST and IMAP_USER and IMAP_PASSWORD)

def is_suppressed(address):
    conn=db(); row=conn.execute("SELECT email FROM suppressions WHERE lower(email)=lower(?)", (address,)).fetchone(); conn.close(); return bool(row)

def send_email(lead_id, recipient, subject, body, kind="initial", in_reply_to=""):
    if not smtp_ready(): raise RuntimeError("SMTP is not configured")
    if is_suppressed(recipient): raise RuntimeError("Recipient is suppressed")
    msg = EmailMessage()
    msg["From"] = f"{OUTREACH_FROM_NAME} <{OUTREACH_FROM_EMAIL}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    message_id = make_msgid(domain=(OUTREACH_FROM_EMAIL.split("@",1)[1] if "@" in OUTREACH_FROM_EMAIL else None))
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["List-Unsubscribe"] = f"<{PUBLIC_URL}/unsubscribe?email={recipient}>"
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        if SMTP_USE_TLS: s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    conn=db(); conn.execute("INSERT INTO sent_messages(lead_id,recipient,subject,message_id,kind,sent_at) VALUES(?,?,?,?,?,?)", (lead_id,recipient,subject,message_id,kind,now())); conn.execute("UPDATE leads SET status='CONTACTED',last_contacted=?,updated_at=? WHERE id=?", (now(),now(),lead_id)); conn.commit(); conn.close()
    log_event("email_sent", lead_id, kind)
    return message_id

def lead_for_reply(sender, in_reply_to, subject):
    conn=db(); row=None
    if in_reply_to:
        row=conn.execute("SELECT l.* FROM sent_messages s JOIN leads l ON l.id=s.lead_id WHERE s.message_id=? ORDER BY s.id DESC LIMIT 1", (in_reply_to.strip(),)).fetchone()
    if not row and sender:
        row=conn.execute("SELECT * FROM leads WHERE lower(email)=lower(?) ORDER BY updated_at DESC LIMIT 1", (sender,)).fetchone()
    if not row and subject:
        clean=re.sub(r"^(re|fw|fwd):\s*", "", subject, flags=re.I).strip()
        sm=conn.execute("SELECT lead_id FROM sent_messages WHERE subject=? ORDER BY id DESC LIMIT 1", (clean,)).fetchone()
        if sm: row=conn.execute("SELECT * FROM leads WHERE id=?", (sm["lead_id"],)).fetchone()
    conn.close(); return dict(row) if row else None

def demo_html(lead):
    name=escape(lead.get("business_name") or "Your Business")
    industry=escape(lead.get("industry") or "local business")
    city=escape(lead.get("city") or lead.get("address") or "your area")
    phone=escape(lead.get("phone") or "Call for a quote")
    reviews=int(lead.get("reviews") or 0)
    rating=float(lead.get("rating") or 0)
    social = f"Rated {rating:.1f}★ from {reviews} Google reviews" if rating and reviews else "Trusted local service"
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} — Concept Website</title><style>
*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,Arial,sans-serif;color:#171717;background:#fff}}.notice{{background:#111;color:#fff;text-align:center;padding:8px;font-size:12px}}.nav{{display:flex;justify-content:space-between;align-items:center;padding:20px 6vw;border-bottom:1px solid #eee}}.brand{{font-size:22px;font-weight:800}}.btn{{display:inline-block;background:#111;color:#fff;padding:13px 18px;border-radius:10px;text-decoration:none;font-weight:700}}.hero{{padding:90px 6vw;background:linear-gradient(135deg,#f7f7f7,#e9edf4)}}h1{{font-size:clamp(42px,7vw,76px);line-height:.98;max-width:900px;margin:0 0 22px}}.hero p{{font-size:20px;max-width:650px;color:#555}}.proof{{margin-top:22px;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;padding:60px 6vw}}.card{{padding:28px;border:1px solid #e5e5e5;border-radius:18px}}.cta{{margin:20px 6vw 70px;background:#111;color:white;border-radius:24px;padding:50px}}.muted{{color:#777}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><div class="notice">Concept demo prepared by Modvera — this is not the business's live website.</div><div class="nav"><div class="brand">{name}</div><a class="btn" href="tel:{phone}">Call Now</a></div><section class="hero"><div class="muted">{industry.title()} • {city}</div><h1>A cleaner website built to turn visitors into customers.</h1><p>A fast, mobile-friendly concept for {name}, designed around clear calls to action, trust, and easy contact.</p><div class="proof">{escape(social)}</div><p><a class="btn" href="#contact">Get a Free Quote</a></p></section><section class="grid"><div class="card"><h3>Clear Services</h3><p>Make it instantly obvious what {name} offers and where you work.</p></div><div class="card"><h3>Mobile First</h3><p>Designed for customers finding the business from Google on their phone.</p></div><div class="card"><h3>More Calls</h3><p>Strong quote and call buttons placed where customers actually need them.</p></div></section><section class="cta" id="contact"><h2>Ready to make this real?</h2><p>This concept can be customized with the business's real photos, services, branding, testimonials, and contact forms.</p><a class="btn" style="background:white;color:#111" href="mailto:{escape(lead.get('email') or '')}">Contact {name}</a></section></body></html>'''

def create_demo(lead_id):
    conn=db(); row=conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone(); conn.close()
    if not row: raise RuntimeError("Lead not found")
    lead=dict(row); token=secrets.token_urlsafe(18); html=demo_html(lead)
    conn=db(); conn.execute("INSERT INTO demos(lead_id,token,html,created_at) VALUES(?,?,?,?)", (lead_id,token,html,now())); conn.commit(); conn.close(); log_event("demo_created",lead_id,token)
    return token

def send_demo_for_reply(lead, inbound_message_id):
    token=create_demo(lead["id"]); url=f"{PUBLIC_URL}/demo/{token}"
    subject=f"Re: Website idea for {lead['business_name']}"
    body=f"Thanks for getting back to me. I put together a quick concept for {lead['business_name']} so you can see the direction I had in mind.\n\nDemo: {url}\n\nThis is just a concept, so I can change the colors, layout, wording, services, photos, and anything else before it becomes the real site.\n\nIf you like the direction, reply here and I can send you the next steps and pricing.\n\nJoe\nModvera"
    send_email(lead["id"],lead["email"],subject,body,"demo",inbound_message_id)
    conn=db(); conn.execute("UPDATE demos SET emailed_at=? WHERE token=?", (now(),token)); conn.execute("UPDATE leads SET status='INTERESTED',updated_at=? WHERE id=?", (now(),lead["id"])); conn.commit(); conn.close(); return url

def process_incoming_message(msg):
    sender=parseaddr(msg.get("From") or "")[1].lower(); subject=decode_mime(msg.get("Subject")); message_id=(msg.get("Message-ID") or "").strip(); in_reply_to=(msg.get("In-Reply-To") or "").strip(); body=get_plain_body(msg)
    if not sender or sender.lower()==OUTREACH_FROM_EMAIL.lower(): return
    lead=lead_for_reply(sender,in_reply_to,subject)
    if not lead: return
    classification=classify_reply(body)
    conn=db()
    try: conn.execute("INSERT INTO inbound_replies(lead_id,sender,subject,body,message_id,in_reply_to,classification,received_at) VALUES(?,?,?,?,?,?,?,?)", (lead["id"],sender,subject,body[:12000],message_id or None,in_reply_to,classification,now()))
    except Exception: conn.close(); return
    if classification=="negative":
        conn.execute("INSERT OR REPLACE INTO suppressions(email,reason,created_at) VALUES(?,?,?)", (sender,"negative reply / opt out",now())); conn.execute("UPDATE leads SET status='LOST',updated_at=? WHERE id=?",(now(),lead["id"])); conn.commit(); conn.close(); log_event("reply_negative",lead["id"],subject); return
    conn.execute("UPDATE leads SET status=?,updated_at=? WHERE id=?", ("INTERESTED" if classification=="positive" else "REPLIED",now(),lead["id"])); conn.commit(); conn.close(); log_event("reply_received",lead["id"],classification)
    if classification=="positive" and AUTO_DEMO_ON_POSITIVE_REPLY:
        try:
            send_demo_for_reply(lead,message_id)
            conn=db(); conn.execute("UPDATE inbound_replies SET processed=1 WHERE message_id=?", (message_id,)); conn.commit(); conn.close()
        except Exception as e: log_event("demo_send_error",lead["id"],str(e))

def poll_mail_once():
    if not imap_ready(): return {"checked":0,"configured":False}
    checked=0
    with imaplib.IMAP4_SSL(IMAP_HOST,IMAP_PORT) as m:
        m.login(IMAP_USER,IMAP_PASSWORD); m.select("INBOX")
        status,data=m.search(None,'UNSEEN')
        if status!="OK": return {"checked":0,"configured":True}
        for num in data[0].split()[-100:]:
            status,msg_data=m.fetch(num,'(RFC822)')
            if status!="OK" or not msg_data: continue
            raw=next((x[1] for x in msg_data if isinstance(x,tuple)),None)
            if not raw: continue
            process_incoming_message(email.message_from_bytes(raw)); checked+=1
    return {"checked":checked,"configured":True}

def worker():
    time.sleep(10)
    while True:
        try:
            if AUTOMATION_ENABLED: poll_mail_once()
        except Exception as e: log_event("mail_poll_error",None,str(e))
        time.sleep(MAIL_POLL_SECONDS)

_worker_started=False
def start_worker():
    global _worker_started
    if _worker_started: return
    _worker_started=True
    threading.Thread(target=worker,daemon=True,name="modvera-mail-worker").start()

@router.get("/demo/{token}", response_class=HTMLResponse)
def demo(token:str):
    conn=db(); row=conn.execute("SELECT html FROM demos WHERE token=?",(token,)).fetchone(); conn.close()
    if not row: raise HTTPException(404,"Demo not found")
    return HTMLResponse(row["html"])

@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(email:str=""):
    if email:
        conn=db(); conn.execute("INSERT OR REPLACE INTO suppressions(email,reason,created_at) VALUES(?,?,?)",(email,"unsubscribe link",now())); conn.execute("UPDATE leads SET status='LOST',updated_at=? WHERE lower(email)=lower(?)",(now(),email)); conn.commit(); conn.close()
    return HTMLResponse("<html><body style='font-family:Arial;padding:40px'><h2>You have been unsubscribed.</h2><p>You will not receive further outreach from Modvera at this email address.</p></body></html>")

@router.get("/api/automation/status")
def automation_status(request:Request):
    require_owner(request); conn=db(); counts={"sent":conn.execute("SELECT count(*) c FROM sent_messages").fetchone()["c"],"replies":conn.execute("SELECT count(*) c FROM inbound_replies").fetchone()["c"],"demos":conn.execute("SELECT count(*) c FROM demos").fetchone()["c"],"suppressed":conn.execute("SELECT count(*) c FROM suppressions").fetchone()["c"]}; conn.close(); return {"automation_enabled":AUTOMATION_ENABLED,"smtp_ready":smtp_ready(),"imap_ready":imap_ready(),"auto_demo":AUTO_DEMO_ON_POSITIVE_REPLY,"counts":counts}

@router.post("/api/automation/check-replies")
def check_replies(request:Request):
    require_owner(request); return poll_mail_once()

@router.post("/api/leads/{lead_id}/send-outreach")
def send_outreach(lead_id:int,request:Request):
    require_owner(request); conn=db(); row=conn.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone(); conn.close()
    if not row: raise HTTPException(404,"Lead not found")
    lead=dict(row)
    if not lead.get("email"): raise HTTPException(400,"Lead has no email")
    issues=[]
    try:
        import json; issues=json.loads(lead.get("issues") or "[]")
    except Exception: pass
    if "No website" in issues:
        subject=f"Website for {lead['business_name']}"
        body=f"Hi {lead['business_name']} team,\n\nI found {lead['business_name']} on Google and noticed you don't currently have a website listed.\n\nI run Modvera, and we build modern websites for small businesses that are designed to turn Google visitors into actual calls and customers.\n\nI already have a few ideas for what we could build for {lead['business_name']}.\n\nWould you be interested in seeing a quick example?\n\nJoe\nModvera"
    else:
        subject=f"Quick website idea for {lead['business_name']}"
        top=issues[0] if issues else "your current website"
        body=f"Hi {lead['business_name']} team,\n\nI came across your business and noticed {top.lower()}.\n\nI run Modvera and help small businesses improve their websites so they look better on mobile and turn more visitors into calls and quote requests.\n\nWould you be interested in seeing a quick concept for {lead['business_name']}?\n\nJoe\nModvera"
    try: mid=send_email(lead_id,lead['email'],subject,body,"initial")
    except Exception as e: raise HTTPException(400,str(e))
    return {"ok":True,"message_id":mid}

@router.post("/api/leads/{lead_id}/generate-demo")
def manual_demo(lead_id:int,request:Request):
    require_owner(request); token=create_demo(lead_id); return {"url":f"{PUBLIC_URL}/demo/{token}"}
