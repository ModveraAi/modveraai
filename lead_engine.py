import csv
import io
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

router = APIRouter()
DB_PATH = os.getenv("MODVERA_DB_PATH", "modvera_leads.db")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_MAPS_ACTOR = os.getenv("APIFY_MAPS_ACTOR", "compass/crawler-google-places")
OWNER_EMAIL = os.getenv("DEV_FREE_EMAIL", "modverashop@gmail.com").lower()

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 business_name TEXT NOT NULL,
 industry TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '',
 address TEXT DEFAULT '', phone TEXT DEFAULT '', email TEXT DEFAULT '',
 website TEXT DEFAULT '', maps_url TEXT DEFAULT '', place_id TEXT DEFAULT '',
 rating REAL DEFAULT 0, reviews INTEGER DEFAULT 0, source TEXT DEFAULT '',
 score INTEGER DEFAULT 0, temperature TEXT DEFAULT 'COLD',
 issues TEXT DEFAULT '[]', status TEXT DEFAULT 'NEW', notes TEXT DEFAULT '',
 last_contacted TEXT DEFAULT '', follow_up TEXT DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(place_id), UNIQUE(phone, business_name)
);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
"""

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def now(): return datetime.now(timezone.utc).isoformat()

def require_owner(request: Request):
    email = (request.cookies.get("auth_email") or "").lower()
    if not email:
        raise HTTPException(status_code=401, detail="Sign in first")
    return email

def normalize_url(url):
    if not url: return ""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I): url = "https://" + url
    return url

def score_lead(data):
    score = 0
    issues = []
    website = (data.get("website") or "").strip()
    rating = float(data.get("rating") or 0)
    reviews = int(float(data.get("reviews") or 0))
    if not website:
        score += 45; issues.append("No website")
    if reviews >= 100: score += 18; issues.append("Established business (100+ reviews)")
    elif reviews >= 30: score += 12; issues.append("Strong review volume")
    elif reviews >= 10: score += 6
    if rating >= 4.5 and reviews >= 10: score += 12; issues.append("Strong Google reputation")
    if data.get("email"): score += 8
    if data.get("phone"): score += 7
    for issue in data.get("audit_issues", []):
        weight = {
            "Website unreachable": 35,
            "No HTTPS": 12,
            "No mobile viewport": 12,
            "No clear call-to-action": 10,
            "No contact form": 10,
            "Missing meta description": 5,
            "Missing page title": 5,
            "Very slow response": 10,
            "Facebook-only web presence": 20,
        }.get(issue, 5)
        score += weight
        if issue not in issues: issues.append(issue)
    score = min(score, 100)
    temp = "HOT" if score >= 75 else "WARM" if score >= 45 else "COLD"
    return score, temp, issues

async def audit_website(url):
    if not url: return []
    u = normalize_url(url)
    issues = []
    if "facebook.com" in u or "instagram.com" in u:
        issues.append("Facebook-only web presence")
        return issues
    if not u.lower().startswith("https://"): issues.append("No HTTPS")
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(follow_redirects=True, timeout=12, headers={"User-Agent":"Mozilla/5.0 ModveraAudit/1.0"}) as c:
            r = await c.get(u)
        elapsed = time.perf_counter() - start
        if r.status_code >= 400:
            issues.append("Website unreachable")
            return issues
        if elapsed > 3.0: issues.append("Very slow response")
        soup = BeautifulSoup(r.text[:1500000], "html.parser")
        if not soup.title or not soup.title.get_text(strip=True): issues.append("Missing page title")
        if not soup.find("meta", attrs={"name": re.compile("description", re.I)}): issues.append("Missing meta description")
        if not soup.find("meta", attrs={"name": re.compile("viewport", re.I)}): issues.append("No mobile viewport")
        if not soup.find("form"): issues.append("No contact form")
        text = " ".join(soup.stripped_strings).lower()
        cta_terms = ["get a quote","free estimate","book now","schedule","contact us","call now","get started","request a quote"]
        if not any(x in text for x in cta_terms): issues.append("No clear call-to-action")
    except Exception:
        issues.append("Website unreachable")
    return issues

def upsert_lead(data):
    data = dict(data)
    score, temp, issues = score_lead(data)
    ts = now()
    vals = {
        "business_name": (data.get("business_name") or data.get("name") or "Unknown").strip(),
        "industry": data.get("industry", ""), "city": data.get("city", ""), "state": data.get("state", ""),
        "address": data.get("address", ""), "phone": data.get("phone", ""), "email": data.get("email", ""),
        "website": data.get("website", ""), "maps_url": data.get("maps_url", ""), "place_id": data.get("place_id", ""),
        "rating": float(data.get("rating") or 0), "reviews": int(float(data.get("reviews") or 0)), "source": data.get("source", ""),
        "score": score, "temperature": temp, "issues": json.dumps(issues), "status": data.get("status", "NEW"),
        "notes": data.get("notes", ""), "created_at": ts, "updated_at": ts,
    }
    conn = db()
    existing = None
    if vals["place_id"]:
        existing = conn.execute("SELECT id FROM leads WHERE place_id=?", (vals["place_id"],)).fetchone()
    if not existing and vals["phone"]:
        existing = conn.execute("SELECT id FROM leads WHERE phone=? AND business_name=?", (vals["phone"], vals["business_name"])).fetchone()
    if existing:
        vals["id"] = existing["id"]
        conn.execute("""UPDATE leads SET business_name=:business_name,industry=:industry,city=:city,state=:state,address=:address,
        phone=:phone,email=CASE WHEN :email='' THEN email ELSE :email END,website=:website,maps_url=:maps_url,
        rating=:rating,reviews=:reviews,source=:source,score=:score,temperature=:temperature,issues=:issues,updated_at=:updated_at
        WHERE id=:id""", vals)
        lead_id = existing["id"]
    else:
        cur = conn.execute("""INSERT INTO leads (business_name,industry,city,state,address,phone,email,website,maps_url,place_id,rating,reviews,source,score,temperature,issues,status,notes,created_at,updated_at)
        VALUES (:business_name,:industry,:city,:state,:address,:phone,:email,:website,:maps_url,:place_id,:rating,:reviews,:source,:score,:temperature,:issues,:status,:notes,:created_at,:updated_at)""", vals)
        lead_id = cur.lastrowid
    conn.commit(); conn.close()
    return lead_id

@router.get("/tools/lead-generator", response_class=HTMLResponse)
def lead_dashboard(request: Request):
    require_owner(request)
    return HTMLResponse(HTML)

@router.get("/api/leads")
def list_leads(request: Request, status: str = "", temperature: str = "", q: str = "", limit: int = 500):
    require_owner(request)
    sql = "SELECT * FROM leads WHERE 1=1"; args=[]
    if status: sql += " AND status=?"; args.append(status)
    if temperature: sql += " AND temperature=?"; args.append(temperature)
    if q:
        sql += " AND (business_name LIKE ? OR city LIKE ? OR industry LIKE ? OR email LIKE ?)"; args += [f"%{q}%"]*4
    sql += " ORDER BY score DESC, reviews DESC LIMIT ?"; args.append(min(limit,2000))
    conn=db(); rows=[dict(x) for x in conn.execute(sql,args).fetchall()]; conn.close()
    for x in rows: x["issues"] = json.loads(x["issues"] or "[]")
    return {"leads":rows}

@router.post("/api/leads")
async def add_lead(request: Request):
    require_owner(request); data=await request.json(); return {"id":upsert_lead(data)}

@router.patch("/api/leads/{lead_id}")
async def update_lead(lead_id:int, request:Request):
    require_owner(request); data=await request.json()
    allowed={"status","notes","email","phone","follow_up","last_contacted"}
    fields=[]; args=[]
    for k,v in data.items():
        if k in allowed: fields.append(f"{k}=?"); args.append(v)
    if not fields: return {"ok":True}
    fields.append("updated_at=?"); args += [now(),lead_id]
    conn=db(); conn.execute(f"UPDATE leads SET {','.join(fields)} WHERE id=?",args); conn.commit(); conn.close()
    return {"ok":True}

@router.delete("/api/leads/{lead_id}")
def delete_lead(lead_id:int, request:Request):
    require_owner(request); conn=db(); conn.execute("DELETE FROM leads WHERE id=?",(lead_id,)); conn.commit(); conn.close(); return {"ok":True}

@router.post("/api/leads/{lead_id}/audit")
async def audit_lead(lead_id:int, request:Request):
    require_owner(request); conn=db(); row=conn.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone(); conn.close()
    if not row: raise HTTPException(404,"Lead not found")
    d=dict(row); d["audit_issues"]=await audit_website(d.get("website")); score,temp,issues=score_lead(d)
    conn=db(); conn.execute("UPDATE leads SET score=?,temperature=?,issues=?,updated_at=? WHERE id=?",(score,temp,json.dumps(issues),now(),lead_id)); conn.commit(); conn.close()
    return {"score":score,"temperature":temp,"issues":issues}

@router.post("/api/leads/import")
async def import_csv(request:Request, file:UploadFile=File(...)):
    require_owner(request); raw=(await file.read()).decode("utf-8-sig",errors="ignore")
    reader=csv.DictReader(io.StringIO(raw)); count=0
    aliases={"name":"business_name","title":"business_name","company":"business_name","url":"website","web":"website","telephone":"phone","totalScore":"rating","reviewsCount":"reviews","placeId":"place_id"}
    for row in reader:
        mapped={}
        for k,v in row.items(): mapped[aliases.get(k,k)] = v or ""
        mapped["source"] = mapped.get("source") or "CSV Import"; upsert_lead(mapped); count+=1
    return {"imported":count}

@router.get("/api/leads/export")
def export_csv(request:Request):
    require_owner(request); conn=db(); rows=[dict(x) for x in conn.execute("SELECT * FROM leads ORDER BY score DESC").fetchall()]; conn.close()
    out=io.StringIO(); fields=list(rows[0].keys()) if rows else ["business_name","industry","city","state","phone","email","website","score","temperature","status"]
    w=csv.DictWriter(out,fieldnames=fields); w.writeheader(); w.writerows(rows)
    return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=modvera_leads.csv"})

@router.post("/api/discover/google")
async def discover_google(request:Request):
    require_owner(request); body=await request.json(); query=(body.get("query") or "").strip(); location=(body.get("location") or "").strip(); max_results=min(int(body.get("max_results") or 20),60)
    if not query or not location: raise HTTPException(400,"query and location required")
    if not GOOGLE_MAPS_API_KEY: raise HTTPException(400,"Add GOOGLE_MAPS_API_KEY in Render environment variables")
    text=f"{query} in {location}"
    headers={"Content-Type":"application/json","X-Goog-Api-Key":GOOGLE_MAPS_API_KEY,"X-Goog-FieldMask":"places.id,places.displayName,places.formattedAddress,places.websiteUri,places.nationalPhoneNumber,places.rating,places.userRatingCount,places.googleMapsUri"}
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.post("https://places.googleapis.com/v1/places:searchText",headers=headers,json={"textQuery":text,"pageSize":min(max_results,20)})
    if r.status_code>=400: raise HTTPException(r.status_code,r.text)
    found=[]
    for p in r.json().get("places",[]):
        d={"business_name":(p.get("displayName") or {}).get("text",""),"address":p.get("formattedAddress",""),"phone":p.get("nationalPhoneNumber",""),"website":p.get("websiteUri",""),"maps_url":p.get("googleMapsUri",""),"place_id":p.get("id",""),"rating":p.get("rating",0),"reviews":p.get("userRatingCount",0),"industry":query,"city":location,"source":"Google Places"}
        d["id"]=upsert_lead(d); found.append(d)
    return {"found":len(found),"leads":found}

@router.get("/api/leads/{lead_id}/outreach")
def outreach(lead_id:int, request:Request):
    require_owner(request); conn=db(); row=conn.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone(); conn.close()
    if not row: raise HTTPException(404,"Lead not found")
    d=dict(row); issues=json.loads(d.get("issues") or "[]"); top=issues[0] if issues else "your current online presence"
    if "No website" in issues:
        subject=f"Website idea for {d['business_name']}"
        message=f"Hi {d['business_name']} team,\n\nI found you on Google and noticed you don’t currently have a website listed. You already have {d['reviews']} Google reviews, so there’s a good chance people are finding you but have nowhere strong to convert into a call or quote.\n\nI run Modvera and we build modern, mobile-friendly websites for businesses like yours. I can put together a quick homepage concept for {d['business_name']} so you can see what it could look like before deciding anything.\n\nWorth sending it over?\n\nJoe\nModvera"
    else:
        subject=f"Quick website idea for {d['business_name']}"
        message=f"Hi {d['business_name']} team,\n\nI came across your business and took a quick look at the website. One thing that stood out was {top.lower()}.\n\nI run Modvera. We rebuild business websites to make them cleaner, faster, mobile-friendly, and better at turning visitors into calls and quote requests. I can make a quick concept showing what I’d change for {d['business_name']}.\n\nWant me to send it over?\n\nJoe\nModvera"
    return {"subject":subject,"message":message}

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Modvera Lead Engine</title><style>
:root{--bg:#08090c;--card:#11131a;--line:#252936;--text:#f7f8fb;--muted:#9298aa;--accent:#746cff;--hot:#ff5668;--warm:#ffb84d;--cold:#5f86ff;--good:#3ddc97}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}.top{position:sticky;top:0;z-index:5;background:#08090ce8;backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:14px 18px;display:flex;align-items:center;justify-content:space-between}.brand{font-weight:800}.wrap{max-width:1500px;margin:auto;padding:18px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}.stat,.panel{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:14px}.stat b{font-size:25px;display:block}.stat span{color:var(--muted);font-size:12px}.controls{display:grid;grid-template-columns:2fr 2fr 1fr auto;gap:8px;margin-bottom:12px}input,select,button{background:#0d0f15;color:white;border:1px solid var(--line);border-radius:10px;padding:10px}button{cursor:pointer;font-weight:700}button.primary{background:var(--accent);border-color:var(--accent)}button:hover{filter:brightness(1.12)}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.tablewrap{overflow:auto;border:1px solid var(--line);border-radius:15px}table{width:100%;border-collapse:collapse;min-width:1050px}th,td{padding:10px;border-bottom:1px solid #1c1f29;text-align:left;font-size:13px}th{position:sticky;top:0;background:#10121a;color:#b9bfd0}.badge{padding:4px 8px;border-radius:999px;font-size:11px;font-weight:800}.HOT{background:#3c1620;color:#ff8390}.WARM{background:#382814;color:#ffd080}.COLD{background:#14213d;color:#8dabff}.score{font-size:18px;font-weight:900}.muted{color:var(--muted)}a{color:#a9a4ff}.actions{display:flex;gap:5px}.modal{display:none;position:fixed;inset:0;background:#000a;z-index:20;align-items:center;justify-content:center;padding:15px}.modal.on{display:flex}.box{width:min(720px,100%);background:#11131a;border:1px solid var(--line);border-radius:18px;padding:18px}.box textarea{width:100%;height:260px;background:#090b10;color:white;border:1px solid var(--line);border-radius:10px;padding:12px}@media(max-width:800px){.stats{grid-template-columns:1fr 1fr}.controls{grid-template-columns:1fr}.wrap{padding:10px}}
</style></head><body><div class="top"><div class="brand">MODVERA <span class="muted">Lead Engine</span></div><a href="/dashboard">← Dashboard</a></div><div class="wrap">
<div class="stats"><div class="stat"><b id="total">0</b><span>Total leads</span></div><div class="stat"><b id="hot">0</b><span>Hot leads</span></div><div class="stat"><b id="new">0</b><span>Not contacted</span></div><div class="stat"><b id="pipeline">0</b><span>Interested / proposal</span></div></div>
<div class="panel"><b>Find businesses</b><div class="controls"><input id="query" placeholder="Industry: roofer, plumber, dentist..."><input id="location" placeholder="City / state: Dallas, TX"><input id="max" type="number" value="20" min="1" max="60"><button class="primary" onclick="discover()">Find Leads</button></div><div class="muted" style="font-size:12px">Uses Google Places when GOOGLE_MAPS_API_KEY is configured. Leads are deduplicated automatically.</div></div>
<div class="toolbar"><input id="search" placeholder="Search leads..." oninput="load()"><select id="temp" onchange="load()"><option value="">All temperatures</option><option>HOT</option><option>WARM</option><option>COLD</option></select><button onclick="document.getElementById('file').click()">Import CSV</button><input id="file" type="file" accept=".csv" hidden onchange="upload(this.files[0])"><button onclick="location.href='/api/leads/export'">Export CSV</button><button onclick="load()">Refresh</button></div>
<div class="tablewrap"><table><thead><tr><th>Score</th><th>Business</th><th>Location</th><th>Proof</th><th>Problem</th><th>Contact</th><th>Status</th><th>Actions</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<div id="modal" class="modal" onclick="if(event.target===this)this.classList.remove('on')"><div class="box"><h3 id="msub"></h3><textarea id="mmsg"></textarea><div class="toolbar"><button class="primary" onclick="copyMsg()">Copy Outreach</button><button onclick="document.getElementById('modal').classList.remove('on')">Close</button></div></div></div>
<script>
let leads=[]; async function load(){let q=encodeURIComponent(document.getElementById('search').value),t=document.getElementById('temp').value;let d=await (await fetch(`/api/leads?q=${q}&temperature=${t}`)).json();leads=d.leads||[];render()}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function render(){total.textContent=leads.length;hot.textContent=leads.filter(x=>x.temperature==='HOT').length;new.textContent=leads.filter(x=>x.status==='NEW').length;pipeline.textContent=leads.filter(x=>['INTERESTED','PROPOSAL'].includes(x.status)).length;rows.innerHTML=leads.map(x=>`<tr><td><div class="score">${x.score}</div><span class="badge ${x.temperature}">${x.temperature}</span></td><td><b>${esc(x.business_name)}</b><div class="muted">${esc(x.industry)}</div></td><td>${esc(x.city||x.address)}</td><td>${x.rating?`⭐ ${x.rating} (${x.reviews})`:''}${x.maps_url?`<div><a target="_blank" href="${esc(x.maps_url)}">Maps</a></div>`:''}${x.website?`<div><a target="_blank" href="${esc(x.website)}">Website</a></div>`:'<div class="badge HOT">NO WEBSITE</div>'}</td><td>${esc((x.issues||[]).slice(0,3).join(' • '))}</td><td>${esc(x.phone)}<br>${esc(x.email)}</td><td><select onchange="status(${x.id},this.value)">${['NEW','CONTACTED','FOLLOW_UP','INTERESTED','PROPOSAL','WON','LOST'].map(s=>`<option ${x.status===s?'selected':''}>${s}</option>`).join('')}</select></td><td><div class="actions"><button onclick="audit(${x.id})">Audit</button><button class="primary" onclick="outreach(${x.id})">Pitch</button></div></td></tr>`).join('')}
async function discover(){let b={query:query.value,location:location.value,max_results:+max.value};if(!b.query||!b.location)return alert('Enter industry and location');let r=await fetch('/api/discover/google',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});let d=await r.json();if(!r.ok)return alert(d.detail||'Search failed');alert(`Found ${d.found} leads`);load()}
async function upload(f){if(!f)return;let fd=new FormData();fd.append('file',f);let d=await (await fetch('/api/leads/import',{method:'POST',body:fd})).json();alert(`Imported ${d.imported} rows`);load()}
async function audit(id){let d=await (await fetch(`/api/leads/${id}/audit`,{method:'POST'})).json();alert(`${d.temperature} ${d.score}/100\n${(d.issues||[]).join('\n')}`);load()}
async function status(id,status){await fetch(`/api/leads/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,last_contacted:status==='CONTACTED'?new Date().toISOString():undefined})});load()}
async function outreach(id){let d=await (await fetch(`/api/leads/${id}/outreach`)).json();msub.textContent=d.subject;mmsg.value=d.message;modal.classList.add('on')}
function copyMsg(){navigator.clipboard.writeText(mmsg.value);alert('Copied')}
load();
</script></body></html>'''
