import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Request

from lead_engine import db, now, upsert_lead, audit_website, score_lead, GOOGLE_MAPS_API_KEY, require_owner
from automation_engine import send_email, smtp_ready, is_suppressed, log_event

router = APIRouter()

PROSPECTING_ENABLED = os.getenv("PROSPECTING_ENABLED", "0").lower() in ("1","true","yes")
AUTO_OUTREACH_ENABLED = os.getenv("AUTO_OUTREACH_ENABLED", "0").lower() in ("1","true","yes")
PROSPECT_INTERVAL_SECONDS = max(3600, int(os.getenv("PROSPECT_INTERVAL_SECONDS", "21600")))
DAILY_SEND_LIMIT = max(1, int(os.getenv("DAILY_SEND_LIMIT", "25")))
MIN_AUTO_SEND_SCORE = max(50, int(os.getenv("MIN_AUTO_SEND_SCORE", "75")))
TARGET_INDUSTRIES = [x.strip() for x in os.getenv("TARGET_INDUSTRIES", "roofing,plumbing,hvac,electrician,landscaping,concrete,painting,auto repair,dentist,med spa").split(",") if x.strip()]
TARGET_LOCATIONS = [x.strip() for x in os.getenv("TARGET_LOCATIONS", "Detroit MI,Dallas TX,Tampa FL,Phoenix AZ,Charlotte NC,Nashville TN").split(",") if x.strip()]
RESULTS_PER_SEARCH = min(20, max(5, int(os.getenv("RESULTS_PER_SEARCH", "20"))))

PUBLIC_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
BAD_EMAIL_WORDS = ("example.com","wixpress","sentry","cloudflare","wordpress","noreply","no-reply")

async def google_places_search(query, location):
    if not GOOGLE_MAPS_API_KEY: return []
    headers={"Content-Type":"application/json","X-Goog-Api-Key":GOOGLE_MAPS_API_KEY,"X-Goog-FieldMask":"places.id,places.displayName,places.formattedAddress,places.websiteUri,places.nationalPhoneNumber,places.rating,places.userRatingCount,places.googleMapsUri"}
    async with httpx.AsyncClient(timeout=25) as c:
        r=await c.post("https://places.googleapis.com/v1/places:searchText",headers=headers,json={"textQuery":f"{query} in {location}","pageSize":RESULTS_PER_SEARCH})
    if r.status_code>=400: return []
    out=[]
    for p in r.json().get("places",[]):
        out.append({"business_name":(p.get("displayName") or {}).get("text", ""),"industry":query,"city":location,"address":p.get("formattedAddress", ""),"phone":p.get("nationalPhoneNumber", ""),"website":p.get("websiteUri", ""),"maps_url":p.get("googleMapsUri", ""),"place_id":p.get("id", ""),"rating":p.get("rating", 0),"reviews":p.get("userRatingCount", 0),"source":"Auto Google Places"})
    return out

