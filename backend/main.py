"""
InviteForge Backend — FastAPI
Handles event creation, invite pages, SMS sending, RSVP tracking, payments
"""

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field as _Field
from typing import Optional
import os, json, uuid, re, asyncio, shutil, html, time
from datetime import datetime, timedelta
import threading
from pathlib import Path
from urllib.parse import quote as urlquote
from twilio.rest import Client

import database as db
import auth as auth_module

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

app = FastAPI(title="InviteForge API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── STATIC FILES ──
app.mount("/app", StaticFiles(directory="../frontend/app", html=True), name="app")
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

# Create uploads directory and mount it
UPLOADS_DIR = Path("../frontend/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# ── CONFIG ──
TEST_MODE            = os.getenv("TEST_MODE", "true").lower() == "true"
TWILIO_SID           = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN         = os.getenv("TWILIO_TOKEN", "")
STRIPE_SECRET_KEY    = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_URL           = os.getenv("PUBLIC_URL", "").rstrip("/")
RESEND_API_KEY       = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL           = os.getenv("FROM_EMAIL", "InviteForge <onboarding@resend.dev>")

# Only load Stripe if keys are present
stripe = None
if STRIPE_SECRET_KEY:
    try:
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        stripe = _stripe
    except ImportError:
        pass

# ── RATE LIMITING ──
# Simple in-memory per-IP rate limiter (resets on restart — good enough for free tier)
_rate_store: dict = {}  # {ip: [timestamp, ...]}

def check_rate_limit(ip: str, max_calls: int, window_seconds: int) -> bool:
    """Returns True if request is allowed, False if rate limited."""
    now = time.time()
    calls = _rate_store.get(ip, [])
    calls = [t for t in calls if now - t < window_seconds]
    if len(calls) >= max_calls:
        return False
    calls.append(now)
    _rate_store[ip] = calls
    return True

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")

# ── EMAIL ──
def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send email via Resend API. Returns True on success."""
    api_key = os.getenv("RESEND_API_KEY", "") or RESEND_API_KEY
    if not api_key or not to:
        return False
    import urllib.request as _req, ssl as _ssl
    body = json.dumps({"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html_body}).encode()
    req = _req.Request("https://api.resend.com/emails", data=body, method="POST",
                       headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                                "User-Agent": "python-urllib/3.11",
                                "Accept": "application/json"})
    try:
        _req.urlopen(req, timeout=10)
        return True
    except Exception:
        return False

def build_magic_link_email(event: dict, event_id: str, auth_token: str) -> str:
    link = f"{PUBLIC_URL}/app/?event={event_id}&token={auth_token}"
    event_name = html.escape(event.get("name", "Your Event"))
    event_date = html.escape(event.get("date", ""))
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
  body{{font-family:'Helvetica Neue',Arial,sans-serif;background:#0a0e1a;color:#f8f6f0;margin:0;padding:40px 20px;}}
  .card{{max-width:520px;margin:0 auto;background:#111827;border-radius:16px;overflow:hidden;border:1px solid rgba(201,169,110,0.2);}}
  .header{{background:linear-gradient(160deg,#0a0e1a,#1e2d45);padding:40px 40px 32px;text-align:center;}}
  .logo{{font-size:28px;font-weight:700;color:#c9a96e;letter-spacing:-0.5px;margin-bottom:8px;}}
  .logo span{{color:#f8f6f0;}}
  .body{{padding:40px;}}
  h2{{font-size:22px;font-weight:700;color:#f8f6f0;margin:0 0 8px;}}
  p{{font-size:15px;color:#9ca3af;line-height:1.7;margin:0 0 24px;}}
  .event-box{{background:rgba(201,169,110,0.06);border:1px solid rgba(201,169,110,0.2);border-radius:10px;padding:16px 20px;margin-bottom:28px;}}
  .event-name{{font-size:18px;font-weight:700;color:#c9a96e;margin-bottom:4px;}}
  .event-date{{font-size:13px;color:#9ca3af;}}
  .btn{{display:block;width:100%;padding:18px;background:#c9a96e;color:#0a0e1a;text-align:center;text-decoration:none;border-radius:10px;font-size:16px;font-weight:700;letter-spacing:0.5px;box-sizing:border-box;}}
  .note{{font-size:12px;color:#6b7280;margin-top:24px;text-align:center;}}
  .footer{{padding:24px 40px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;font-size:12px;color:#4b5563;}}
</style></head>
<body>
<div class="card">
  <div class="header">
    <div class="logo">Invite<span>Forge</span></div>
  </div>
  <div class="body">
    <h2>Your Dashboard is Ready</h2>
    <p>Here's your personal link to track RSVPs and manage your event. Bookmark it — tap it any time to check in.</p>
    <div class="event-box">
      <div class="event-name">{event_name}</div>
      {'<div class="event-date">📅 ' + event_date + '</div>' if event_date else ''}
    </div>
    <a href="{link}" class="btn">Open My Dashboard →</a>
    <p class="note">This link is unique to you. Don't share it with guests.<br/>It gives full access to your RSVP dashboard.</p>
  </div>
  <div class="footer">InviteForge · Luxury Digital Invitations · <a href="{PUBLIC_URL}/privacy" style="color:#c9a96e;">Privacy</a></div>
</div>
</body></html>"""

def send_magic_link_email(event: dict, event_id: str, auth_token: str) -> bool:
    email = event.get("host_email", "")
    if not email:
        return False
    subject = f"Your InviteForge Dashboard — {event.get('name', 'Your Event')}"
    html_body = build_magic_link_email(event, event_id, auth_token)
    return send_email(email, subject, html_body)

# ── EVENT STORAGE ──
GIST_ID      = os.getenv("GIST_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Wire database module with credentials and initialise
db.configure(GIST_ID, GITHUB_TOKEN)
db.init_db()
db.load_from_gist()   # Restore persisted data on startup

# Drop-in wrappers — all existing code calls these unchanged
def load_events() -> dict:
    return db.load_events()

def save_events(events: dict):
    db.save_events(events)

# ── MODELS ──
class TestSendRequest(BaseModel):
    number: str
    message: str
    sender: str = "InviteForge"

class InviteRequest(BaseModel):
    name: str
    number: str
    message: str
    sender: str = "InviteForge"

class BulkInviteRequest(BaseModel):
    guests: list
    message: str
    sender: str = "InviteForge"
    event_id: Optional[str] = None

class EventCreateRequest(BaseModel):
    name: str
    type: str = ""
    date: str = ""
    time: str = ""
    venue: str = ""
    rsvp_deadline: str = ""
    host_email: str = ""
    template: str = "luxury"
    message: str = ""
    sender: str = "InviteForge"
    media_url: str = ""
    guests: list = []  # [{name, number}] — stored server-side for account isolation

class RSVPRequest(BaseModel):
    guest_name: str  = _Field("",  max_length=100)
    attending: str   = _Field("",  max_length=10)
    guests_count: int = 1
    dietary: str     = _Field("",  max_length=500)
    message: str     = _Field("",  max_length=1000)

class SaveGuestsRequest(BaseModel):
    event_id: str
    auth_token: str = ""
    guests: list  # [{name, number}]

class CheckoutRequest(BaseModel):
    event_id: str
    auth_token: str = ""
    guests: list   # [{name, number}]
    message: str
    sender: str = "InviteForge"

class PhoneCheckoutRequest(BaseModel):
    event_id: str
    auth_token: str = ""
    method: str = "phone_sms"  # phone_sms | phone_whatsapp

# ── HELPERS ──
def format_number(raw: str) -> Optional[str]:
    num = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if num.startswith("+"): return num
    if re.match(r'^44\d{10}$', num): return f"+{num}"          # 447712345678
    if num.startswith("39") and len(num) >= 12: return f"+{num}"
    if num.startswith("1") and len(num) == 11: return f"+{num}"
    if num.startswith("7") and len(num) == 10: return f"+44{num}"
    if num.startswith("07") and len(num) == 11: return f"+44{num[1:]}"
    return None

def twilio_client():
    return Client(TWILIO_SID, TWILIO_TOKEN)

def get_base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    return str(request.base_url).rstrip("/")

async def fire_sms_blast(guests: list, message: str, sender: str, event_id: str):
    """Fire SMS to all valid guests. Used by both direct send and Stripe webhook."""
    # Sanitise sender: Twilio alpha IDs max 11 chars, no spaces
    sender = re.sub(r'\s+', '', sender or '')[:11] or 'InviteForge'
    client = twilio_client()
    results = []
    for guest in guests:
        name = guest.get("name", "")
        raw_number = guest.get("number", "")
        # Normalise number in case it was stored without + prefix
        number = format_number(raw_number) if raw_number else None
        if not number:
            results.append({"name": name, "success": False, "error": "Invalid number"})
            continue
        invite_url = f"{PUBLIC_URL}/invite/{event_id}?name={urlquote(name)}" if PUBLIC_URL else f"/invite/{event_id}?name={urlquote(name)}"
        personalised = message.replace("[Name]", name).replace("[InviteLink]", invite_url)
        try:
            msg = client.messages.create(body=personalised, from_=sender, to=number)
            results.append({"name": name, "number": number, "success": True, "sid": msg.sid})
        except Exception as e:
            results.append({"name": name, "number": number, "success": False, "error": str(e)})
        await asyncio.sleep(0.3)  # Rate limit: 300ms between sends

    sent = sum(1 for r in results if r["success"])
    failed = len(results) - sent

    # Update event status
    events = load_events()
    if event_id in events:
        events[event_id]["sent"] = True
        events[event_id]["sent_count"] = sent
        events[event_id]["failed_count"] = failed
        events[event_id]["sent_at"] = datetime.now().isoformat()
        # Revenue tracking
        twilio_cost = sent * 0.043
        platform_revenue = 50 + sent * 0.30 - twilio_cost
        events[event_id]["twilio_cost"] = round(twilio_cost, 4)
        events[event_id]["platform_revenue"] = round(platform_revenue, 4)
        save_events(events)

    return {"total": len(results), "sent": sent, "failed": failed, "results": results}


# ── INVITE PAGE TEMPLATE ──
INVITE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{EVENT_NAME} — You're Invited</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;0,900;1,400;1,700&family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --gold: #c9a96e; --gold-light: #e8d5a3; --gold-dark: #a07840;
      --navy: #080c18; --navy-mid: #0e1525; --navy-light: #1a2540;
      --white: #f8f6f0; --grey: #8a93a8;
    }}
    html {{ scroll-behavior: smooth; }}
    body {{ font-family: 'Inter', sans-serif; background: var(--navy); color: var(--white); min-height: 100vh; }}

    /* ── HERO ── */
    .hero {{
      position: relative; min-height: 100svh;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      text-align: center; overflow: hidden; padding: 40px 24px;
    }}
    .hero-media {{ position: absolute; inset: 0; z-index: 0; }}
    .hero-media video, .hero-media img {{ width: 100%; height: 100%; object-fit: cover; opacity: 0.35; }}
    .hero-gradient {{
      position: absolute; inset: 0; z-index: 1;
      background: {HERO_GRADIENT}; opacity: 0.85;
    }}
    .hero-content {{ position: relative; z-index: 2; animation: fadeUp 1.2s ease both; }}
    @keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(30px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    .invite-tag {{
      display: inline-block;
      font-size: 11px; font-weight: 600; letter-spacing: 4px; text-transform: uppercase;
      color: var(--gold); border: 1px solid rgba(201,169,110,0.4);
      padding: 8px 20px; border-radius: 100px; margin-bottom: 32px;
      backdrop-filter: blur(8px); background: rgba(201,169,110,0.06);
    }}
    .hero h1 {{
      font-family: 'Playfair Display', serif;
      font-size: clamp(42px, 10vw, 88px); font-weight: 700; line-height: 1.05;
      color: var(--white); margin-bottom: 16px; text-shadow: 0 4px 40px rgba(0,0,0,0.6);
    }}
    .hero-divider {{ width: 60px; height: 1px; background: var(--gold); margin: 24px auto; opacity: 0.6; }}
    .hero-date {{
      font-family: 'Cormorant Garamond', serif;
      font-size: clamp(18px, 4vw, 26px); font-weight: 300; font-style: italic;
      color: var(--gold-light); letter-spacing: 1px;
    }}
    .hero-venue {{ font-size: 14px; color: rgba(248,246,240,0.6); margin-top: 10px; letter-spacing: 1px; }}
    .hero-cta {{ margin-top: 48px; display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; }}
    .btn-rsvp {{
      display: inline-block; padding: 18px 48px;
      background: var(--gold); color: var(--navy);
      font-size: 14px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase;
      border: none; border-radius: 100px; cursor: pointer; text-decoration: none;
      transition: all 0.3s; font-family: 'Inter', sans-serif;
      box-shadow: 0 8px 32px rgba(201,169,110,0.35);
    }}
    .btn-rsvp:hover {{ background: var(--gold-light); transform: translateY(-2px); box-shadow: 0 12px 40px rgba(201,169,110,0.5); }}
    .btn-details {{
      display: inline-block; padding: 18px 36px;
      background: transparent; color: var(--gold);
      font-size: 14px; font-weight: 600; letter-spacing: 1px;
      border: 1px solid rgba(201,169,110,0.4); border-radius: 100px; cursor: pointer;
      text-decoration: none; transition: all 0.3s; font-family: 'Inter', sans-serif;
      backdrop-filter: blur(8px);
    }}
    .btn-details:hover {{ border-color: var(--gold); background: rgba(201,169,110,0.08); }}
    .scroll-hint {{
      position: absolute; bottom: 32px; left: 50%; transform: translateX(-50%);
      z-index: 2; animation: bounce 2s ease infinite;
      color: rgba(201,169,110,0.5); font-size: 20px;
    }}
    @keyframes bounce {{ 0%,100% {{ transform: translateX(-50%) translateY(0); }} 50% {{ transform: translateX(-50%) translateY(8px); }} }}

    /* ── MESSAGE ── */
    .section {{ padding: 80px 24px; max-width: 720px; margin: 0 auto; }}
    .section-label {{
      font-size: 10px; font-weight: 700; letter-spacing: 4px; text-transform: uppercase;
      color: var(--gold); margin-bottom: 32px; display: flex; align-items: center; gap: 16px;
    }}
    .section-label::after {{ content: ''; flex: 1; height: 1px; background: rgba(201,169,110,0.2); }}
    .message-block {{
      background: rgba(255,255,255,0.03); border: 1px solid rgba(201,169,110,0.12);
      border-radius: 20px; padding: 48px;
      backdrop-filter: blur(16px);
    }}
    .message-greeting {{
      font-family: 'Playfair Display', serif; font-style: italic;
      font-size: clamp(24px, 5vw, 36px); color: var(--gold-light);
      margin-bottom: 24px; line-height: 1.3;
    }}
    .message-body {{
      font-family: 'Cormorant Garamond', serif;
      font-size: clamp(18px, 3vw, 22px); font-weight: 300; line-height: 1.9;
      color: rgba(248,246,240,0.8); white-space: pre-line;
    }}

    /* ── DETAILS ── */
    .details-section {{ padding: 0 24px 80px; }}
    .details-grid {{
      max-width: 720px; margin: 0 auto;
      display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px;
    }}
    .detail-card {{
      background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
      border-radius: 16px; padding: 28px 24px; transition: all 0.2s;
    }}
    .detail-card:hover {{ border-color: rgba(201,169,110,0.2); background: rgba(201,169,110,0.04); }}
    .detail-icon {{ font-size: 22px; margin-bottom: 12px; }}
    .detail-label {{ font-size: 10px; font-weight: 700; letter-spacing: 3px; text-transform: uppercase; color: var(--gold); margin-bottom: 8px; }}
    .detail-value {{ font-size: 16px; color: var(--white); line-height: 1.4; font-weight: 500; }}

    /* ── RSVP ── */
    .rsvp-section {{
      padding: 80px 24px;
      background: rgba(201,169,110,0.04);
      border-top: 1px solid rgba(201,169,110,0.1);
    }}
    .rsvp-inner {{ max-width: 560px; margin: 0 auto; text-align: center; }}
    .rsvp-title {{
      font-family: 'Playfair Display', serif;
      font-size: clamp(32px, 7vw, 52px); font-weight: 700;
      color: var(--white); margin-bottom: 12px;
    }}
    .rsvp-sub {{ color: var(--grey); font-size: 15px; margin-bottom: 40px; }}
    .rsvp-form {{ text-align: left; }}
    .rsvp-group {{ margin-bottom: 20px; }}
    .rsvp-label {{ display: block; font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: var(--gold); margin-bottom: 8px; }}
    .rsvp-input, .rsvp-select, .rsvp-textarea {{
      width: 100%; padding: 14px 18px;
      background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px; color: var(--white); font-family: 'Inter', sans-serif; font-size: 15px;
      outline: none; transition: border-color 0.2s;
    }}
    .rsvp-input:focus, .rsvp-select:focus, .rsvp-textarea:focus {{ border-color: var(--gold); background: rgba(255,255,255,0.07); }}
    .rsvp-input::placeholder, .rsvp-textarea::placeholder {{ color: rgba(255,255,255,0.25); }}
    .rsvp-select option {{ background: var(--navy-mid); }}
    .rsvp-attending {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }}
    .rsvp-attending-btn {{
      padding: 16px; border-radius: 12px; border: 2px solid rgba(255,255,255,0.1);
      background: transparent; color: var(--white); font-size: 15px; font-weight: 600;
      cursor: pointer; transition: all 0.2s; font-family: 'Inter', sans-serif;
    }}
    .rsvp-attending-btn:hover {{ border-color: var(--gold); color: var(--gold); }}
    .rsvp-attending-btn.yes.selected {{ background: rgba(34,197,94,0.15); border-color: #22c55e; color: #22c55e; }}
    .rsvp-attending-btn.no.selected {{ background: rgba(239,68,68,0.1); border-color: #ef4444; color: #ef4444; }}
    #rsvpAttending {{ display: none; }}
    .btn-submit-rsvp {{
      width: 100%; padding: 18px; border-radius: 12px;
      background: var(--gold); color: var(--navy);
      font-size: 16px; font-weight: 700; border: none; cursor: pointer;
      transition: all 0.2s; font-family: 'Inter', sans-serif; letter-spacing: 1px; margin-top: 8px;
    }}
    .btn-submit-rsvp:hover {{ background: var(--gold-light); transform: translateY(-1px); }}
    .rsvp-success {{ text-align: center; padding: 40px; display: none; }}
    .rsvp-success-icon {{ font-size: 48px; margin-bottom: 16px; }}
    .rsvp-success h3 {{ font-family: 'Playfair Display', serif; font-size: 28px; color: var(--gold); margin-bottom: 12px; }}
    .rsvp-success p {{ color: var(--grey); font-size: 15px; line-height: 1.7; }}

    /* ── FOOTER ── */
    footer {{
      text-align: center; padding: 40px 24px;
      border-top: 1px solid rgba(255,255,255,0.05);
      font-size: 12px; color: rgba(255,255,255,0.2);
    }}
    footer a {{ color: var(--gold); text-decoration: none; opacity: 0.6; }}

    @media (max-width: 600px) {{
      .message-block {{ padding: 32px 24px; }}
      .hero-cta {{ flex-direction: column; align-items: center; }}
      .btn-rsvp, .btn-details {{ width: 100%; text-align: center; }}
    }}
  </style>
</head>
<body>

  <!-- Loading overlay — hides content until fonts/media ready -->
  <div id="pageLoader" style="position:fixed;inset:0;background:#050914;display:flex;align-items:center;justify-content:center;z-index:9999;transition:opacity 0.6s ease;">
    <div style="text-align:center;">
      <div style="width:48px;height:48px;border:3px solid rgba(201,169,110,0.2);border-top-color:#c9a96e;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 16px;"></div>
      <div style="color:#c9a96e;font-size:12px;letter-spacing:3px;text-transform:uppercase;">Loading your invitation</div>
    </div>
  </div>
  <style>@keyframes spin{{to{{transform:rotate(360deg)}}}}</style>
  <script>window.addEventListener('load',function(){{var l=document.getElementById('pageLoader');l.style.opacity='0';setTimeout(function(){{l.style.display='none'}},600);}});</script>

  <section class="hero">
    <div class="hero-media" id="heroMedia">{HERO_MEDIA}</div>
    <div class="hero-gradient"></div>
    <div class="hero-content">
      <div class="invite-tag">{INVITE_TAG}</div>
      <h1>{EVENT_NAME}</h1>
      <div class="hero-divider"></div>
      <div class="hero-date">{EVENT_DATE_FORMATTED}</div>
      {VENUE_LINE}
      <div class="hero-cta">
        <a href="#rsvp" class="btn-rsvp">RSVP Now</a>
        <a href="#details" class="btn-details">View Details</a>
        <a id="calBtn" href="#" class="btn-details" style="display:none;" target="_blank" rel="noopener">📅 Google Calendar</a>
        <a id="calBtnIcs" href="#" class="btn-details" style="display:none;" download="invite.ics">📅 Apple / Outlook</a>
      </div>
    </div>
    <div class="scroll-hint">↓</div>
  </section>

  <section class="section" id="message">
    <div class="section-label">Your Personal Invitation</div>
    <div class="message-block">
      <div class="message-greeting" id="greeting">Dear Guest,</div>
      <div class="message-body">{MESSAGE_BODY}</div>
    </div>
  </section>

  <section class="details-section" id="details">
    <div class="details-grid">
      {DATE_CARD}
      {VENUE_CARD}
      {TIME_CARD}
      {DEADLINE_CARD}
    </div>
  </section>

  <section class="rsvp-section" id="rsvp">
    <div class="rsvp-inner">
      <div class="rsvp-title">Will you be there?</div>
      <div class="rsvp-sub">Your response means the world to us. Just a moment of your time.</div>
      <div class="rsvp-form" id="rsvpForm">
        <div class="rsvp-group">
          <label class="rsvp-label">Your Name</label>
          <input class="rsvp-input" id="rsvpName" placeholder="Full name" value=""/>
        </div>
        <div class="rsvp-group">
          <label class="rsvp-label">Will you be attending?</label>
          <div class="rsvp-attending">
            <button class="rsvp-attending-btn yes" onclick="selectAttending('yes')">✓ Attending</button>
            <button class="rsvp-attending-btn no" onclick="selectAttending('no')">✗ Can't Make It</button>
          </div>
          <input type="hidden" id="rsvpAttending"/>
        </div>
        <div id="rsvpYesFields" style="display:none;">
          <div class="rsvp-group">
            <label class="rsvp-label">Number of guests (including yourself)</label>
            <select class="rsvp-select" id="rsvpGuests">
              <option value="1">Just me</option>
              <option value="2">2 guests</option>
              <option value="3">3 guests</option>
              <option value="4">4 guests</option>
              <option value="5">5+ guests</option>
            </select>
          </div>
          <div class="rsvp-group">
            <label class="rsvp-label">Dietary requirements (optional)</label>
            <input class="rsvp-input" id="rsvpDietary" placeholder="e.g. Vegetarian, Gluten free, Nut allergy"/>
          </div>
        </div>
        <div class="rsvp-group">
          <label class="rsvp-label">Message to the host (optional)</label>
          <textarea class="rsvp-textarea" id="rsvpMessage" rows="3" placeholder="Anything you'd like to add..."></textarea>
        </div>
        <button class="btn-submit-rsvp" id="rsvpSubmitBtn" onclick="submitRSVP()">Send My RSVP</button>
      </div>
      <div class="rsvp-success" id="rsvpSuccess">
        <div class="rsvp-success-icon">💌</div>
        <h3>RSVP Received</h3>
        <p>Thank you! Your response has been sent and the host has been notified. We look forward to seeing you.</p>
        <p style="margin-top:12px;font-size:13px;opacity:0.6;">You can close this page — your RSVP is saved.</p>
      </div>
    </div>
  </section>

  <!-- GALLERY SECTION -->
  <section class="section" id="gallery" style="padding:60px 20px 40px;">
    <div style="max-width:600px;margin:0 auto;">

      <!-- Upload form -->
      <div style="text-align:center;margin-bottom:40px;">
        <div style="font-size:11px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#c9a96e;margin-bottom:10px;">Share a Moment</div>
        <div style="font-family:'Playfair Display',serif;font-size:26px;font-weight:700;margin-bottom:10px;">Were you there?</div>
        <div style="font-size:15px;color:#9ca3af;margin-bottom:28px;">Share a photo from this event and it may appear in the guest gallery.</div>

        <div id="galleryUploadForm">
          <input type="text" id="galleryName" placeholder="Your name" maxlength="50"
            style="width:100%;max-width:340px;padding:13px 18px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.12);border-radius:10px;color:#f8f6f0;font-family:Inter,sans-serif;font-size:15px;outline:none;display:block;margin:0 auto 12px;box-sizing:border-box;"/>

          <div id="photoDropZone"
            style="max-width:340px;margin:0 auto 12px;border:2px dashed rgba(201,169,110,0.35);border-radius:12px;padding:28px 20px;cursor:pointer;transition:border-color 0.2s;"
            onclick="document.getElementById('galleryFileInput').click()"
            ondragover="event.preventDefault();this.style.borderColor='#c9a96e'"
            ondragleave="this.style.borderColor='rgba(201,169,110,0.35)'"
            ondrop="handlePhotoDrop(event)">
            <div style="font-size:36px;margin-bottom:8px;">📷</div>
            <div id="dropZoneText" style="font-size:14px;color:#9ca3af;">Tap to choose a photo</div>
          </div>
          <input type="file" id="galleryFileInput" accept="image/*" style="display:none;" onchange="handlePhotoSelect(this.files[0])"/>

          <div id="galleryPreviewWrap" style="display:none;max-width:340px;margin:0 auto 12px;">
            <img id="galleryPreviewImg" style="width:100%;border-radius:10px;border:1px solid rgba(201,169,110,0.25);"/>
          </div>

          <button id="gallerySubmitBtn"
            style="max-width:340px;width:100%;padding:14px;background:#c9a96e;color:#0a0e1a;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;font-family:Inter,sans-serif;display:block;margin:0 auto;"
            onclick="submitGalleryPhoto()">Share Photo</button>

          <div id="galleryUploadStatus" style="margin-top:12px;font-size:14px;text-align:center;"></div>
        </div>

        <div id="galleryUploadSuccess" style="display:none;text-align:center;padding:20px;">
          <div style="font-size:40px;margin-bottom:12px;">✅</div>
          <div style="font-size:17px;font-weight:600;color:#f8f6f0;margin-bottom:6px;">Photo submitted!</div>
          <div style="font-size:14px;color:#9ca3af;">The host will review it and add it to the gallery.</div>
          <button onclick="resetGalleryUpload()" style="margin-top:16px;padding:10px 24px;background:rgba(201,169,110,0.1);border:1px solid rgba(201,169,110,0.3);color:#c9a96e;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:Inter,sans-serif;">Share Another</button>
        </div>
      </div>

      <!-- Approved gallery grid -->
      <div id="approvedGallerySection" style="display:none;">
        <div style="font-size:11px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:#c9a96e;margin-bottom:16px;text-align:center;">Guest Gallery</div>
        <div id="approvedGalleryGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;"></div>
      </div>

    </div>
  </section>

  <footer>
    <p>Created with <a href="/" target="_blank">InviteForge</a> &nbsp;·&nbsp; Luxury Digital Invitations</p>
  </footer>

  <script>
    const EVENT_ID = '{EVENT_ID}';
    const urlParams = new URLSearchParams(window.location.search);
    const guestName = urlParams.get('name') || '';
    if (guestName) {{
      document.getElementById('greeting').textContent = 'Dear ' + guestName + ',';
      document.getElementById('rsvpName').value = guestName;
    }}

    // Build calendar links (Google + Apple/iCal)
    (function() {{
      const rawDate = '{RAW_DATE}';
      const rawTime = '{RAW_TIME}';
      const eventName = '{EVENT_NAME_CAL}';
      const venue = '{VENUE_CAL}';
      if (!rawDate) return;
      try {{
        const d = rawDate.replace(/-/g, '');
        let start = d, end = d;
        if (rawTime) {{
          const [h, m] = rawTime.split(':');
          const startH = String(h).padStart(2,'0'), startM = String(m).padStart(2,'0');
          const endH = String(parseInt(h)+2).padStart(2,'0');
          start = d + 'T' + startH + startM + '00';
          end   = d + 'T' + endH   + startM + '00';
        }}
        const googleCal = 'https://calendar.google.com/calendar/render?action=TEMPLATE' +
          '&text=' + encodeURIComponent(eventName) +
          '&dates=' + start + '/' + end +
          (venue ? '&location=' + encodeURIComponent(venue) : '');
        const btn = document.getElementById('calBtn');
        if (btn) {{ btn.href = googleCal; btn.style.display = 'inline-block'; }}
        // Apple Calendar / Outlook: generate .ics data URI
        const icsLines = [
          'BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//InviteForge//EN',
          'BEGIN:VEVENT',
          'DTSTART:' + start,
          'DTEND:' + end,
          'SUMMARY:' + eventName.replace(/,/g, '\\,'),
          venue ? ('LOCATION:' + venue.replace(/,/g, '\\,')) : '',
          'END:VEVENT','END:VCALENDAR'
        ].filter(Boolean).join('\r\n');
        const icsBtn = document.getElementById('calBtnIcs');
        if (icsBtn) {{
          icsBtn.href = 'data:text/calendar;charset=utf8,' + encodeURIComponent(icsLines);
          icsBtn.style.display = 'inline-block';
        }}
      }} catch(e) {{}}
    }})();

    function selectAttending(val) {{
      document.getElementById('rsvpAttending').value = val;
      document.querySelectorAll('.rsvp-attending-btn').forEach(b => b.classList.remove('selected'));
      document.querySelector('.rsvp-attending-btn.' + val).classList.add('selected');
      document.getElementById('rsvpYesFields').style.display = val === 'yes' ? 'block' : 'none';
    }}

    async function submitRSVP() {{
      const btn = document.getElementById('rsvpSubmitBtn');
      if (btn.disabled) return;
      const name = document.getElementById('rsvpName').value.trim();
      const attending = document.getElementById('rsvpAttending').value;
      if (!name) {{ showRsvpError('Please enter your name.'); return; }}
      if (!attending) {{ showRsvpError('Please select whether you are attending.'); return; }}
      btn.disabled = true;
      btn.textContent = 'Sending...';
      const payload = {{
        guest_name: name, attending,
        guests_count: parseInt(document.getElementById('rsvpGuests')?.value || 1),
        dietary: document.getElementById('rsvpDietary')?.value || '',
        message: document.getElementById('rsvpMessage').value || '',
      }};
      try {{
        const res = await fetch('/api/rsvp/' + EVENT_ID, {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        if (!res.ok) {{
          const err = await res.json().catch(function() {{ return {{}}; }});
          showRsvpError(err.detail || 'Something went wrong. Please try again.');
          btn.disabled = false; btn.textContent = 'Send My RSVP';
          return;
        }}
      }} catch(e) {{
        showRsvpError('Could not connect. Please check your internet and try again.');
        btn.disabled = false; btn.textContent = 'Send My RSVP';
        return;
      }}
      document.getElementById('rsvpForm').style.display = 'none';
      document.getElementById('rsvpSuccess').style.display = 'block';
    }}

    function showRsvpError(msg) {{
      let err = document.getElementById('rsvpError');
      if (!err) {{
        err = document.createElement('div');
        err.id = 'rsvpError';
        err.style.cssText = 'color:#f87171;font-size:13px;margin-bottom:12px;padding:10px 14px;background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.3);border-radius:8px;';
        document.getElementById('rsvpSubmitBtn').before(err);
      }}
      err.textContent = '\u26a0\ufe0f ' + msg;
    }}

    // ── GALLERY ──
    var _galleryFile = null;

    function handlePhotoSelect(file) {{
      if (!file) return;
      var ext = file.name.split('.').pop().toLowerCase();
      if (ext === 'heic' || ext === 'heif') {{
        var statusEl = document.getElementById('galleryUploadStatus');
        if (statusEl) {{
          statusEl.textContent = 'iPhone HEIC photos aren\u2019t supported. Open the photo in Photos app, tap Share \u2192 Save Image as JPEG, then upload.';
          statusEl.style.color = '#f59e0b';
        }}
        return;
      }}
      _galleryFile = file;
      var dropText = document.getElementById('dropZoneText');
      if (dropText) dropText.textContent = file.name;
      compressAndPreview(file);
    }}

    function handlePhotoDrop(e) {{
      e.preventDefault();
      var dt = e.dataTransfer;
      var zone = document.getElementById('photoDropZone');
      if (zone) zone.style.borderColor = 'rgba(201,169,110,0.35)';
      if (dt && dt.files && dt.files[0]) handlePhotoSelect(dt.files[0]);
    }}

    function compressAndPreview(file) {{
      var reader = new FileReader();
      reader.onload = function(e) {{
        var img = new Image();
        img.onload = function() {{
          var maxW = 1200;
          var scale = Math.min(maxW / img.width, 1);
          var canvas = document.createElement('canvas');
          canvas.width = Math.round(img.width * scale);
          canvas.height = Math.round(img.height * scale);
          var ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          canvas.toBlob(function(blob) {{
            if (!blob) {{ return; }}
            _galleryFile = blob;
            var prev = document.getElementById('galleryPreviewWrap');
            var prevImg = document.getElementById('galleryPreviewImg');
            if (prev && prevImg) {{
              prevImg.src = URL.createObjectURL(blob);
              prev.style.display = 'block';
            }}
          }}, 'image/jpeg', 0.82);
        }};
        img.src = e.target.result;
      }};
      reader.readAsDataURL(file);
    }}

    async function submitGalleryPhoto() {{
      var name = (document.getElementById('galleryName').value || '').trim();
      var statusEl = document.getElementById('galleryUploadStatus');
      var btn = document.getElementById('gallerySubmitBtn');
      if (!_galleryFile) {{ statusEl.textContent = 'Please choose a photo first.'; statusEl.style.color='#f59e0b'; return; }}
      if (!name) {{ statusEl.textContent = 'Please enter your name.'; statusEl.style.color='#f59e0b'; return; }}
      btn.disabled = true;
      btn.textContent = 'Uploading\u2026';
      statusEl.textContent = '';
      try {{
        var fd = new FormData();
        fd.append('file', _galleryFile, 'photo.jpg');
        fd.append('submitted_by', name);
        var res = await fetch('/api/event/' + EVENT_ID + '/gallery/upload', {{method:'POST', body:fd}});
        var data = await res.json();
        if (res.ok && data.success) {{
          document.getElementById('galleryUploadForm').style.display = 'none';
          document.getElementById('galleryUploadSuccess').style.display = 'block';
        }} else {{
          statusEl.textContent = data.detail || 'Upload failed. Try again.';
          statusEl.style.color = '#f87171';
          btn.disabled = false;
          btn.textContent = 'Share Photo';
        }}
      }} catch(e) {{
        statusEl.textContent = 'Could not connect. Check your connection.';
        statusEl.style.color = '#f87171';
        btn.disabled = false;
        btn.textContent = 'Share Photo';
      }}
    }}

    function resetGalleryUpload() {{
      _galleryFile = null;
      document.getElementById('galleryUploadForm').style.display = 'block';
      document.getElementById('galleryUploadSuccess').style.display = 'none';
      document.getElementById('galleryName').value = '';
      document.getElementById('galleryFileInput').value = '';
      var prev = document.getElementById('galleryPreviewWrap');
      if (prev) prev.style.display = 'none';
      var dropText = document.getElementById('dropZoneText');
      if (dropText) dropText.textContent = 'Tap to choose a photo';
      document.getElementById('galleryUploadStatus').textContent = '';
      document.getElementById('gallerySubmitBtn').disabled = false;
      document.getElementById('gallerySubmitBtn').textContent = 'Share Photo';
    }}

    function loadApprovedGallery() {{
      fetch('/api/event/' + EVENT_ID + '/gallery')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          var photos = data.approved || [];
          var section = document.getElementById('approvedGallerySection');
          var grid = document.getElementById('approvedGalleryGrid');
          if (!photos.length || !section || !grid) return;
          grid.textContent = '';
          photos.forEach(function(p) {{
            var wrap = document.createElement('div');
            wrap.style.cssText = 'position:relative;border-radius:10px;overflow:hidden;aspect-ratio:1;cursor:pointer;';
            var img = document.createElement('img');
            img.src = p.url;
            img.alt = 'Photo by ' + p.submitted_by;
            img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;';
            img.loading = 'lazy';
            var caption = document.createElement('div');
            caption.textContent = p.submitted_by;
            caption.style.cssText = 'position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.55);color:#f8f6f0;font-size:11px;padding:5px 8px;font-family:Inter,sans-serif;';
            wrap.onclick = function() {{ window.open(p.url, '_blank'); }};
            wrap.appendChild(img);
            wrap.appendChild(caption);
            grid.appendChild(wrap);
          }});
          section.style.display = 'block';
        }})
        .catch(function() {{}});
    }}

    loadApprovedGallery();

    // Hide gallery upload section for upcoming events
    (function() {{
      var rawDate = '{RAW_DATE}';
      if (!rawDate) return;
      try {{
        var eventDate = new Date(rawDate + 'T00:00:00');
        var today = new Date(); today.setHours(0,0,0,0);
        if (eventDate > today) {{
          // Event hasn't happened yet — hide upload, only show gallery if photos exist
          var gallerySection = document.getElementById('gallery');
          fetch('/api/event/' + EVENT_ID + '/gallery')
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
              if ((d.approved || []).length === 0) {{
                if (gallerySection) gallerySection.style.display = 'none';
              }} else {{
                var uploadForm = document.getElementById('galleryUploadForm');
                if (uploadForm) uploadForm.style.display = 'none';
                var uploadSuccess = document.getElementById('galleryUploadSuccess');
                if (uploadSuccess) uploadSuccess.style.display = 'none';
              }}
            }})
            .catch(function() {{
              if (gallerySection) gallerySection.style.display = 'none';
            }});
        }}
      }} catch(e) {{}}
    }})();
  </script>
</body>
</html>"""


def build_invite_page(event: dict, event_id: str) -> str:
    template_gradients = {
        "luxury":      "linear-gradient(180deg, rgba(8,12,24,0.6) 0%, rgba(8,12,24,0.95) 100%)",
        "garden":      "linear-gradient(180deg, rgba(5,46,22,0.5) 0%, rgba(5,46,22,0.92) 100%)",
        "minimal":     "linear-gradient(180deg, rgba(15,23,42,0.4) 0%, rgba(15,23,42,0.9) 100%)",
        "celebration": "linear-gradient(180deg, rgba(30,27,75,0.5) 0%, rgba(30,27,75,0.92) 100%)",
        "corporate":   "linear-gradient(180deg, rgba(15,23,42,0.6) 0%, rgba(15,23,42,0.95) 100%)",
    }
    template = event.get("template", "luxury")
    hero_gradient = template_gradients.get(template, template_gradients["luxury"])

    media_url = event.get("media_url", "")
    media_url_e = html.escape(media_url, quote=True)
    if media_url and re.search(r'\.(mp4|mov|webm)$', media_url, re.I):
        hero_media = f'<video autoplay muted loop playsinline><source src="{media_url_e}"></video>'
    elif media_url:
        hero_media = f'<img src="{media_url_e}" alt="Event"/>'
    else:
        hero_media = ""

    raw_date = event.get("date", "")
    raw_time = event.get("time", "")
    try:
        dt = datetime.strptime(raw_date, "%Y-%m-%d")
        date_formatted = dt.strftime("%A, %d %B %Y")
    except Exception:
        date_formatted = raw_date

    date_time_str = date_formatted
    if raw_time:
        try:
            t = datetime.strptime(raw_time, "%H:%M")
            date_time_str += f" at {t.strftime('%I:%M %p').lstrip('0')}"
        except Exception:
            date_time_str += f" at {raw_time}"

    venue = event.get("venue", "")
    venue_e = html.escape(venue)
    venue_line = f'<div class="hero-venue">📍 {venue_e}</div>' if venue else ""

    date_card = f'<div class="detail-card"><div class="detail-icon">📅</div><div class="detail-label">Date</div><div class="detail-value">{html.escape(date_formatted)}</div></div>' if raw_date else ""
    venue_card = f'<div class="detail-card"><div class="detail-icon">📍</div><div class="detail-label">Venue</div><div class="detail-value">{venue_e}</div></div>' if venue else ""
    time_card = f'<div class="detail-card"><div class="detail-icon">🕐</div><div class="detail-label">Time</div><div class="detail-value">{html.escape(raw_time)}</div></div>' if raw_time else ""

    msg = event.get("message", "")
    msg_clean = re.sub(r'https?://\S+', '', msg).strip()
    msg_clean = re.sub(r'\[InviteLink\]', '', msg_clean).strip()
    msg_clean = re.sub(r'\n{3,}', '\n\n', msg_clean)

    # RSVP deadline card
    rsvp_deadline = event.get("rsvp_deadline", "")
    if rsvp_deadline:
        try:
            dl = datetime.strptime(rsvp_deadline, "%Y-%m-%d")
            deadline_formatted = dl.strftime("%d %B %Y")
        except Exception:
            deadline_formatted = rsvp_deadline
        deadline_card = f'<div class="detail-card"><div class="detail-icon">📬</div><div class="detail-label">RSVP By</div><div class="detail-value">{html.escape(deadline_formatted)}</div></div>'
    else:
        deadline_card = ""

    event_name_safe = html.escape(event.get("name", "You're Invited"))

    # Build a human-readable event type tag for the hero badge
    event_type_raw = event.get("type", "").strip().lower()
    type_labels = {
        "wedding":      "Wedding Invitation",
        "birthday":     "Birthday Celebration",
        "party":        "Party Invitation",
        "corporate":    "Corporate Event",
        "anniversary":  "Anniversary Celebration",
        "baby shower":  "Baby Shower",
        "graduation":   "Graduation Celebration",
        "engagement":   "Engagement Party",
        "christening":  "Christening",
        "funeral":      "Memorial Service",
        "conference":   "Conference",
        "gala":         "Gala Evening",
    }
    invite_tag = type_labels.get(event_type_raw, "You're Invited")

    return INVITE_TEMPLATE.format(
        EVENT_NAME=event_name_safe,
        INVITE_TAG=html.escape(invite_tag),
        EVENT_ID=html.escape(event_id),
        HERO_GRADIENT=hero_gradient,
        HERO_MEDIA=hero_media,
        EVENT_DATE_FORMATTED=html.escape(date_time_str),
        VENUE_LINE=venue_line,
        MESSAGE_BODY=html.escape(msg_clean),
        DATE_CARD=date_card,
        VENUE_CARD=venue_card,
        TIME_CARD=time_card,
        DEADLINE_CARD=deadline_card,
        RAW_DATE=html.escape(raw_date),
        RAW_TIME=html.escape(raw_time),
        EVENT_NAME_CAL=html.escape(event.get("name", "")),
        VENUE_CAL=html.escape(venue),
    )


# ── ROUTES ──

@app.get("/")
async def root():
    return FileResponse("../frontend/index.html")

@app.get("/login")
async def login_page():
    return FileResponse("../frontend/login.html")

@app.get("/privacy")
async def privacy():
    return FileResponse("../frontend/privacy.html")

@app.get("/terms")
async def terms():
    return FileResponse("../frontend/terms.html")

@app.get("/manifest.json")
async def manifest():
    return FileResponse("../frontend/manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse("../frontend/sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})

@app.get("/health")
async def health():
    return {"status": "ok", "service": "InviteForge API"}

@app.get("/api/config")
async def get_config():
    return {
        "test_mode": TEST_MODE,
        "stripe_enabled": bool(STRIPE_SECRET_KEY and stripe),
    }


@app.post("/api/events")
async def create_event(req: EventCreateRequest, request: Request):
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=20, window_seconds=3600):
        raise HTTPException(429, detail="Too many events created. Please try again later.")
    events = load_events()
    event_id = str(uuid.uuid4())[:8]
    auth_token = str(uuid.uuid4()).replace("-", "")
    base = get_base_url(request)
    invite_url = f"{base}/invite/{event_id}"
    # Link event to logged-in user if JWT present
    user_id = auth_module.get_current_user_id(request)
    events[event_id] = {
        "id": event_id,
        "user_id": user_id,
        "auth_token": auth_token,
        "name": req.name,
        "type": req.type,
        "date": req.date,
        "time": req.time,
        "venue": req.venue,
        "rsvp_deadline": req.rsvp_deadline,
        "host_email": req.host_email,
        "template": req.template,
        "message": req.message,
        "sender": req.sender,
        "media_url": req.media_url,
        "invite_url": invite_url,
        "created": datetime.now().isoformat(),
        "sent": False,
        "rsvps": [],
        "guests": req.guests,  # [{name, number}] stored server-side per account
    }
    save_events(events)
    email_sent = False
    if req.host_email:
        email_sent = send_magic_link_email(events[event_id], event_id, auth_token)
    return {"event_id": event_id, "invite_url": invite_url, "auth_token": auth_token, "email_sent": email_sent}


NOT_FOUND_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Invitation Not Found — InviteForge</title>
  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=Inter:wght@400;500&display=swap" rel="stylesheet"/>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Inter',sans-serif;background:#0a0e1a;color:#f8f6f0;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;padding:40px 24px}
    .card{max-width:480px}
    .icon{font-size:64px;margin-bottom:24px;opacity:0.6}
    h1{font-family:'Playfair Display',serif;font-size:36px;color:#c9a96e;margin-bottom:16px}
    p{font-size:16px;color:#9ca3af;line-height:1.7;margin-bottom:32px}
    a{display:inline-block;padding:14px 36px;background:#c9a96e;color:#0a0e1a;border-radius:100px;font-weight:700;font-size:15px;text-decoration:none;letter-spacing:0.5px}
    a:hover{background:#e8d5a3}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">💌</div>
    <h1>Invitation Not Found</h1>
    <p>This invitation link has expired, been removed, or the URL may be incorrect.<br/>If you received this link from someone, ask them to resend it.</p>
    <a href="/">Create Your Own Invitation →</a>
  </div>
</body>
</html>"""

@app.get("/invite/{event_id}", response_class=HTMLResponse)
async def serve_invite(event_id: str):
    events = load_events()
    event = events.get(event_id)
    if not event:
        return HTMLResponse(NOT_FOUND_PAGE, status_code=404)
    return HTMLResponse(build_invite_page(event, event_id))


@app.post("/api/upload-media")
async def upload_media(file: UploadFile = File(...), request: Request = None):
    """Upload a video or image — stored in GitHub repo for permanence (survives deploys)."""
    ip = get_client_ip(request) if request else "unknown"
    if not check_rate_limit(ip, max_calls=10, window_seconds=3600):
        raise HTTPException(429, detail="Too many uploads.")
    ext = Path(file.filename).suffix.lower()
    allowed = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if ext not in allowed:
        raise HTTPException(400, detail=f"File type {ext} not supported.")

    data = await file.read()
    # 50MB limit
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, detail="File too large. Maximum size is 50MB.")

    safe_name = str(uuid.uuid4())[:8] + ext

    # Try to store in GitHub repo for permanent CDN access
    if GITHUB_TOKEN:
        try:
            import urllib.request as urllib_req, ssl, base64
            encoded = base64.b64encode(data).decode()
            body = json.dumps({
                "message": f"Upload media: {safe_name}",
                "content": encoded,
            }).encode()
            req = urllib_req.Request(
                f"https://api.github.com/repos/lambosonly25-eng/inviteforge/contents/uploads/{safe_name}",
                data=body, method="PUT",
                headers={"Authorization": f"token {GITHUB_TOKEN}",
                         "Content-Type": "application/json",
                         "Accept": "application/vnd.github.v3+json"}
            )
            with urllib_req.urlopen(req) as r:
                resp = json.load(r)
            cdn_url = resp["content"]["download_url"]
            return {"url": cdn_url, "filename": safe_name, "storage": "github"}
        except Exception:
            pass  # Fall through to local storage

    # Fallback: local disk (ephemeral on Render, works locally)
    dest = UPLOADS_DIR / safe_name
    dest.write_bytes(data)
    return {"url": f"/uploads/{safe_name}", "filename": safe_name, "storage": "local"}


