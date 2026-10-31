# Security

Please report vulnerabilities privately to info@whiteeagles.sk or through GitHub's "Report a vulnerability" on this repository. Do not open public issues for security problems. We answer within a few days.

## How the service is protected

- The settings page is static (GitHub Pages) with a strict Content-Security-Policy: only its own scripts, no inline code, no third-party scripts, fonts or trackers. All text, including anything from the server, is inserted with `textContent`.
- Sign-in goes through the Telegram bot. The page gets a public nonce and a private secret; the bot sends a one-time 6-digit code to the chat that opened the link. The server accepts the code only together with the secret held by the browser that started the sign-in, at most 5 attempts within 10 minutes. A leaked or planted link alone gives nothing.
- Session keys are random 256-bit tokens. The database keeps only SHA-256 hashes of session keys, nonces and secrets, and an HMAC of login codes.
- The API accepts requests only from the page's origin (CORS), checks every field against a strict schema that rejects unknown keys and out-of-range values, limits body size, and rate-limits per IP, per account and per Telegram chat. Error responses never echo input.
- Hotel links are accepted only as `https://www.booking.com/hotel/...` pages and are never fetched by the server.
- The Telegram webhook requires the secret header and is reachable only from Telegram's networks.
- The gateway runs in a container as an unprivileged user with a read-only root filesystem, no Linux capabilities, `no-new-privileges`, and CPU, memory and process limits. It listens on localhost only; nginx terminates TLS in front of it. fail2ban bans IPs that flood the rate limits or probe for paths.
- Dependencies are pinned with hashes. CI runs tests, ruff security rules, bandit, pip-audit and CodeQL; Dependabot proposes updates.

## What we store

A Telegram chat id, the interface language and the settings the user saved. No names, phone numbers or contacts. Users can delete everything at once on the page or with /delete in the bot.