async def public_email_from_site(url):
    if not url: return ""
    if not url.startswith("http"): url="https://"+url
    parsed=urlparse(url); root=f"{parsed.scheme}://{parsed.netloc}"
    pages=[url,urljoin(root,"/contact"),urljoin(root,"/contact-us"),urljoin(root,"/about"),urljoin(root,"/about-us")]
    seen=set()
    async with httpx.AsyncClient(timeout=10,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 ModveraResearch/1.0"}) as c:
        for page in pages:
            if page in seen: continue
            seen.add(page)
            try:
                r=await c.get(page)
                if r.status_code>=400: continue
                text=r.text[:1000000]
                emails=[]
                soup=BeautifulSoup(text,"html.parser")
                for a in soup.select('a[href^="mailto:"]'):
                    emails.append((a.get("href") or "").replace("mailto:","").split("?",1)[0])
                emails += PUBLIC_EMAIL_RE.findall(text)
                for e in emails:
                    e=e.strip().lower()
                    if e and not any(b in e for b in BAD_EMAIL_WORDS): return e
            except Exception: pass
    return ""

def sent_today_count():
    conn=db(); row=conn.execute("SELECT count(*) c FROM sent_messages WHERE kind='initial' AND date(sent_at)=date('now')").fetchone(); conn.close(); return int(row["c"])

def already_contacted(lead_id):
    conn=db(); row=conn.execute("SELECT 1 FROM sent_messages WHERE lead_id=? AND kind='initial' LIMIT 1",(lead_id,)).fetchone(); conn.close(); return bool(row)

def outreach_copy(lead):
    issues=json.loads(lead.get("issues") or "[]")
    if "No website" in issues:
        subject=f"Website for {lead['business_name']}"
        body=f"Hi {lead['business_name']} team,\n\nI found {lead['business_name']} on Google and noticed you don't currently have a website listed.\n\nI run Modvera, and we build modern websites for small businesses that are designed to turn Google visitors into actual calls and customers.\n\nI already have a few ideas for what we could build for {lead['business_name']}.\n\nWould you be interested in seeing a quick example?\n\nJoe\nModvera"
    else:
        top=(issues[0] if issues else "your current website").lower()
        subject=f"Quick website idea for {lead['business_name']}"
        body=f"Hi {lead['business_name']} team,\n\nI came across your business and noticed {top}.\n\nI run Modvera and help small businesses improve their websites so they look better on mobile and turn more visitors into calls and quote requests.\n\nWould you be interested in seeing a quick concept for {lead['business_name']}?\n\nJoe\nModvera"
    return subject,body

async def enrich_and_score(lead_id):
    conn=db(); row=conn.execute("SELECT * FROM leads WHERE id=?",(lead_id,)).fetchone(); conn.close()
    if not row: return None
    lead=dict(row)
    if lead.get("website") and not lead.get("email"):
        email=await public_email_from_site(lead["website"])
        if email:
            conn=db(); conn.execute("UPDATE leads SET email=?,updated_at=? WHERE id=?",(email,now(),lead_id)); conn.commit(); conn.close(); lead["email"]=email
    issues=await audit_website(lead.get("website")) if lead.get("website") else []
    lead["audit_issues"]=issues
    score,temp,merged=score_lead(lead)
    conn=db(); conn.execute("UPDATE leads SET score=?,temperature=?,issues=?,updated_at=? WHERE id=?",(score,temp,json.dumps(merged),now(),lead_id)); conn.commit(); conn.close()
    lead.update({"score":score,"temperature":temp,"issues":json.dumps(merged)})
    return lead

async def run_cycle():
    discovered=0; enriched=0; sent=0
    if not GOOGLE_MAPS_API_KEY: return {"discovered":0,"enriched":0,"sent":0,"reason":"GOOGLE_MAPS_API_KEY missing"}
    for location in TARGET_LOCATIONS:
        for industry in TARGET_INDUSTRIES:
            try: results=await google_places_search(industry,location)
            except Exception as e: log_event("prospecting_search_error",None,str(e)); continue
            for item in results:
                lead_id=upsert_lead(item); discovered+=1
                try: lead=await enrich_and_score(lead_id); enriched+=1
                except Exception as e: log_event("prospecting_enrich_error",lead_id,str(e)); continue
                if not AUTO_OUTREACH_ENABLED or not smtp_ready() or not lead: continue
                if sent_today_count()>=DAILY_SEND_LIMIT: return {"discovered":discovered,"enriched":enriched,"sent":sent,"daily_limit":True}
                if int(lead.get("score") or 0)<MIN_AUTO_SEND_SCORE or not lead.get("email") or already_contacted(lead_id) or is_suppressed(lead["email"]): continue
                subject,body=outreach_copy(lead)
                try:
                    send_email(lead_id,lead["email"],subject,body,"initial"); sent+=1
                except Exception as e: log_event("auto_send_error",lead_id,str(e))
    log_event("prospecting_cycle",None,f"discovered={discovered}; enriched={enriched}; sent={sent}")
    return {"discovered":discovered,"enriched":enriched,"sent":sent}

def worker():
    time.sleep(20)
    while True:
        if PROSPECTING_ENABLED:
            try: asyncio.run(run_cycle())
            except Exception as e: log_event("prospecting_cycle_error",None,str(e))
        time.sleep(PROSPECT_INTERVAL_SECONDS)

_started=False
def start_prospecting_worker():
    global _started
    if _started: return
    _started=True
    threading.Thread(target=worker,daemon=True,name="modvera-prospecting-worker").start()

@router.get("/api/prospecting/status")
def status(request:Request):
    require_owner(request)
    return {"prospecting_enabled":PROSPECTING_ENABLED,"auto_outreach_enabled":AUTO_OUTREACH_ENABLED,"daily_send_limit":DAILY_SEND_LIMIT,"min_score":MIN_AUTO_SEND_SCORE,"industries":TARGET_INDUSTRIES,"locations":TARGET_LOCATIONS,"google_ready":bool(GOOGLE_MAPS_API_KEY),"smtp_ready":smtp_ready(),"sent_today":sent_today_count()}

@router.post("/api/prospecting/run")
async def run_now(request:Request):
    require_owner(request); return await run_cycle()