# ── GALLERY ──

def _gallery_cleanup_loop():
    """Background thread: delete photos declined >7 days ago, notify host."""
    while True:
        try:
            cutoff = datetime.now() - timedelta(days=7)
            events = load_events()
            changed = False
            cleaned: dict = {}
            for eid, event in events.items():
                gallery = event.get("gallery", [])
                kept, removed = [], 0
                for p in gallery:
                    if p.get("status") == "declined" and p.get("declined_at"):
                        try:
                            if datetime.fromisoformat(p["declined_at"]) < cutoff:
                                removed += 1
                                changed = True
                                continue
                        except Exception:
                            pass
                    kept.append(p)
                if removed:
                    events[eid]["gallery"] = kept
                    cleaned[eid] = {"event": event, "count": removed}
            if changed:
                save_events(events)
                for eid, info in cleaned.items():
                    host_email = info["event"].get("host_email", "")
                    if not host_email:
                        continue
                    n = info["count"]
                    ename = html.escape(info["event"].get("name", "Your Event"))
                    send_email(
                        host_email,
                        f"Gallery cleanup — {ename}",
                        f'<div style="font-family:Arial,sans-serif;color:#374151;padding:24px;">'
                        f'<p>{n} declined photo{"s have" if n > 1 else " has"} been permanently deleted '
                        f'from <strong>{ename}</strong> after 7 days.</p>'
                        f'<p style="color:#6b7280;font-size:13px;">Your approved gallery is untouched.</p></div>'
                    )
        except Exception:
            pass
        time.sleep(86400)


