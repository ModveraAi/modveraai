import os, stripe
from datetime import datetime
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Template

# =========================
# ENV / CONFIG
# =========================
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Modvera AI")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://modveraai.com")

# Stripe LIVE keys
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")             # sk_live_...
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")# pk_live_...
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Price IDs (from Stripe → Products → Prices)
PRICE_MONTHLY_ID = os.getenv("PRICE_MONTHLY_ID", "")
PRICE_ANNUAL_ID  = os.getenv("PRICE_ANNUAL_ID", "")

# Owner-only free access
DEV_FREE = os.getenv("DEV_FREE", "0") == "1"
DEV_FREE_EMAIL = (os.getenv("DEV_FREE_EMAIL", "modverashop@gmail.com") or "").lower()
DEV_PROMO_CODE_ID = os.getenv("DEV_PROMO_CODE_ID", "")          # promo_...

# Optional pixels
GA_MEASUREMENT_ID = os.getenv("GA_MEASUREMENT_ID", "")
GTM_ID            = os.getenv("GTM_ID", "")
META_PIXEL_ID     = os.getenv("META_PIXEL_ID", "")
TIKTOK_PIXEL_ID   = os.getenv("TIKTOK_PIXEL_ID", "")

# Social links
TIKTOK_URL   = "https://www.tiktok.com/@modvera_ai"
INSTAGRAM_URL= "https://www.instagram.com/modvera_ai"

# =========================
# Shared head snippet
# =========================
HEAD_PIXELS = """
{% if gtm_id %}
<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);})(window,document,'script','dataLayer','{{ gtm_id }}');</script>
{% endif %}
{% if ga_id %}
<script async src="https://www.googletagmanager.com/gtag/js?id={{ ga_id }}"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','{{ ga_id }}');</script>
{% endif %}
{% if meta_pixel %}
<script>!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod? n.callMethod.apply(n,arguments):n.queue.push(arguments)}; if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0'; n.queue=[];t=b.createElement(e);t.async=!0;t.src=v;s=b.getElementsByTagName(e)[0]; s.parentNode.insertBefore(t,s)}(window, document,'script','https://connect.facebook.net/en_US/fbevents.js'); fbq('init','{{ meta_pixel }}'); fbq('track','PageView');</script><noscript><img height="1" width="1" style="display:none" src="https://www.facebook.com/tr?id={{ meta_pixel }}&ev=PageView&noscript=1"/></noscript>
{% endif %}
{% if tiktok_pixel %}
<script>!function (w, d, t) { w.TiktokAnalyticsObject=t;var ttq=w[t]=w[t]||[]; ttq.methods=['page','track','identify','instances','debug','on','off','once','ready','alias','group','enableCookie','disableCookie'],ttq.setAndDefer=function(t,e){t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}};for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]); ttq.instance=function(t){var e=ttq._i[t]||[];return function(){ttq.push([t].concat(Array.prototype.slice.call(arguments,0)))}}; ttq.load=function(e,n){var i='https://analytics.tiktok.com/i18n/pixel/events.js'; ttq._i=ttq._i||{},ttq._i[e]=[],ttq._t=ttq._t||{},ttq._t[e]=+new Date,ttq._o=ttq._o||{},ttq._o[e]=n||{}; var o=document.createElement('script'); o.type='text/javascript',o.async=!0;o.src=i+'?sdkid='+e+'&lib='+t; var a=document.getElementsByTagName('script')[0]; a.parentNode.insertBefore(o,a)}; ttq.load('{{ tiktok_pixel }}'); ttq.page(); }(window, document, 'ttq');</script>
{% endif %}
"""

# =========================
# Pages (Templates)
# =========================

