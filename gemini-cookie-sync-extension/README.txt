Gemini Cookie Sync v1.1

Purpose:
- Read cookies for the current Google/Gemini session (chrome.cookies can read HttpOnly).
- Extract the XSRF token named SNlM0e from the Gemini page.
- Extract gemini_bl from cfb2h or from page requests when available.
- Push everything straight into a running gemini-web2api server, or export
  gemini-auth.json as an offline fallback.

Installation:
1. Open chrome://extensions
2. Enable Developer mode
3. Click Load unpacked
4. Select this folder

One-click sync:
1. Start gemini-web2api and open http://127.0.0.1:8081/ui
2. Open https://gemini.google.com/app, sign in, and refresh the page
3. Open this extension, confirm the server address, click "Sync to local server"

The account is matched by the /u/N index in the Gemini tab URL:
- /u/1 updates the account with auth_user=1
- no match yet -> a new account is created
- no /u/N at all -> the default account is reused
Syncing twice therefore does not create duplicates.

Notes:
- XSRF (SNlM0e) is optional; syncing succeeds without it.
- If the server has api_keys set, fill in the API key field first.
- "Download gemini-auth.json" under More is the offline fallback.

Security:
The synced data represents the real Google session and must be treated as secret.
Do not send it, print it, or commit it to Git. Nothing leaves this machine except
the request to your own local server.