@app.on_event("startup")
async def startup_event():
    threading.Thread(target=_gallery_cleanup_loop, daemon=True).start()


def _save_photo_to_cdn(data: bytes, path: str) -> str:
    """Try GitHub CDN first, fall back to local. Returns URL."""
    if GITHUB_TOKEN:
        try:
            import urllib.request as _ur, ssl as _ssl, base64 as _b64
            encoded = _b64.b64encode(data).decode()
            body = json.dumps({"message": f"gallery: {path}", "content": encoded}).encode()
            req = _ur.Request(
                f"https://api.github.com/repos/lambosonly25-eng/inviteforge/contents/uploads/{path}",
                data=body, method="PUT",
                headers={"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json",
                         "Accept": "application/vnd.github.v3+json"}
            )
            with _ur.urlopen(req) as r:
                return json.load(r)["content"]["download_url"]
        except Exception:
            pass
    fname = path.split("/")[-1]
    gallery_dir = UPLOADS_DIR / "gallery" / path.split("/")[-2]
    gallery_dir.mkdir(parents=True, exist_ok=True)
    (gallery_dir / fname).write_bytes(data)
    return f"/uploads/gallery/{path.split('/')[-2]}/{fname}"


@app.post("/api/event/{event_id}/gallery/upload")
async def gallery_upload(
    event_id: str,
    file: UploadFile = File(...),
    submitted_by: str = Form(""),
    request: Request = None,
):
    ip = get_client_ip(request) if request else "unknown"
    if not check_rate_limit(ip, max_calls=10, window_seconds=3600):
        raise HTTPException(429, detail="Too many uploads. Try again in an hour.")
    events = load_events()
    if event_id not in events:
        raise HTTPException(404, detail="Event not found")
    ext = Path(file.filename or "photo.jpg").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        raise HTTPException(400, detail="Only image files allowed.")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, detail="Photo too large. Max 10MB.")
    photo_id = str(uuid.uuid4())[:8]
    url = _save_photo_to_cdn(data, f"gallery/{event_id}/{photo_id}{ext}")
    name_safe = html.escape((submitted_by or "A guest").strip()[:50])
    photo = {
        "id": photo_id, "url": url, "submitted_by": name_safe,
        "submitted_at": datetime.now().isoformat(),
        "status": "pending", "declined_at": None,
    }
    event = events[event_id]
    if len(event.get("gallery", [])) >= 100:
        raise HTTPException(400, detail="Gallery is full (100 photo limit).")
    event.setdefault("gallery", []).append(photo)
    save_events(events)
    host_email = event.get("host_email", "")
    if host_email:
        ename = html.escape(event.get("name", "Your Event"))
        tok = event.get("auth_token", "")
        dash = f"{PUBLIC_URL}/app/?event={event_id}&token={tok}"
        send_email(host_email, f"New photo shared — {ename}",
            f'<div style="font-family:Arial,sans-serif;color:#374151;padding:24px;max-width:500px;">'
            f'<p style="font-size:16px;">📸 <strong>{name_safe}</strong> just shared a photo for '
            f'<strong>{ename}</strong>.</p>'
            f'<p>Open your dashboard to approve or decline it — takes 5 seconds.</p>'
            f'<a href="{dash}" style="display:inline-block;padding:12px 28px;background:#c9a96e;'
            f'color:#0a0e1a;text-decoration:none;border-radius:8px;font-weight:700;">Review Photo →</a></div>')
    return {"success": True, "photo_id": photo_id}