LANDING_TMPL = Template("""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ business }} — Automations Suite</title>
<link rel="icon" type="image/png" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:type" content="website"><meta property="og:url" content="{{ public }}">
<meta property="og:title" content="{{ business }} — Automate Websites, Reception, Ads & CRM">
<meta property="og:description" content="All-in-one dashboard. 50% OFF for first 20 users.">
<meta property="og:image" content="{{ public }}/og-cover.jpg"><meta name="twitter:card" content="summary_large_image">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:radial-gradient(1200px 600px at 50% -10%,rgba(120,119,198,.12),transparent 60%),#0b0f1a;color:#e5e7eb}
  .glow{box-shadow:0 10px 30px rgba(99,102,241,.3)}
  .mv-float-cta{position:fixed;right:18px;bottom:18px;z-index:9999;padding:12px 16px;border-radius:999px;font-weight:700;background:linear-gradient(90deg,#818cf8,#22d3ee);color:#0b1220;box-shadow:0 12px 30px rgba(0,0,0,.35);transition:transform .15s ease,opacity .15s ease}
  .mv-float-cta:hover{transform:translateY(-2px)} .mv-float-hide{opacity:0;pointer-events:none;transform:translateY(8px)}
  .mv-help-toast{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;background:#0f172a;border:1px solid #1e293b;color:#e5e7eb;border-radius:14px;padding:10px 14px;font-size:14px;box-shadow:0 10px 30px rgba(0,0,0,.35);z-index:9998;display:none}
  .mv-help-toast a{color:#93c5fd;text-decoration:underline}
  .mv-sticky{position:sticky;top:0;z-index:50;backdrop-filter:saturate(140%) blur(10px);background:rgba(11,15,26,.75);border-bottom:1px solid rgba(148,163,184,.15)}
  a:hover{color:#fff}
</style>
""" + HEAD_PIXELS + """
</head><body>
{% if gtm_id %}<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={{ gtm_id }}" height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>{% endif %}
<header class="mv-sticky"><div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
  <div class="flex items-center gap-2"><div class="h-9 w-9 rounded-xl bg-indigo-500/15 border border-indigo-400/30 flex items-center justify-center text-indigo-300 font-black">M</div><div class="font-semibold text-lg">{{ business }}</div></div>
  <nav class="hidden md:flex items-center gap-6 text-sm text-slate-300">
    <a href="/dashboard" class="hover:text-white">Dashboard</a>
    <a href="#pricing" class="hover:text-white">Pricing</a>
    <a href="/social" class="hover:text-white">Social</a>
    <a href="/legal/privacy" class="hover:text-white">Privacy</a>
    <a href="/legal/terms" class="hover:text-white">Terms</a>
    <a href="/contact" class="hover:text-white">Contact</a>
    <span class="hidden md:inline-block h-5 w-px bg-slate-700 mx-1"></span>
    <a href="{{ tiktok }}" target="_blank" rel="noopener" class="hover:text-white">TikTok</a>
    <a href="{{ instagram }}" target="_blank" rel="noopener" class="hover:text-white">Instagram</a>
    <a href="/dashboard#Billing" class="ml-2 px-3 py-2 rounded-lg bg-indigo-500 text-slate-950 font-semibold hover:brightness-110">Subscribe</a>
  </nav>
  <a href="/dashboard#Billing" class="md:hidden px-3 py-2 rounded-lg bg-indigo-500 text-slate-950 font-semibold">Subscribe</a>
</div></header>

<div class="max-w-7xl mx-auto px-4 py-10">
  <section class="mt-10 text-center">
    <div class="inline-block px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-400/30 text-indigo-300 text-xs">Launch Promo — 50% OFF for first 20 users</div>
    <h1 class="mt-5 text-4xl md:text-6xl font-extrabold leading-tight">Automate Your Business<br class="hidden md:block"/>with <span class="text-indigo-300">AI</span>.</h1>
    <p class="text-slate-300 mt-4 max-w-2xl mx-auto">Websites, AI receptionist, ads, and CRM — all in one dashboard.</p>
    <div class="mt-6 flex flex-col sm:flex-row items-center justify-center gap-3">
      <a href="/dashboard#Billing" class="px-6 py-3 rounded-xl bg-indigo-500 text-slate-950 font-semibold glow" data-cta="hero">Subscribe Now</a>
      <a href="#pricing" class="px-6 py-3 rounded-xl bg-slate-800 border border-slate-700">See Pricing</a>
    </div>
    <div class="mt-3 text-sm text-slate-400">Need help? <a href="/contact" id="helpLink" class="underline hover:text-white">Contact Support</a></div>
    <div class="mt-4 flex items-center justify-center gap-4 text-slate-400"><span class="text-xs uppercase tracking-wide">Follow us</span><a href="{{ tiktok }}" target="_blank" rel="noopener" class="hover:text-white">TikTok</a><span class="opacity-40">•</span><a href="{{ instagram }}" target="_blank" rel="noopener" class="hover:text-white">Instagram</a></div>
    <div class="text-sm text-slate-400 mt-2">No free trial • Cancel anytime • Secure Stripe checkout</div>
  </section>

  <section class="mt-10 grid grid-cols-2 md:grid-cols-4 gap-4 text-center text-sm text-slate-300 opacity-90">
    <div class="bg-slate-900/50 border border-slate-800 rounded-xl p-3">Trusted by local pros</div>
    <div class="bg-slate-900/50 border border-slate-800 rounded-xl p-3">Minutes to set up</div>
    <div class="bg-slate-900/50 border border-slate-800 rounded-xl p-3">Stripe billing built-in</div>
    <div class="bg-slate-900/50 border border-slate-800 rounded-xl p-3">Grows with your team</div>
  </section>

  <section class="mt-14 grid grid-cols-1 md:grid-cols-3 gap-6">
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="font-semibold">AI Receptionist</div><p class="text-sm text-slate-300 mt-2">Reply instantly via SMS or web chat.</p></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="font-semibold">Websites & Proposals</div><p class="text-sm text-slate-300 mt-2">Generate pages & pricing in seconds.</p></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="font-semibold">Social Ads + CRM</div><p class="text-sm text-slate-300 mt-2">Launch ads & track leads.</p></div>
  </section>

  <section class="mt-14"><h2 class="text-xl font-semibold text-center">What users are saying</h2>
    <div class="mt-6 grid grid-cols-1 md:grid-cols-3 gap-6">
      <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="text-sm text-slate-300">“We cut response time by 60%.”</div><div class="text-xs text-slate-500 mt-3">— Xpert Roofing</div></div>
      <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="text-sm text-slate-300">“Feels like we hired extra staff.”</div><div class="text-xs text-slate-500 mt-3">— Sarah L.</div></div>
      <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="text-sm text-slate-300">“Proposals in 2 minutes.”</div><div class="text-xs text-slate-500 mt-3">— Precision Dental</div></div>
    </div>
    <div class="text-center mt-8"><a href="/dashboard#Billing" class="px-6 py-3 rounded-xl bg-indigo-500 text-slate-950 font-semibold glow" data-cta="mid">Get Started</a></div>
  </section>

  <section id="pricing" class="mt-16 grid grid-cols-1 md:grid-cols-2 gap-6">
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6">
      <div class="text-sm text-emerald-400">50% OFF — First 20</div><div class="text-xl font-semibold mt-1">Monthly</div>
      <div class="text-3xl font-extrabold mt-1">$29.99 <span class="text-slate-400 text-base line-through ml-1">$59.99</span></div>
      <ul class="text-slate-300 text-sm mt-4 space-y-2"><li>AI Receptionist</li><li>Website Builder</li><li>Social Ads Generator</li><li>CRM Leads</li></ul>
      <a href="/dashboard#Billing" class="mt-5 inline-block px-4 py-2 rounded-xl bg-indigo-500 text-slate-950 font-semibold glow" data-cta="pricing_monthly">Subscribe Monthly</a>
    </div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6">
      <div class="text-sm text-emerald-400">50% OFF — First 20</div><div class="text-xl font-semibold mt-1">Annual</div>
      <div class="text-3xl font-extrabold mt-1">$299.99 <span class="text-slate-400 text-base line-through ml-1">$599.99</span></div>
      <ul class="text-slate-300 text-sm mt-4 space-y-2"><li>Everything in Monthly</li><li>2 months free equivalent</li><li>Priority support</li></ul>
      <a href="/dashboard#Billing" class="mt-5 inline-block px-4 py-2 rounded-xl bg-indigo-500 text-slate-950 font-semibold glow" data-cta="pricing_annual">Subscribe Annual</a>
    </div>
  </section>

  <a id="mvFloatCta" href="/dashboard#Billing" class="mv-float-cta mv-float-hide">Subscribe — 50% OFF</a>
  <div id="mvHelpToast" class="mv-help-toast">Not sure which plan fits your business? <a id="helpToastLink" href="/contact">Message our team</a>.</div>

  <footer class="text-center text-slate-500 text-xs py-16">
    <div class="flex items-center justify-center gap-4 mb-3"><a href="{{ tiktok }}" target="_blank" rel="noopener" class="hover:text-white">TikTok</a><span class="opacity-40">•</span><a href="{{ instagram }}" target="_blank" rel="noopener" class="hover:text-white">Instagram</a></div>
    © {{ year }} {{ business }} — Operated by Modvera LLC — <a class="underline" href="mailto:modverashop@gmail.com">modverashop@gmail.com</a>
  </footer>
</div>

<script>
(function(){
  window.dataLayer = window.dataLayer || [];
  function fire(evt, extra){ try{ window.dataLayer.push(Object.assign({event:evt, ts:Date.now()}, extra||{})); }catch(e){} }
  document.addEventListener('click', function(e){
    const a=e.target.closest('a'); if(!a) return;
    if(a.matches('[data-cta]') || (a.getAttribute('href')||'').includes('#Billing')) fire('cta_subscribe_click',{placement:a.getAttribute('data-cta')||'landing'});
    if(a.id==='helpLink'||a.id==='helpToastLink') fire('contact_link_click',{placement:'landing'});
  },{passive:true});
  const floatBtn=document.getElementById('mvFloatCta');
  function toggleFloat(){const y=window.scrollY||0;const r=(document.getElementById('pricing')||{}).getBoundingClientRect?.();const near=r? (r.top<260&&r.bottom>0):false; if(y>220&&!near) floatBtn.classList.remove('mv-float-hide'); else floatBtn.classList.add('mv-float-hide');}
  addEventListener('scroll',toggleFloat,{passive:true}); addEventListener('load',toggleFloat);
  let shown=false; function maybeShow(){ if(shown) return; const y=scrollY||0; const r=(document.getElementById('pricing')||{}).getBoundingClientRect?.(); const near=r?(r.top<260&&r.bottom>0):false; if((y>400&&near)||y>1200){ shown=true; var el=document.getElementById('mvHelpToast'); if(el){ el.style.display='block'; setTimeout(()=>{el.style.display='none'},12000);} } }
  addEventListener('scroll',maybeShow,{passive:true}); setTimeout(maybeShow,60000);
})();
</script>
</body></html>
""")

