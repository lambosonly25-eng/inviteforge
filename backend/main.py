"""
InviteForge Backend — FastAPI
Handles event creation, invite pages, SMS sending, RSVP tracking, payments
"""

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import os, json, uuid, re, asyncio, shutil
from datetime import datetime
from pathlib import Path
from twilio.rest import Client

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

# Only load Stripe if keys are present
stripe = None
if STRIPE_SECRET_KEY:
    try:
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        stripe = _stripe
    except ImportError:
        pass

# ── EVENT STORAGE ──
EVENTS_FILE  = Path("events.json")
GIST_ID      = os.getenv("GIST_ID", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

def load_events() -> dict:
    if GIST_ID and GITHUB_TOKEN:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
            )
            import ssl
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, context=ctx) as r:
                data = json.load(r)
            return json.loads(data["files"]["events.json"]["content"])
        except Exception:
            pass
    if EVENTS_FILE.exists():
        try:
            return json.loads(EVENTS_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_events(events: dict):
    content = json.dumps(events, indent=2, default=str)
    if GIST_ID and GITHUB_TOKEN:
        try:
            import urllib.request, ssl
            body = json.dumps({"files": {"events.json": {"content": content}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=body, method="PATCH",
                headers={"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json",
                         "Accept": "application/vnd.github.v3+json"}
            )
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, context=ctx):
                pass
            return
        except Exception:
            pass
    EVENTS_FILE.write_text(content)

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
    template: str = "luxury"
    message: str = ""
    sender: str = "InviteForge"
    media_url: str = ""

class RSVPRequest(BaseModel):
    guest_name: str = ""
    attending: str = ""
    guests_count: int = 1
    dietary: str = ""
    message: str = ""

class CheckoutRequest(BaseModel):
    event_id: str
    guests: list   # [{name, number}]
    message: str
    sender: str = "InviteForge"

# ── HELPERS ──
def format_number(raw: str) -> Optional[str]:
    num = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if num.startswith("+"): return num
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
    client = twilio_client()
    results = []
    for guest in guests:
        name = guest.get("name", "")
        number = guest.get("number", "")
        if not number or not number.startswith("+"):
            results.append({"name": name, "success": False, "error": "Invalid number"})
            continue
        invite_url = f"{PUBLIC_URL}/invite/{event_id}?name={name}" if PUBLIC_URL else f"/invite/{event_id}?name={name}"
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

  <section class="hero">
    <div class="hero-media" id="heroMedia">{HERO_MEDIA}</div>
    <div class="hero-gradient"></div>
    <div class="hero-content">
      <div class="invite-tag">You're Invited</div>
      <h1>{EVENT_NAME}</h1>
      <div class="hero-divider"></div>
      <div class="hero-date">{EVENT_DATE_FORMATTED}</div>
      {VENUE_LINE}
      <div class="hero-cta">
        <a href="#rsvp" class="btn-rsvp">RSVP Now</a>
        <a href="#details" class="btn-details">View Details</a>
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
        <button class="btn-submit-rsvp" onclick="submitRSVP()">Send My RSVP</button>
      </div>
      <div class="rsvp-success" id="rsvpSuccess">
        <div class="rsvp-success-icon">💌</div>
        <h3>RSVP Received</h3>
        <p>Thank you so much. Your response has been noted and the host has been notified.</p>
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

    function selectAttending(val) {{
      document.getElementById('rsvpAttending').value = val;
      document.querySelectorAll('.rsvp-attending-btn').forEach(b => b.classList.remove('selected'));
      document.querySelector('.rsvp-attending-btn.' + val).classList.add('selected');
      document.getElementById('rsvpYesFields').style.display = val === 'yes' ? 'block' : 'none';
    }}

    async function submitRSVP() {{
      const name = document.getElementById('rsvpName').value.trim();
      const attending = document.getElementById('rsvpAttending').value;
      if (!name) {{ alert('Please enter your name.'); return; }}
      if (!attending) {{ alert('Please select whether you are attending.'); return; }}
      const payload = {{
        guest_name: name, attending,
        guests_count: parseInt(document.getElementById('rsvpGuests')?.value || 1),
        dietary: document.getElementById('rsvpDietary')?.value || '',
        message: document.getElementById('rsvpMessage').value || '',
      }};
      try {{
        await fetch('/api/rsvp/' + EVENT_ID, {{
          method: 'POST', headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
      }} catch(e) {{}}
      document.getElementById('rsvpForm').style.display = 'none';
      document.getElementById('rsvpSuccess').style.display = 'block';
    }}
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
    if media_url and re.search(r'\.(mp4|mov|webm)$', media_url, re.I):
        hero_media = f'<video autoplay muted loop playsinline><source src="{media_url}"></video>'
    elif media_url:
        hero_media = f'<img src="{media_url}" alt="Event"/>'
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
    venue_line = f'<div class="hero-venue">📍 {venue}</div>' if venue else ""

    date_card = f'<div class="detail-card"><div class="detail-icon">📅</div><div class="detail-label">Date</div><div class="detail-value">{date_formatted}</div></div>' if raw_date else ""
    venue_card = f'<div class="detail-card"><div class="detail-icon">📍</div><div class="detail-label">Venue</div><div class="detail-value">{venue}</div></div>' if venue else ""
    time_card = f'<div class="detail-card"><div class="detail-icon">🕐</div><div class="detail-label">Time</div><div class="detail-value">{raw_time}</div></div>' if raw_time else ""

    msg = event.get("message", "")
    msg_clean = re.sub(r'https?://\S+', '', msg).strip()
    msg_clean = re.sub(r'\[InviteLink\]', '', msg_clean).strip()
    msg_clean = re.sub(r'\n{3,}', '\n\n', msg_clean)

    return INVITE_TEMPLATE.format(
        EVENT_NAME=event.get("name", "You're Invited"),
        EVENT_ID=event_id,
        HERO_GRADIENT=hero_gradient,
        HERO_MEDIA=hero_media,
        EVENT_DATE_FORMATTED=date_time_str,
        VENUE_LINE=venue_line,
        MESSAGE_BODY=msg_clean,
        DATE_CARD=date_card,
        VENUE_CARD=venue_card,
        TIME_CARD=time_card,
    )


# ── ROUTES ──

@app.get("/")
async def root():
    return FileResponse("../frontend/index.html")

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
    events = load_events()
    event_id = str(uuid.uuid4())[:8]
    base = get_base_url(request)
    invite_url = f"{base}/invite/{event_id}"
    events[event_id] = {
        "id": event_id,
        "name": req.name,
        "type": req.type,
        "date": req.date,
        "time": req.time,
        "venue": req.venue,
        "template": req.template,
        "message": req.message,
        "sender": req.sender,
        "media_url": req.media_url,
        "invite_url": invite_url,
        "created": datetime.now().isoformat(),
        "sent": False,
        "rsvps": [],
    }
    save_events(events)
    return {"event_id": event_id, "invite_url": invite_url}


@app.get("/invite/{event_id}", response_class=HTMLResponse)
async def serve_invite(event_id: str):
    events = load_events()
    event = events.get(event_id)
    if not event:
        raise HTTPException(404, detail="Invite not found")
    return HTMLResponse(build_invite_page(event, event_id))


@app.post("/api/upload-media")
async def upload_media(file: UploadFile = File(...)):
    """Upload a video or image for use in the invite hero."""
    ext = Path(file.filename).suffix.lower()
    allowed = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if ext not in allowed:
        raise HTTPException(400, detail=f"File type {ext} not supported.")
    safe_name = str(uuid.uuid4())[:8] + ext
    dest = UPLOADS_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"url": f"/uploads/{safe_name}", "filename": safe_name}


@app.post("/api/send-test")
async def send_test(req: TestSendRequest):
    number = format_number(req.number)
    if not number:
        raise HTTPException(400, detail="Invalid phone number format")
    try:
        client = twilio_client()
        msg = client.messages.create(body=req.message, from_=req.sender, to=number)
        return {"success": True, "sid": msg.sid, "to": number}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/send-invite")
async def send_invite(req: InviteRequest):
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


@app.post("/api/create-checkout")
async def create_checkout(req: CheckoutRequest, request: Request):
    """Create a Stripe Checkout session or bypass in TEST_MODE."""
    events = load_events()
    if req.event_id not in events:
        raise HTTPException(404, detail="Event not found")

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
        cancel_url=f"{base}/app/?cancelled=1",
        metadata={"event_id": req.event_id},
    )
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
        event_id = session.get("metadata", {}).get("event_id")
        if event_id:
            events = load_events()
            ev = events.get(event_id, {})
            pending = ev.get("pending_send", {})
            if pending:
                await fire_sms_blast(
                    guests=pending.get("guests", []),
                    message=pending.get("message", ""),
                    sender=pending.get("sender", "InviteForge"),
                    event_id=event_id,
                )

    return {"received": True}


@app.post("/api/rsvp/{event_id}")
async def handle_rsvp(event_id: str, data: RSVPRequest):
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
    events[event_id]["rsvps"].append(rsvp)
    save_events(events)
    return {"success": True, "message": "RSVP received"}


@app.get("/api/event/{event_id}/rsvps")
async def get_rsvps(event_id: str):
    events = load_events()
    event = events.get(event_id)
    if not event:
        raise HTTPException(404, detail="Event not found")
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

@app.get("/api/event/{event_id}/status")
async def get_event_status(event_id: str):
    events = load_events()
    event = events.get(event_id)
    if not event:
        raise HTTPException(404, detail="Event not found")
    return {
        "event_id": event_id,
        "sent": event.get("sent", False),
        "sent_count": event.get("sent_count", 0),
        "failed_count": event.get("failed_count", 0),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