@app.get("/api/event/{event_id}/gallery")
async def get_gallery(event_id: str, token: str = "", request: Request = None):
    events = load_events()
    if event_id not in events:
        raise HTTPException(404, detail="Event not found")
    event = events[event_id]
    gallery = event.get("gallery", [])
    user_id = auth_module.get_current_user_id(request) if request else None
    stored_token = event.get("auth_token", "")
    is_host = (user_id and event.get("user_id") == user_id) or (stored_token and token == stored_token)
    approved = [p for p in gallery if p.get("status") == "approved"]
    event_date = event.get("date", "")
    if is_host:
        pending = [p for p in gallery if p.get("status") == "pending"]
        return {"approved": approved, "pending": pending, "is_host": True, "event_date": event_date}
    return {"approved": approved, "is_host": False, "event_date": event_date}


@app.post("/api/event/{event_id}/gallery/{photo_id}/approve")
async def gallery_approve(event_id: str, photo_id: str, request: Request, token: str = ""):
    events = load_events()
    if event_id not in events:
        raise HTTPException(404, detail="Event not found")
    event = events[event_id]
    user_id = auth_module.get_current_user_id(request)
    stored_token = event.get("auth_token", "")
    if not ((user_id and event.get("user_id") == user_id) or (stored_token and token == stored_token)):
        raise HTTPException(403, detail="Not authorised")
    for p in event.get("gallery", []):
        if p["id"] == photo_id:
            p["status"] = "approved"
            p["declined_at"] = None
            save_events(events)
            return {"success": True}
    raise HTTPException(404, detail="Photo not found")