DASHBOARD_TMPL = Template("""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ business }} — Dashboard</title>
<link rel="icon" type="image/png" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:image" content="{{ public }}/og-cover.jpg">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:#0b0f1a;color:#e5e7eb}
  .mv-sticky{position:sticky;top:0;z-index:40;backdrop-filter:saturate(140%) blur(10px);background:rgba(11,15,26,.75);border-bottom:1px solid rgba(148,163,184,.15)}
  .mv-container{max-width:1200px;margin:0 auto;padding:12px 16px}
  .mv-cta{padding:8px 12px;border-radius:10px;font-weight:700;background:#6366f1;color:#0b1220}
  .mv-cta:hover{filter:brightness(1.1)}
</style>
""" + HEAD_PIXELS + """
</head><body>
{% if gtm_id %}<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={{ gtm_id }}" height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>{% endif %}
<header class="mv-sticky"><div class="mv-container flex items-center justify-between">
  <div class="flex items-center gap-2"><div class="h-8 w-8 rounded-xl bg-indigo-500/15 border border-indigo-400/30 flex items-center justify-center text-indigo-300 font-black">M</div><div class="font-semibold">{{ business }}</div></div>
  <nav class="hidden md:flex items-center gap-6 text-sm text-slate-300"><a href="/landing" class="hover:text-white">Home</a><a href="#Features" class="hover:text-white">Features</a><a href="#Support" class="hover:text-white">Support</a><a href="#Billing" class="mv-cta">Billing</a></nav>
  <a href="#Billing" class="md:hidden mv-cta">Billing</a>
</div></header>

<main class="mv-container">
  <section id="Features" class="mt-8 grid grid-cols-1 md:grid-cols-3 gap-6">
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="font-semibold">AI Receptionist</div><p class="text-sm text-slate-300 mt-2">Replies via web chat/SMS.</p></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="font-semibold">Websites & Proposals</div><p class="text-sm text-slate-300 mt-2">Generate pages in seconds.</p></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="font-semibold">Social Ads + CRM</div><p class="text-sm text-slate-300 mt-2">Launch ads & track leads.</p></div>
  </section>

  <section id="Billing" class="mt-12">
    <h2 class="text-xl font-semibold">Billing</h2><p class="text-slate-400 text-sm mt-1">Choose a plan to subscribe via Stripe.</p>
    <div class="mt-4 grid grid-cols-1 md:grid-cols-2 gap-6">
      <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="text-sm text-emerald-400">50% OFF — First 20</div><div class="text-xl font-semibold mt-1">Monthly</div><div class="text-3xl font-extrabold mt-1">$29.99 <span class="text-slate-400 text-base line-through ml-1">$59.99</span></div><button class="mt-5 mv-cta" onclick="subscribe('monthly')">Subscribe Monthly</button></div>
      <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-6"><div class="text-sm text-emerald-400">50% OFF — First 20</div><div class="text-xl font-semibold mt-1">Annual</div><div class="text-3xl font-extrabold mt-1">$299.99 <span class="text-slate-400 text-base line-through ml-1">$599.99</span></div><button class="mt-5 mv-cta" onclick="subscribe('annual')">Subscribe Annual</button></div>
    </div>
  </section>
</main>

<script>
(async function(){ try{ const q=new URLSearchParams(location.search); if(q.get('paid')==='1'){ window.dataLayer=window.dataLayer||[]; window.dataLayer.push({event:'checkout_completed',ts:Date.now()}); if(history.replaceState){ q.delete('paid'); history.replaceState({}, location.pathname + (q.toString()?('?'+q.toString()):'')); } } }catch(e){} })();

async function subscribe(plan){
  window.dataLayer=window.dataLayer||[]; try{ window.dataLayer.push({event:'checkout_started',plan:plan,ts:Date.now()}); }catch(e){}
  const email = prompt("Enter your email to continue:"); if(!email) return;
  const res = await fetch("/create-checkout-session",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({plan,email})});
  const data = await res.json(); if(data.url) location.href=data.url; else alert("Checkout error");
}
document.addEventListener('click', function(e){const a=e.target.closest('a[href^=\"#\"]'); if(!a) return; const id=a.getAttribute('href').slice(1); const el=document.getElementById(id); if(el){ e.preventDefault(); el.scrollIntoView({behavior:'smooth',block:'start'});}},{passive:false});
</script>
</body></html>
""")

