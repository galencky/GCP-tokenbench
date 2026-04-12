# Deploying GCP Token Bench to Vercel

Complete guide from zero to production. Follow each section in order.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Create a Google Cloud Project](#2-create-a-google-cloud-project)
3. [Set Up Google OAuth (Sign-In)](#3-set-up-google-oauth-sign-in)
4. [Create a GCP Service Account (for Vertex AI)](#4-create-a-gcp-service-account-for-vertex-ai)
5. [Install Vercel CLI & Initial Deploy](#5-install-vercel-cli--initial-deploy)
6. [Add Neon Postgres via Vercel Dashboard](#6-add-neon-postgres-via-vercel-dashboard)
7. [Set Environment Variables](#7-set-environment-variables)
8. [Deploy to Production](#8-deploy-to-production)
9. [Update Google OAuth with Final URL](#9-update-google-oauth-with-final-url)
10. [Verify Everything Works](#10-verify-everything-works)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites

Make sure you have:

- [ ] A Google account
- [ ] A credit card linked to Google Cloud (free tier is sufficient, but billing must be enabled for Vertex AI)
- [ ] Node.js installed (`node -v` to check — needed for Vercel CLI)
- [ ] Python 3.10+ installed (`python3 --version` to check)

---

## 2. Create a Google Cloud Project

If you already have a GCP project, skip to Step 3.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Click the project dropdown at the top left → **New Project**
3. Name it something like `tokenbench`
4. Click **Create**
5. Make sure the new project is selected in the dropdown
6. **Enable billing** for the project:
   - Go to **Billing** in the left sidebar
   - Link a billing account (you won't be charged unless you exceed free tier)
7. **Enable the Vertex AI API**:
   - Go to **APIs & Services** → **Library**
   - Search for `Vertex AI API`
   - Click **Enable**

> **Note your Project ID** — you'll see it on the project dashboard (e.g., `tokenbench-12345`). You'll need it later.

---

## 3. Set Up Google OAuth (Sign-In)

This lets users log into Token Bench with their Google account.

### 3a. Configure the OAuth Consent Screen

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Navigate to **APIs & Services** → **OAuth consent screen**
3. Click **Get Started** (or **Configure Consent Screen**)
4. Fill in:
   - **App name**: `GCP Token Bench`
   - **User support email**: your email
   - **Audience**: choose **External** (anyone with a Google account can sign in)
5. Click **Next** / **Continue**
6. On the **Scopes** step:
   - Click **Add or Remove Scopes**
   - Search for and add these scopes:
     - `openid`
     - `email`
     - `profile`
   - Click **Update** → **Save and Continue**
7. On the **Test users** step:
   - If your app is in "Testing" mode, you must add the Google email addresses of anyone who will test the app
   - Click **Add Users** → enter your email → **Save**
   - (Once you publish the app later, anyone can sign in)
8. Click **Save and Continue** → **Back to Dashboard**

### 3b. Create OAuth Client ID

1. Go to **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **OAuth client ID**
3. Set:
   - **Application type**: `Web application`
   - **Name**: `Token Bench Web`
4. Under **Authorized JavaScript origins**, add:
   ```
   http://localhost:5050
   ```
   (You'll add the Vercel URL later in Step 9)
5. Leave **Authorized redirect URIs** empty (not needed for Google Identity Services)
6. Click **Create**
7. A dialog appears with your **Client ID** and **Client Secret**

> **Copy the Client ID** — it looks like:
> ```
> 123456789012-abcdefghijklmnop.apps.googleusercontent.com
> ```
> Save this somewhere. You don't need the Client Secret for this app.

---

## 4. Create a GCP Service Account (for Vertex AI)

This is the JSON key file that users upload into Token Bench to access Gemini models.

1. Go to **IAM & Admin** → **Service Accounts**
2. Click **+ Create Service Account**
3. Set:
   - **Name**: `tokenbench-vertex`
   - **Description**: `Service account for Token Bench Vertex AI access`
4. Click **Create and Continue**
5. Under **Grant this service account access to project**, add the role:
   - `Vertex AI User` (search for it in the dropdown)
6. Click **Continue** → **Done**
7. Click on the service account you just created
8. Go to the **Keys** tab
9. Click **Add Key** → **Create new key** → **JSON** → **Create**
10. A `.json` file downloads — **this is your service account key**

> **Keep this file safe.** You (or your users) will upload it into Token Bench after logging in. Never commit it to git.

---

## 5. Install Vercel CLI & Initial Deploy

### 5a. Install Vercel CLI

```bash
npm install -g vercel
```

### 5b. Log In

```bash
vercel login
```

Follow the prompts (email link or GitHub OAuth).

### 5c. Initial Deploy

From the project directory:

```bash
cd "/Users/galen/Desktop/Claude projects/GCP-tokenbench"
vercel
```

It will ask you:

| Prompt | Answer |
|--------|--------|
| Set up and deploy? | `Y` |
| Which scope? | Select your account |
| Link to existing project? | `N` |
| Project name? | `gcp-tokenbench` (or your choice) |
| In which directory is your code located? | `./` (press Enter) |

This first deploy will fail — that's expected. We need to add the database and env vars first. But it creates the project on Vercel, which we need for the next step.

---

## 6. Add Neon Postgres via Vercel Dashboard

This is the easiest part — Vercel has Neon built in. No external accounts needed.

1. Go to [vercel.com/dashboard](https://vercel.com/dashboard)
2. Click on your **gcp-tokenbench** project
3. Go to the **Storage** tab
4. Click **Create Database**
5. Select **Neon Serverless Postgres**
6. Choose a region (pick one close to your users, e.g., `us-east-1`)
7. Click **Create**

Vercel automatically creates the database AND sets the `POSTGRES_URL` environment variable for you. You can verify this:

8. Go to **Settings** → **Environment Variables**
9. You should see `POSTGRES_URL` (and related `POSTGRES_*` vars) already populated

> **That's it.** No connection strings to copy, no users to create, no network rules to configure. Vercel handles all of it.

---

## 7. Set Remaining Environment Variables

Generate your secrets first. Open a terminal and run:

```bash
# JWT Secret (random 64-character string)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Encryption Key (Fernet key for encrypting service account keys at rest)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Save both outputs.** Now set them on Vercel. For each command, select **Production**, **Preview**, and **Development** when prompted (press `a` to select all).

```bash
vercel env add JWT_SECRET
```
> Paste the 64-char hex string

```bash
vercel env add ENCRYPTION_KEY
```
> Paste the Fernet key

```bash
vercel env add GOOGLE_CLIENT_ID
```
> Paste the OAuth Client ID from Step 3b

```bash
vercel env add DEV_LOGIN
```
> Type: `false`

```bash
vercel env add ALLOWED_ORIGINS
```
> Type your Vercel URL, e.g.: `https://gcp-tokenbench.vercel.app`
> (Run `vercel ls` to see the URL Vercel assigned)

### Verify your env vars

```bash
vercel env ls
```

You should see `POSTGRES_URL` (auto-set by Neon) plus the 5 you just added.

---

## 8. Deploy to Production

```bash
vercel --prod
```

Wait for the build to complete. Vercel will print the production URL, e.g.:

```
https://gcp-tokenbench.vercel.app
```

---

## 9. Update Google OAuth with Final URL

Now that you have your production URL:

1. Go back to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Credentials**
2. Click on your OAuth Client ID (`Token Bench Web`)
3. Under **Authorized JavaScript origins**, click **+ Add URI** and add:
   ```
   https://gcp-tokenbench.vercel.app
   ```
   (Use your actual Vercel URL)
4. Click **Save**

> Changes take effect immediately, but may take up to 5 minutes to propagate.

---

## 10. Verify Everything Works

1. Open your Vercel URL in a browser
2. You should see the login screen with a **Sign in with Google** button
3. Click it and sign in with a Google account that's in your test users list (Step 3a)
4. After signing in, you'll see the key upload screen
5. Upload the service account JSON key from Step 4
6. You should now see the chat interface
7. Select a model, type a message, and send it
8. After getting a response, refresh the page — your chat should persist in the sidebar

### Checklist

- [ ] Google Sign-In works
- [ ] Service account key upload succeeds
- [ ] Chat sends and receives streaming responses
- [ ] Chat history persists after page refresh
- [ ] Creating multiple chats works (each gets its own entry)
- [ ] Dark/light theme toggle works

---

## 11. Troubleshooting

### "Google Sign-In not configured"
- Check that `GOOGLE_CLIENT_ID` is set correctly in Vercel env vars
- Redeploy with `vercel --prod` after changing env vars

### "Failed to verify Google token" / Sign-In popup closes with no effect
- Make sure your Vercel URL is in the **Authorized JavaScript origins** in Google Cloud Console
- If your app is in "Testing" mode, make sure your Google email is added as a test user
- Check browser console for CSP or CORS errors

### "Access to this API has been restricted"
- The Google account signing in must be added as a test user in the OAuth consent screen, OR you need to publish the app (set status to "In production" in the consent screen settings)

### Chat history disappears after refresh
- Verify `POSTGRES_URL` is set (should be auto-set by Neon integration)
- Check Vercel logs: `vercel logs --follow`
- Go to Vercel dashboard → Storage → check that Neon database is connected

### "502 Bad Gateway" or "Function timed out"
- The `google-cloud-aiplatform` package is large. If the Lambda exceeds 50MB:
  - The `vercel.json` already sets `maxLambdaSize: 50mb`
  - If it still fails, you may need to slim dependencies (the app only directly uses `google-auth`, `flask`, `requests`, `PyJWT`, `cryptography`, and `psycopg2-binary`)

### Cold start is slow (5-10 seconds on first request)
- This is normal on Vercel's free tier for Python functions
- Subsequent requests within ~15 minutes will be fast
- Upgrade to Vercel Pro for better cold start performance

### Redeploying after code changes

```bash
git add -A && git commit -m "your message"
vercel --prod
```

Or connect your GitHub repo to Vercel for automatic deploys on push:
1. Go to [vercel.com/dashboard](https://vercel.com/dashboard)
2. Select your project → **Settings** → **Git**
3. Connect your GitHub repo
4. Every push to `master` will auto-deploy

---

## Quick Reference

| Item | Where to find it |
|------|-----------------|
| Vercel Dashboard | [vercel.com/dashboard](https://vercel.com/dashboard) |
| Vercel Logs | `vercel logs` or dashboard → Deployments → Functions |
| Neon Database | Vercel dashboard → your project → Storage |
| Google Cloud Console | [console.cloud.google.com](https://console.cloud.google.com/) |
| OAuth Consent Screen | GCP Console → APIs & Services → OAuth consent screen |
| OAuth Credentials | GCP Console → APIs & Services → Credentials |

| Command | What it does |
|---------|-------------|
| `vercel` | Deploy to preview |
| `vercel --prod` | Deploy to production |
| `vercel env ls` | List environment variables |
| `vercel env add NAME` | Add an environment variable |
| `vercel env rm NAME` | Remove an environment variable |
| `vercel logs` | View runtime logs |
| `vercel domains add example.com` | Add a custom domain |