@app.post("/api/event/{event_id}/gallery/{photo_id}/decline")
async def gallery_decline(event_id: str, photo_id: str, request: Request, token: str = ""):
    events = load_events()
    if event_id not in events:
        raise HTTPException(404, detail="Event not found")
    event = events[event_id]
    user_id = auth_module.get_current_user_id(request)
    stored_token = event.get("auth_token", "")
    if not ((user_id and event.get("user_id") == user_id) or (stored_token and token == stored_token)):
        raise HTTPException(403, detail="Not authorised")
    for p in event.get("gallery", []):
        if p["id"] == photo_id:
            p["status"] = "declined"
            p["declined_at"] = datetime.now().isoformat()
            save_events(events)
            return {"success": True}
    raise HTTPException(404, detail="Photo not found")


@app.post("/api/send-test")
async def send_test(req: TestSendRequest, request: Request):
    if not TEST_MODE:
        raise HTTPException(403, detail="Test sends disabled in production.")
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=5, window_seconds=60):
        raise HTTPException(429, detail="Too many test sends. Wait a minute and try again.")
    number = format_number(req.number)
    if not number:
        raise HTTPException(400, detail="Invalid phone number format")
    # Sanitise sender same as blast
    sender = re.sub(r'\s+', '', req.sender or '')[:11] or 'InviteForge'
    try:
        client = twilio_client()
        msg = client.messages.create(body=req.message, from_=sender, to=number)
        return {"success": True, "sid": msg.sid, "to": number}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/send-invite")