SOCIAL_TMPL = Template("""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ business }} — Social</title>
<link rel="icon" type="image/png" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta property="og:image" content="{{ public }}/og-cover.jpg">
<script src="https://cdn.tailwindcss.com"></script>
<style>body{background:#0b0f1a;color:#e5e7eb}.wrap{max-width:760px;margin:0 auto;padding:20px}.card{background:#0f172a;border:1px solid #1e293b;border-radius:16px;padding:20px}.cta{display:inline-block;margin-top:14px;padding:10px 14px;border-radius:12px;background:#6366f1;color:#0b1220;font-weight:700}.cta:hover{filter:brightness(1.1)}</style>
""" + HEAD_PIXELS + """
</head><body>
<div class="wrap"><h1 class="text-2xl font-bold">{{ business }}</h1><p class="text-slate-400 mt-2">Websites, AI receptionist, ads, and CRM — all in one dashboard.</p>
<div class="card mt-6"><h2 class="text-xl font-semibold">Launch Promo — 50% OFF (first 20)</h2><p class="text-slate-300 mt-2">Automate your business today.</p><a class="cta" href="/dashboard#Billing">Subscribe Now</a></div>
<div class="mt-6 flex items-center gap-4 text-slate-400"><span class="text-xs uppercase tracking-wide">Follow</span><a href="{{ tiktok }}" target="_blank" rel="noopener">TikTok</a><span class="opacity-40">•</span><a href="{{ instagram }}" target="_blank" rel="noopener">Instagram</a></div>
</div></body></html>
""")

