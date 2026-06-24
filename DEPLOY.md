# Full deploy guide (free — no credit card)

Deploy **Broadcast Messenger** online using:

- **[GitHub](https://github.com)** — stores your code (free)
- **[Neon](https://neon.tech)** — PostgreSQL database (free, always on)
- **[Render](https://render.com)** — runs the app (free, sleeps when idle)

**Total cost: $0.** You do not need `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` on Render. Each person who signs in pastes **their own** Meta app credentials on the setup page; those are saved per user in Neon.

**Time:** about 30–45 minutes the first time.

---

## Before you start

You need:

- A computer with this project folder (e.g. `/Users/you/Documents/real-estate`)
- A **GitHub account** ([github.com/signup](https://github.com/signup))
- A **Neon account** ([neon.tech](https://neon.tech))
- A **Render account** ([render.com](https://render.com))

Install Git if you do not have it:

```bash
git --version
```

If that fails on Mac:

```bash
xcode-select --install
```

---

## Part 1 — Get `DATABASE_URL` from Neon

Neon hosts your production database. Render connects to it using a connection string called `DATABASE_URL`.

### Step 1.1 — Create a Neon account

1. Open **[neon.tech](https://neon.tech)**.
2. Click **Sign up** (GitHub login is fastest).
3. Complete sign-up. **No credit card required.**

### Step 1.2 — Create a project

1. After login, click **Create a project** (or **New Project**).
2. Fill in:
   - **Project name:** `broadcast-messenger` (any name is fine)
   - **Region:** pick one close to you (e.g. `US East`, `Singapore`)
   - **Postgres version:** leave the default (16 or 17)
3. Click **Create project**.

Neon creates a database named `neondb` and a default branch `main`.

### Step 1.3 — Copy the connection string

1. On the project **Dashboard**, find the **Connection details** panel (or click **Connect**).
2. Under **Connection string**, choose:
   - **Role:** `neondb_owner` (default)
   - **Database:** `neondb`
   - **Branch:** `main`
3. Select format **URI** or **Connection string** (not JDBC).
4. Click **Copy** (or highlight and copy).

It looks like this:

```
postgresql://neondb_owner:AbCdEf123456@ep-cool-name-12345678.us-east-2.aws.neon.tech/neondb?sslmode=require
```

**Important:**

- Keep the `?sslmode=require` at the end. The app needs SSL to connect.
- This string contains your **database password**. Treat it like a secret.
- Do **not** commit it to GitHub. You will paste it only into Render’s Environment settings.

### Step 1.4 — Save it somewhere safe

Paste the string into a notes app or password manager labeled **Neon DATABASE_URL**. You will use it in Part 3.

### Step 1.5 — Pooled vs direct connection (optional)

Neon may show two connection strings:

| Type | When to use |
|------|-------------|
| **Direct** | Fine for this app on Render free tier |
| **Pooled** (`-pooler` in hostname) | Better if you run many concurrent connections |

Either works. If unsure, use the **direct** URI with `?sslmode=require`.

### Step 1.6 — Verify the string format

Checklist:

- [ ] Starts with `postgresql://`
- [ ] Contains `@ep-....neon.tech/`
- [ ] Ends with `neondb?sslmode=require` (database name + SSL)
- [ ] You copied the full string including the password

The app converts `postgresql://` to the async driver automatically — paste it **exactly** as Neon gives it.

---

## Part 2 — Push your code to GitHub

Render deploys from GitHub. Your repo must be online before Part 3.

### Step 2.1 — Make sure secrets are not committed

This project’s `.gitignore` already excludes:

- `.env` (local secrets)
- `*.db` (local SQLite files)

**Never commit `.env`.** It may contain Facebook secrets and local keys.

Check:

```bash
cd /path/to/real-estate
cat .gitignore
```

If you ever accidentally committed `.env`, remove it from Git tracking:

```bash
git rm --cached .env
git commit -m "Stop tracking .env"
```

### Step 2.2 — Create a new repository on GitHub

1. Go to **[github.com/new](https://github.com/new)** (log in first).
2. Set:
   - **Repository name:** `broadcast-messenger` (or any name)
   - **Description:** optional
   - **Public** or **Private:** either works with Render
   - **Do not** check “Add a README” (you already have code locally)
   - **Do not** add `.gitignore` or license (you already have them)
3. Click **Create repository**.

GitHub shows a page with setup commands. Keep it open.

### Step 2.3 — Initialize Git locally (skip if already a repo)

If this folder is **not** yet a Git repo:

```bash
cd /path/to/real-estate
git init
git branch -M main
```

If `git status` already works, skip `git init`.

### Step 2.4 — Stage and commit your files

```bash
cd /path/to/real-estate
git status
```

Review the list. Confirm `.env` and `*.db` files are **not** listed.

Stage everything that should be deployed:

```bash
git add .
git status
```

Commit:

```bash
git commit -m "Initial commit — Broadcast Messenger"
```

If Git asks for your name/email the first time:

```bash
git config user.email "you@example.com"
git config user.name "Your Name"
```

(Use your real GitHub email if you want commits linked to your account.)

### Step 2.5 — Connect to GitHub and push

Replace `YOUR_GITHUB_USERNAME` and `YOUR_REPO_NAME` with yours:

```bash
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME.git
```

If `origin` already exists but points to the wrong URL:

```bash
git remote set-url origin https://github.com/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME.git
```

Push:

```bash
git push -u origin main
```

**Authentication:**

- GitHub no longer accepts account passwords for Git over HTTPS.
- When prompted, use a **Personal Access Token** as the password, or sign in via **GitHub CLI** / **GitHub Desktop**.

**Create a token (one-time):**

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
2. **Generate new token (classic)**
3. Scopes: check **`repo`**
4. Copy the token and paste it when `git push` asks for a password

**Alternative — GitHub CLI:**

```bash
brew install gh
gh auth login
git push -u origin main
```

### Step 2.6 — Confirm on GitHub

1. Refresh your repo page on GitHub.
2. You should see folders like `app/`, files like `Dockerfile`, `render.yaml`, `requirements.txt`.
3. Confirm **`.env` is not visible** in the file list.

### Step 2.7 — Push updates later

After you change code locally:

```bash
cd /path/to/real-estate
git add .
git commit -m "Describe your change"
git push
```

Render redeploys automatically when you push to `main` (after Part 3 is set up).

---

## Part 3 — Deploy on Render

### Step 3.1 — Create a Render account

1. Go to **[render.com](https://render.com)** → **Get Started**.
2. Sign up with **GitHub** (easiest — Render can read your repos).
3. Authorize Render when GitHub asks. **Free tier does not require a credit card.**

### Step 3.2 — Deploy with Blueprint

1. In Render dashboard, click **New +** → **Blueprint**.
2. **Connect a repository** → select your `broadcast-messenger` repo.
3. Render reads `render.yaml` from the repo and shows a service named `broadcast-messenger`.
4. You will be prompted for environment variables.

### Step 3.3 — Set environment variables

| Key | Value | Required? |
|-----|--------|-----------|
| `DATABASE_URL` | Paste your **full Neon connection string** from Part 1 | **Yes** |
| `SECRET_KEY` | Leave blank — Render auto-generates it | Auto |
| `FACEBOOK_APP_ID` | Leave blank | No |
| `FACEBOOK_APP_SECRET` | Leave blank | No |

**Do not set `APP_URL`.** Render sets `RENDER_EXTERNAL_URL`; the app uses that for OAuth redirects.

Click **Apply** or **Create**.

### Step 3.4 — Wait for the deploy

1. Render builds a Docker image and starts the service.
2. First deploy takes **5–10 minutes**.
3. Status should become **Live** (green).

If deploy fails, open **Logs** in the Render dashboard and scroll to the red error at the bottom.

### Step 3.5 — Copy your public URL

Render assigns a URL like:

```
https://broadcast-messenger.onrender.com
```

Copy it. You need it for Meta (Part 4) and testing (Part 5).

### Step 3.6 — Health check

In a terminal:

```bash
curl https://YOUR-SERVICE.onrender.com/health
```

Expected response:

```json
{"status":"ok"}
```

If you get a timeout, the free service may be waking up — wait 30–60 seconds and try again.

### Step 3.7 — Manual deploy (if Blueprint is unavailable)

1. **New +** → **Web Service**
2. Connect your GitHub repo
3. Settings:
   - **Language / Runtime:** Docker
   - **Branch:** `main`
   - **Plan:** Free
   - **Health Check Path:** `/health`
4. **Environment** → add `DATABASE_URL` (Neon string)
5. **Create Web Service**

---

## Part 4 — Meta / Facebook (per user)

The **site owner** (you) and **every person who signs in** need their **own** Meta Developer app.

### Step 4.1 — Create a Meta Developer app

1. Go to **[developers.facebook.com](https://developers.facebook.com/)**.
2. **My Apps** → **Create App**.
3. Choose a use case (e.g. **Other** → **Business** or app that supports Login).
4. Name the app and complete creation.

### Step 4.2 — Add Facebook Login

1. In the app dashboard, **Add product**.
2. Find **Facebook Login** → **Set up** (or **Facebook Login for Business**).

### Step 4.3 — Configure app domains and redirect URI

Use your **Render URL** from Part 3. Example host: `broadcast-messenger.onrender.com`

1. **App settings → Basic**
   - **App domains:** `broadcast-messenger.onrender.com` (hostname only, no `https://`)
   - **Privacy Policy URL:** required before Live mode (any public URL you control)
2. **Facebook Login → Settings** (or Login for Business → Settings)
   - **Valid OAuth Redirect URIs** — add exactly:
     ```
     https://broadcast-messenger.onrender.com/auth/facebook/callback
     ```
   - Replace with **your** Render URL if different.
3. **Save changes.**

### Step 4.4 — Copy App ID and App Secret

1. **App settings → Basic**
2. Copy **App ID** (numeric, ~15 digits)
3. Click **Show** next to **App Secret** and copy it

You will paste these on your site’s sign-in page — **not** into Render env vars.

### Step 4.5 — Sign in on your deployed site

1. Open `https://YOUR-SERVICE.onrender.com`
2. First visit after idle may take up to ~60 seconds (Render free tier waking)
3. You land on **Sign in with your Meta Developer app**
4. Paste **App ID** and **App Secret**
5. Confirm the **OAuth redirect URI** shown on the page matches what you added in Meta
6. Click **Save & sign in with Facebook**
7. Approve permissions for your Facebook account and Pages
8. You should reach the **Dashboard** with your Pages listed

### Step 4.6 — Other users (multi-user)

Each new user:

1. Opens the same site URL
2. Pastes **their own** App ID + Secret
3. Adds the **same redirect URI** to **their** Meta app:
   ```
   https://YOUR-SERVICE.onrender.com/auth/facebook/callback
   ```
4. Signs in — their credentials are stored on **their** user row in Neon only

---

## Part 5 — Verify everything works

| Check | How |
|-------|-----|
| Database connected | Render **Logs** show no `database` / `connection` errors on startup |
| Health endpoint | `curl https://YOUR-SERVICE.onrender.com/health` → `{"status":"ok"}` |
| Sign-in page | `/setup/app` loads with redirect URI shown |
| OAuth | Save & sign in → Facebook dialog → dashboard |
| Pages | Dashboard lists your Facebook Pages |
| Persistence | Log out, sign in again — still works (creds in Neon) |
| Multi-user | Second person uses different App ID/Secret — separate accounts |

---

## Part 6 — Local development (unchanged)

On your Mac, still use:

```bash
cd /path/to/real-estate
python3 run.py
```

Open `http://localhost:8000`. The same sign-in / setup flow applies. Locally, saving also writes `.env` for convenience.

---

## Troubleshooting

### Neon / `DATABASE_URL`

**“Database connection failed” in Render logs**

- Paste the **full** Neon URI including `?sslmode=require`
- No extra spaces at start or end
- In Render → your service → **Environment** → edit `DATABASE_URL` → **Save** (triggers redeploy)

**Wrong password**

- Neon dashboard → **Connection details** → reset password if needed → copy new URI

### GitHub / Git push

**“Permission denied” or “Authentication failed”**

- Use a Personal Access Token instead of your GitHub password
- Or run `gh auth login`

**“remote origin already exists”**

```bash
git remote set-url origin https://github.com/YOUR_USER/YOUR_REPO.git
```

**Pushed `.env` by mistake**

```bash
git rm --cached .env
echo ".env" >> .gitignore
git commit -m "Remove .env from repo"
git push
```

Rotate any secrets that were exposed (Neon password reset, new Facebook App Secret, new `SECRET_KEY` on Render).

### Render

**Slow first page load**

- Free tier was asleep. Wait 30–60 seconds and refresh.

**Build failed**

- Render → **Logs** during build phase
- Test locally: `docker build -t broadcast .`

**Service keeps restarting**

- Check **Logs** for Python traceback
- Usually bad `DATABASE_URL` or missing env

### Facebook / OAuth

**“URL blocked” or redirect mismatch**

- Redirect URI in Meta must match **exactly**:
  `https://YOUR-SERVICE.onrender.com/auth/facebook/callback`
- No trailing slash difference; must be `https`

**“App not active”**

- App is in Development mode — add your Facebook account as a **Tester** under App roles → Roles, or complete App Review and go **Live**

**Sign-in works locally but not on Render**

- Meta app must include the **Render** callback URL, not only `localhost`

---

## Quick reference

| Item | Where |
|------|--------|
| Neon `DATABASE_URL` | Neon dashboard → Connect → copy URI |
| GitHub repo | `https://github.com/YOU/broadcast-messenger` |
| Render URL | Render dashboard → Web Service → top of page |
| OAuth redirect | `https://YOUR-SERVICE.onrender.com/auth/facebook/callback` |
| Render env vars | Service → **Environment** → `DATABASE_URL` only (required) |
| User Meta creds | Setup page on your site → saved in Neon per user |

---

## Optional: Fly.io

Files `fly.toml` and `deploy.sh` target Fly.io, which often requires a payment method. Skip Fly if you are on a $0 budget; use Neon + Render above.