async def send_invite(req: InviteRequest, request: Request):
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=300, window_seconds=3600):
        raise HTTPException(429, detail="Rate limit reached. Please wait before sending more.")
    number = format_number(req.number)
    if not number:
        return {"success": False, "error": "Invalid number format"}
    personalised = req.message.replace("[Name]", req.name)
    try:
        client = twilio_client()
        msg = client.messages.create(body=personalised, from_=req.sender, to=number)
        return {"success": True, "sid": msg.sid, "to": number, "name": req.name}
    except Exception as e:
        return {"success": False, "error": str(e), "name": req.name}


MAX_GUESTS = 500

@app.post("/api/create-checkout")
async def create_checkout(req: CheckoutRequest, request: Request):
    """Create a Stripe Checkout session or bypass in TEST_MODE."""
    if len(req.guests) > MAX_GUESTS:
        raise HTTPException(400, detail=f"Guest list too large. Maximum {MAX_GUESTS} guests per event.")
    events = load_events()
    if req.event_id not in events:
        raise HTTPException(404, detail="Event not found")
    # Verify auth token to prevent unauthorized checkout for someone else's event
    stored_token = events[req.event_id].get("auth_token", "")
    if stored_token and req.auth_token != stored_token:
        raise HTTPException(403, detail="Unauthorized")

    # Store pending payload in event so webhook can retrieve it
    events[req.event_id]["pending_send"] = {
        "guests": req.guests,
        "message": req.message,
        "sender": req.sender,
    }
    save_events(events)

    if TEST_MODE or not stripe:
        # Test mode: skip payment, return direct send signal
        return {"test_mode": True, "message": "Test mode — payment skipped"}

    # Production: create Stripe Checkout session
    valid_count = len([g for g in req.guests if g.get("number", "").startswith("+")])
    event_name = events[req.event_id].get("name", "Your Event")

    base = get_base_url(request)
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                # £50 flat — invite page + permanent URL
                "price_data": {
                    "currency": "gbp",
                    "product_data": {
                        "name": f"InviteForge — Invitation Page",
                        "description": f"Beautiful digital invitation for {event_name} with permanent URL",
                    },
                    "unit_amount": 5000,  # £50.00 in pence
                },
                "quantity": 1,
            },
            {
                # £0.30 per SMS
                "price_data": {
                    "currency": "gbp",
                    "product_data": {
                        "name": "Personalised SMS Invitations",
                        "description": f"{valid_count} personalised text messages with invite link",
                    },
                    "unit_amount": 30,  # £0.30 in pence
                },
                "quantity": valid_count,
            },
        ],
        mode="payment",
        success_url=f"{base}/app/?sent=1&event={req.event_id}",
        cancel_url=f"{base}/app/?cancelled=1&event={req.event_id}",
        metadata={"event_id": req.event_id},
    )
    return {"checkout_url": session.url}