PRIVACY_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Privacy — Modvera AI</title></head><body><h1>Privacy Policy — Modvera AI</h1><p>We collect limited data to provide services, including account and billing data.</p></body></html>"""
TERMS_HTML   = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Terms — Modvera AI</title></head><body><h1>Terms of Service — Modvera AI</h1><p>By accessing or using Modvera AI, you agree to these Terms.</p></body></html>"""
REFUND_HTML  = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Refund — Modvera AI</title></head><body><h1>Refund Policy — Modvera AI</h1><p>All purchases are final. We do not offer refunds.</p></body></html>"""
CONTACT_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Contact — Modvera AI</title></head><body><h1>Contact Modvera AI</h1><p>Email: <a href="mailto:modverashop@gmail.com">modverashop@gmail.com</a></p></body></html>"""

# =========================
# FastAPI app + routes
# =========================
app = FastAPI(title="Modvera AI — Single-file App")

@app.get("/", response_class=HTMLResponse)
def root(): return RedirectResponse(url="/landing")

@app.get("/landing", response_class=HTMLResponse)
def landing():
    html = LANDING_TMPL.render(
        business=BUSINESS_NAME, public=PUBLIC_URL, year=datetime.now().year,
        gtm_id=GTM_ID or None, ga_id=GA_MEASUREMENT_ID or None,
        meta_pixel=META_PIXEL_ID or None, tiktok_pixel=TIKTOK_PIXEL_ID or None,
        tiktok=TIKTOK_URL, instagram=INSTAGRAM_URL
    )
    return HTMLResponse(html)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = DASHBOARD_TMPL.render(
        business=BUSINESS_NAME, public=PUBLIC_URL,
        gtm_id=GTM_ID or None, ga_id=GA_MEASUREMENT_ID or None,
        meta_pixel=META_PIXEL_ID or None, tiktok_pixel=TIKTOK_PIXEL_ID or None
    )
    return HTMLResponse(html)

