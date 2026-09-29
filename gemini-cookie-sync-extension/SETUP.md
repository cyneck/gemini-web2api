# Gemini Cookie Sync Setup

Short guide for getting a signed-in Gemini session into `gemini-web2api`.

## Why an extension and not an iframe

A web page cannot read these cookies, so the console can never grab them on its own:

- `gemini.google.com` answers `X-Frame-Options: DENY`, so it refuses to be embedded.
- Even if it could be embedded, the same-origin policy blocks a page on `127.0.0.1`
  from reading anything inside it.
- The session cookies are `HttpOnly`, so no page script can touch them either.

An extension is different: `chrome.cookies` is a privileged browser API that **can**
read `HttpOnly` cookies. That is the only clean way to automate this.

## Install

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select the `gemini-cookie-sync-extension` folder

## One-click sync (recommended)

1. Start `gemini-web2api` and open its console at `http://127.0.0.1:8081/ui`
2. In Chrome, open [https://gemini.google.com/app](https://gemini.google.com/app),
   sign in, and refresh the page
3. Open the extension popup
4. Check **Local server** (defaults to `http://127.0.0.1:8081`); fill in **API key**
   only if the server has `api_keys` set
5. Click **Sync to local server**

That is it. The cookie is written to disk and the account entry is created or updated.
Reload the console to see the new state.

### How accounts are matched

One Google account = one entry, keyed by its `/u/N` index:

- If the Gemini tab is on `/u/1`, the sync updates the account whose `auth_user` is `1`.
- No match yet? A new account is created.
- Single account with no `/u/N` in the URL? It reuses the default account (`auth_user` empty).

So syncing repeatedly does **not** pile up duplicates.

### What it sends

- `cookie`: the session cookies the server requires
- `xsrf`: `SNlM0e` when the page exposes it. **Optional** — requests work without it,
  so a sync never fails just because XSRF is missing.
- `gemini_bl`: applied separately when the page exposes `cfb2h`

## Manual paste (fallback)

If you would rather not install the extension, the console accepts a paste instead:

1. Open `gemini.google.com`, press `F12`, go to **Application** → **Cookies**
2. Copy the whole cookie string and paste it into the account's cookie box
3. Click **Parse and save cookie**

`xsrf_token` is a separate box: press `Ctrl+U`, copy the entire page source, paste it
there, and click **Parse and save xsrf**. The console extracts `SNlM0e` for you.

## Multiple accounts

Sign in to each Google account in its own tab, note the `/u/0`, `/u/1`, ... in the URL,
and sync once per tab. Requests that come back as `400/401/403/429` automatically fail
over to the next enabled account.

## Keep it secret

The exported session is a real Google login. Do not share it, print it, or commit it to Git.
