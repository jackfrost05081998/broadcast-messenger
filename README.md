# Broadcast Messenger

A multi-user Python web app similar to [Reply'em](https://www.replyem.com). Users sign up, connect their Facebook account with one click, view their Facebook Pages, see contacts from previous Messenger conversations, and send broadcast messages to selected contacts.

**Users do not need to create a Facebook Developer app** — only you (the platform owner) configure one Facebook app for the entire platform.

## Features

- **Facebook-only sign-in** — no email/password; works like Reply'em ("Continue as [Name]")
- **Multi-user** — each Facebook account gets its own isolated dashboard
- **Facebook Pages list** — all Pages the user manages
- **Conversation contacts** — people who previously messaged each Page
- **One-click broadcast** — select contacts and send the same message to all at once
- **Delivery tracking** — per-recipient success/failure after each broadcast

## Tech stack

- Python 3.11+
- FastAPI + Jinja2 templates
- SQLAlchemy (async) + SQLite (swap to PostgreSQL via `DATABASE_URL`)
- Facebook Graph API (Messenger Platform)

## Quick start

### 1. Install dependencies

```bash
cd /Users/alexisdiaz/Documents/real-estate
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set:

- `SECRET_KEY` — long random string for sessions
- `FACEBOOK_APP_ID` — from your Facebook Developer app
- `FACEBOOK_APP_SECRET` — from your Facebook Developer app
- `APP_URL` — `http://localhost:8000` for local dev

### 3. Create your Facebook Developer app (one-time, for the platform)

This is what Reply'em does behind the scenes. **Your end users only click "Connect with Facebook".**

1. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps/) → **Create App**
2. Choose **Other** → Next → **Business** → Next → name it → **Create app**
3. On **Add products to your app**, click **Set up** on:
   - **Facebook Login for Business**
   - **Messenger**
4. OAuth settings (**Facebook Login for Business → Settings**):
   - Enable **Client OAuth login** and **Web OAuth login** → Save
   - **Do not add** `http://localhost:8000/...` in Development mode — Meta allows localhost redirects automatically (you'll see a message saying they don't need to be added)
   - When deploying to production, add your HTTPS callback URL here
5. **App settings → Basic** → add `localhost` to **App domains** → copy **App ID** and **App Secret** into `.env`
6. **App roles → Roles** → add your Facebook account as a **Tester** (required in Development mode)

> **Note:** There is no "Use cases" menu in this app type. You add **Facebook Login for Business** and **Messenger** as products directly. Messenger does not need webhooks configured for this app.

> **Development mode:** Only app admins, developers, and testers can log in. Other users see **"App not active"**. Add them as testers (temporary) or complete App Review and switch to **Live** (see `/go-live` in the running app).

### Public access (allow anyone to sign in)

1. Deploy to HTTPS (e.g. `https://yourdomain.com`)
2. Add Privacy Policy URL in **App settings → Basic**
3. **App Review → Permissions and features** — request `pages_show_list`, `pages_messaging`, `pages_read_engagement`, `pages_manage_metadata` (include screencast demo)
4. Complete **Business Verification** if Meta requires it
5. Add production redirect URI: `https://yourdomain.com/auth/facebook/callback`
6. Toggle app from **In development** → **Live** at the top of the Meta dashboard

Until step 6, add each test user under **App roles → Roles → Testers**.

### 4. Run the server

```bash
python run.py
```

Open [http://localhost:8000](http://localhost:8000)

## Usage flow

1. Click **Sign in** at `/login`
2. Click **Connect with Facebook** — if already logged into Facebook, you'll see **Continue as [your name]**
3. Approve Page access in the Facebook permission dialog
4. Click any **Facebook Page** card to see contacts from past conversations
5. Select recipients, write your message, and click **Send broadcast**

## Messaging compliance

Meta restricts when Pages can message users:

| Type | When to use |
|------|-------------|
| `RESPONSE` | Within 24 hours of the user's last message |
| `MESSAGE_TAG` | Outside 24h, only for approved tag categories (account updates, events, etc.) |

Promotional broadcasts outside these rules may fail or risk Page restrictions. This app exposes both options so you can stay compliant.

## Production deployment (free, no credit card)

See **[DEPLOY.md](DEPLOY.md)** for the full step-by-step guide:

1. Get `DATABASE_URL` from [Neon](https://neon.tech)
2. Push the repo to [GitHub](https://github.com)
3. Deploy on [Render](https://render.com) (set `DATABASE_URL` only)
4. Each user adds the Render OAuth callback to their Meta app and signs in on the setup page

## Project structure

```
app/
├── main.py              # FastAPI app entry
├── config.py            # Settings from .env
├── database.py          # Async SQLAlchemy setup
├── models.py            # User, FacebookPage, Broadcast models
├── auth.py              # Password hashing + JWT sessions
├── facebook.py          # Facebook Graph API client
├── dependencies.py      # Auth middleware
├── routes/
│   ├── auth_routes.py       # Login / register
│   ├── facebook_routes.py   # OAuth connect / callback
│   └── dashboard_routes.py  # Pages, contacts, broadcast
└── templates/           # HTML UI
```

## License

MIT