@app.get("/social", response_class=HTMLResponse)
def social():
    html = SOCIAL_TMPL.render(
        business=BUSINESS_NAME, public=PUBLIC_URL,
        gtm_id=GTM_ID or None, ga_id=GA_MEASUREMENT_ID or None,
        meta_pixel=META_PIXEL_ID or None, tiktok_pixel=TIKTOK_PIXEL_ID or None,
        tiktok=TIKTOK_URL, instagram=INSTAGRAM_URL
    )
    return HTMLResponse(html)

@app.get("/legal/privacy", response_class=HTMLResponse)
def privacy(): return HTMLResponse(PRIVACY_HTML)
@app.get("/legal/terms", response_class=HTMLResponse)
def terms():   return HTMLResponse(TERMS_HTML)
@app.get("/legal/refund", response_class=HTMLResponse)
def refund():  return HTMLResponse(REFUND_HTML)
@app.get("/contact", response_class=HTMLResponse)
def contact(): return HTMLResponse(CONTACT_HTML)

@app.get("/config", response_class=JSONResponse)
def config():
    return JSONResponse({
        "publishableKey": STRIPE_PUBLISHABLE_KEY,
        "priceMonthly": PRICE_MONTHLY_ID,
        "priceAnnual": PRICE_ANNUAL_ID,
        "publicUrl": PUBLIC_URL
    })

# =========================
# Stripe: checkout + webhook
# =========================
@app.post("/create-checkout-session", response_class=JSONResponse)
async def create_checkout_session(req: Request):
    data = await req.json()
    plan = data.get("plan")
    email = (data.get("email") or "").strip().lower()
    price_id = PRICE_MONTHLY_ID if plan == "monthly" else PRICE_ANNUAL_ID
    if not price_id: raise HTTPException(status_code=400, detail="Price not configured.")

    params = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{PUBLIC_URL}/dashboard?paid=1",
        "cancel_url": f"{PUBLIC_URL}/landing",
        "allow_promotion_codes": True,
        "customer_email": email or None,
    }
    if DEV_FREE and DEV_PROMO_CODE_ID and email == DEV_FREE_EMAIL:
        params["discounts"] = [{"promotion_code": DEV_PROMO_CODE_ID}]
    session = stripe.checkout.Session.create(**params)
    return JSONResponse({"url": session.url})

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=stripe_signature, secret=STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")
    # handle events here if needed
    return JSONResponse({"received": True})


# --- Diagnostics (safe to keep in production) ---
from datetime import datetime
from fastapi.responses import JSONResponse

@app.get("/health", response_class=JSONResponse)
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

@app.get("/test-checkout", response_class=JSONResponse)
def test_checkout(plan: str = "monthly", email: str = "modverashop@gmail.com"):
    price_id = os.getenv("PRICE_MONTHLY_ID") if plan == "monthly" else os.getenv("PRICE_ANNUAL_ID")
    return {
        "plan": plan,
        "email": email,
        "price_id": price_id,
        "have_secret": bool(os.getenv("STRIPE_SECRET_KEY")),
        "public_url": os.getenv("PUBLIC_URL"),
        "dev_free": os.getenv("DEV_FREE", "0") == "1",
        "promo": bool(os.getenv("DEV_PROMO_CODE_ID")),
    }
added test and health routes