@app.post("/api/create-phone-checkout")
async def create_phone_checkout(req: PhoneCheckoutRequest, request: Request):
    """Create a £25 flat Stripe Checkout for phone-based send (SMS or WhatsApp from user's own phone)."""
    events = load_events()
    if req.event_id not in events:
        raise HTTPException(404, detail="Event not found")
    stored_token = events[req.event_id].get("auth_token", "")
    if stored_token and req.auth_token != stored_token:
        raise HTTPException(403, detail="Unauthorized")

    if TEST_MODE or not stripe:
        raise HTTPException(400, detail="Not needed in test mode")

    event_name = events[req.event_id].get("name", "Your Event")
    method_label = "SMS" if req.method == "phone_sms" else "WhatsApp"
    base = get_base_url(request)

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "gbp",
                    "product_data": {
                        "name": f"InviteForge — Invitation Page",
                        "description": f"Beautiful digital invite for {event_name} with permanent URL & RSVP tracking",
                    },
                    "unit_amount": 5000,  # £50.00
                },
                "quantity": 1,
            },
            {
                "price_data": {
                    "currency": "gbp",
                    "product_data": {
                        "name": f"Phone Send Rights ({method_label})",
                        "description": f"Send from your own number — zero spam filtering, 100% deliverability",
                    },
                    "unit_amount": 2500,  # £25.00
                },
                "quantity": 1,
            },
        ],
        mode="payment",
        success_url=f"{base}/app/?sent=1&event={req.event_id}&method={req.method}",
        cancel_url=f"{base}/app/?cancelled=1&event={req.event_id}",
        metadata={"event_id": req.event_id, "method": req.method, "type": "phone_send"},
    )

    # Record that phone send was purchased
    events[req.event_id]["phone_send_method"] = req.method
    events[req.event_id]["phone_checkout_id"] = session.id
    save_events(events)

    return {"checkout_url": session.url}


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    """Stripe calls this on payment.succeeded — fires the SMS blast."""
    if not stripe:
        raise HTTPException(500, detail="Stripe not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        event_id = meta.get("event_id")
        checkout_type = meta.get("type", "")  # "phone_send" or "" for Twilio auto-send
        if event_id:
            events = load_events()
            ev = events.get(event_id, {})
            if checkout_type == "phone_send":
                # Phone send: no SMS blast from our side, but send dashboard magic link
                auth_token = ev.get("auth_token", "")
                if auth_token:
                    send_magic_link_email(ev, event_id, auth_token)
            else:
                # Twilio auto-send: fire SMS blast (idempotent — flag set first to prevent double-fire)
                pending = ev.get("pending_send", {})
                if pending and not ev.get("pending_send_fired"):
                    events[event_id]["pending_send_fired"] = True
                    save_events(events)
                    try:
                        await fire_sms_blast(
                            guests=pending.get("guests", []),
                            message=pending.get("message", ""),
                            sender=pending.get("sender", "InviteForge"),
                            event_id=event_id,
                        )
                        # Send magic link email post-payment — "your invites are flying out"
                        events_fresh = load_events()
                        ev_fresh = events_fresh.get(event_id, {})
                        auth_token = ev_fresh.get("auth_token", "")
                        if auth_token:
                            send_magic_link_email(ev_fresh, event_id, auth_token)
                    except Exception as blast_err:
                        print(f"[CRITICAL] fire_sms_blast failed for event {event_id}: {blast_err}")
                        host_email = ev.get("host_email", "")
                        if host_email:
                            send_email(
                                host_email,
                                "Action required — invites failed to send",
                                f'<div style="font-family:Arial,sans-serif;padding:24px;max-width:500px;">'
                                f'<p style="font-size:16px;">Your payment was processed successfully, but there was a technical error sending your SMS invitations.</p>'
                                f'<p>Please reply to this email or contact us — we will resolve it immediately. Quote event ID: <strong>{html.escape(event_id)}</strong></p>'
                                f'</div>'
                            )

    return {"received": True}


@app.post("/api/rsvp/{event_id}")
async def handle_rsvp(event_id: str, data: RSVPRequest, request: Request):
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=10, window_seconds=300):
        raise HTTPException(429, detail="Too many RSVP submissions.")
    if not data.guest_name or not data.guest_name.strip():
        raise HTTPException(400, detail="Name is required.")
    events = load_events()
    if event_id not in events:
        raise HTTPException(404, detail="Event not found")
    rsvp = {
        "guest_name": data.guest_name,
        "attending": data.attending,
        "guests_count": data.guests_count,
        "dietary": data.dietary,
        "message": data.message,
        "timestamp": datetime.now().isoformat(),
    }
    # Upsert: update existing RSVP for this guest instead of duplicating
    existing = events[event_id].get("rsvps", [])
    updated = False
    for i, r in enumerate(existing):
        if r.get("guest_name", "").lower() == data.guest_name.lower():
            existing[i] = rsvp
            updated = True
            break
    if not updated:
        existing.append(rsvp)
    events[event_id]["rsvps"] = existing
    save_events(events)

    # Notify host by email every time an RSVP comes in
    host_email = events[event_id].get("host_email", "")
    if host_email:
        event_name = html.escape(events[event_id].get("name", "Your Event"))
        guest_name = html.escape(data.guest_name)
        attending_str = "✅ Attending" if data.attending == "yes" else "❌ Can't make it"
        guests_str = f" (+{data.guests_count - 1} more)" if data.attending == "yes" and data.guests_count > 1 else ""
        dietary_str = f"<br/><strong>Dietary:</strong> {html.escape(data.dietary)}" if data.dietary else ""
        msg_str = f"<br/><strong>Message:</strong> {html.escape(data.message)}" if data.message else ""
        auth_token = events[event_id].get("auth_token", "")
        dashboard_link = f"{PUBLIC_URL}/app/?event={event_id}&token={auth_token}" if auth_token else f"{PUBLIC_URL}/login"
        rsvp_email_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
  body{{font-family:'Helvetica Neue',Arial,sans-serif;background:#0a0e1a;color:#f8f6f0;margin:0;padding:40px 20px;}}
  .card{{max-width:520px;margin:0 auto;background:#111827;border-radius:16px;overflow:hidden;border:1px solid rgba(201,169,110,0.2);}}
  .header{{background:linear-gradient(160deg,#0a0e1a,#1e2d45);padding:32px 40px;}}
  .logo{{font-size:24px;font-weight:700;color:#c9a96e;}}
  .logo span{{color:#f8f6f0;}}
  .body{{padding:36px 40px;}}
  h2{{font-size:20px;color:#f8f6f0;margin:0 0 20px;}}
  .rsvp-box{{background:rgba(201,169,110,0.06);border:1px solid rgba(201,169,110,0.2);border-radius:10px;padding:20px 24px;margin-bottom:24px;font-size:15px;line-height:1.8;}}
  .name{{font-size:18px;font-weight:700;color:#c9a96e;margin-bottom:8px;}}
  .btn{{display:block;width:100%;padding:16px;background:#c9a96e;color:#0a0e1a;text-align:center;text-decoration:none;border-radius:10px;font-size:15px;font-weight:700;box-sizing:border-box;}}
  .footer{{padding:20px 40px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;font-size:12px;color:#4b5563;}}
</style></head>
<body><div class="card">
  <div class="header"><div class="logo">Invite<span>Forge</span></div></div>
  <div class="body">
    <h2>New RSVP for {event_name}</h2>
    <div class="rsvp-box">
      <div class="name">{guest_name}{guests_str}</div>
      <strong>{attending_str}</strong>{dietary_str}{msg_str}
    </div>
    <a href="{dashboard_link}" class="btn">View Full Dashboard →</a>
  </div>
  <div class="footer">InviteForge · <a href="{PUBLIC_URL}/privacy" style="color:#c9a96e;">Privacy</a></div>
</div></body></html>"""
        send_email(host_email, f"New RSVP: {data.guest_name} — {event_name}", rsvp_email_html)

    return {"success": True, "message": "RSVP received", "updated": updated}


@app.post("/api/event/{event_id}/guests")
async def save_guests(event_id: str, req: SaveGuestsRequest, request: Request):
    """Store guest list server-side so it's tied to the account, not just the browser."""
    events = load_events()
    event = events.get(event_id)
    if not event:
        raise HTTPException(404, detail="Event not found")
    # Verify ownership via auth_token OR JWT
    user_id = auth_module.get_current_user_id(request)
    stored_token = event.get("auth_token", "")
    owns_via_jwt = user_id and event.get("user_id") == user_id
    owns_via_token = stored_token and req.auth_token == stored_token
    if not owns_via_jwt and not owns_via_token:
        raise HTTPException(403, detail="Not authorised for this event")
    if len(req.guests) > 500:
        raise HTTPException(400, detail="Maximum 500 guests per event")
    events[event_id]["guests"] = req.guests
    save_events(events)
    return {"saved": len(req.guests)}


@app.get("/api/event/{event_id}/rsvps")
async def get_rsvps(event_id: str, token: str = "", request: Request = None):
    events = load_events()
    event = events.get(event_id)
    if not event:
        raise HTTPException(404, detail="Event not found")
    # Ownership check: valid auth_token OR matching JWT user_id
    user_id = auth_module.get_current_user_id(request) if request else None
    stored_token = event.get("auth_token", "")
    owns_via_jwt = user_id and event.get("user_id") == user_id
    owns_via_token = stored_token and (token == stored_token)
    if not owns_via_jwt and not owns_via_token:
        # Fallback: allow if event has no user_id and no auth_token (legacy anonymous)
        if event.get("user_id") or stored_token:
            raise HTTPException(403, detail="Not authorised for this event")
    rsvps = event.get("rsvps", [])
    attending = [r for r in rsvps if r.get("attending") == "yes"]
    declined = [r for r in rsvps if r.get("attending") == "no"]
    return {
        "event_id": event_id,
        "event_name": event.get("name"),
        "rsvps": rsvps,
        "total": len(rsvps),
        "attending": len(attending),
        "declined": len(declined),
        "total_guests": sum(r.get("guests_count", 1) for r in attending),
    }

class LoginRequest(BaseModel):
    email: str

@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    """Find events by host email and send magic link(s)."""
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=5, window_seconds=300):
        raise HTTPException(429, detail="Too many login attempts. Please wait 5 minutes.")
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Invalid email address.")
    events = load_events()
    matched = [(eid, ev) for eid, ev in events.items()
               if ev.get("host_email", "").strip().lower() == email]
    if not matched:
        return {"success": True, "message": "If that email matches an event, a link is on its way."}
    sent = 0
    for event_id, ev in matched:
        auth_token = ev.get("auth_token", "")
        if auth_token and send_magic_link_email(ev, event_id, auth_token):
            sent += 1
    return {"success": True, "message": "If that email matches an event, a link is on its way.", "sent": sent}


# ── ACCOUNT AUTH ──

class RegisterRequest(BaseModel):
    email: str
    password: str

class AccountLoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/auth/register")
async def register(req: RegisterRequest, request: Request):
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=10, window_seconds=3600):
        raise HTTPException(429, detail="Too many attempts. Try again later.")
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Invalid email address.")
    if len(req.password) < 8:
        raise HTTPException(400, detail="Password must be at least 8 characters.")
    if db.get_user_by_email(email):
        raise HTTPException(409, detail="An account with that email already exists.")
    password_hash = auth_module.hash_password(req.password)
    user = db.create_user(email, password_hash)
    token = auth_module.create_token(user["id"], user["email"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}


@app.post("/api/auth/login")
async def account_login(req: AccountLoginRequest, request: Request):
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=10, window_seconds=300):
        raise HTTPException(429, detail="Too many login attempts. Wait 5 minutes.")
    email = req.email.strip().lower()
    user = db.get_user_by_email(email)
    if not user or not auth_module.verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, detail="Incorrect email or password.")
    token = auth_module.create_token(user["id"], user["email"])
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}


@app.get("/api/auth/me")
async def get_me(request: Request):
    user_id = auth_module.get_current_user_id(request)
    if not user_id:
        raise HTTPException(401, detail="Not authenticated")
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, detail="User not found")
    return {"id": user["id"], "email": user["email"], "created_at": user["created_at"]}


@app.get("/api/user/events")
async def get_user_events(request: Request):
    user_id = auth_module.get_current_user_id(request)
    if not user_id:
        raise HTTPException(401, detail="Not authenticated")
    events = db.get_events_for_user(user_id)
    return {
        "events": [
            {
                "event_id":   e.get("id"),
                "name":       e.get("name"),
                "date":       e.get("date"),
                "venue":      e.get("venue"),
                "invite_url": e.get("invite_url"),
                "auth_token": e.get("auth_token", ""),
                "rsvp_count":        len(e.get("rsvps", [])),
                "sent":              e.get("sent", False),
                "phone_send_method": e.get("phone_send_method", ""),
                "created":           e.get("created"),
                "guests":            e.get("guests", []),
            }
            for e in events
        ]
    }


@app.post("/api/resend-link/{event_id}")
async def resend_magic_link(event_id: str, request: Request):
    """Re-send the magic link to the host of a given event."""
    ip = get_client_ip(request)
    if not check_rate_limit(ip, max_calls=3, window_seconds=300):
        raise HTTPException(429, detail="Too many resend requests.")
    events = load_events()
    ev = events.get(event_id)
    if not ev:
        raise HTTPException(404, detail="Event not found")
    auth_token = ev.get("auth_token", "")
    if not ev.get("host_email") or not auth_token:
        raise HTTPException(400, detail="No email address on file for this event.")
    ok = send_magic_link_email(ev, event_id, auth_token)
    return {"success": ok}


@app.get("/api/event/{event_id}/status")
async def get_event_status(event_id: str):
    events = load_events()
    event = events.get(event_id)
    if not event:
        raise HTTPException(404, detail="Event not found")
    return {
        "event_id": event_id,
        "event_name": event.get("name", ""),
        "sent": event.get("sent", False),
        "sent_count": event.get("sent_count", 0),
        "failed_count": event.get("failed_count", 0),
        "invite_url": event.get("invite_url", ""),
        "has_email": bool(event.get("host_email", "")),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
