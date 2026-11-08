# Owner panel

A private web control panel for FlipScout, served by `tracker run` itself. Only you (the owner)
can log in. It shows everything the Discord commands show, laid out for a big screen, plus
history charts and the audit log.

## Set it up (once)

1. Run this in your own terminal (not through anyone else: it shows your login key once):

   ```bash
   uv run tracker panel setup
   ```

   Scan the QR code with an authenticator app (Google Authenticator, Microsoft Authenticator,
   1Password, Bitwarden, Aegis…), then type the 6-digit code it shows. The secret is saved to
   `.env` as `PANEL_TOTP_SECRET` only after the code matches, so you can't lock yourself out.
2. Restart `uv run tracker run`. The log prints `Owner panel: http://127.0.0.1:8765`.
3. Open http://127.0.0.1:8765 and log in with a code from the app.

Lost your phone? Run `uv run tracker panel setup --rotate` on the server, then restart.
Check the configuration any time with `uv run tracker panel status` (it never prints the secret).

## Pages

| Page | What it shows |
|---|---|
| **Overview** | Credible flips, active deals, events, uptime; stock counts per store region; valuation mix; scheduler jobs (runs, failures, next run); which sources are configured |
| **Opportunities** | Four views: **credible flips** (real evidence only), **sold-out market** (store sold out; eBay asking prices are the live market; restock candidates), **hyped in stock** (in stock but listed far above retail; asking prices only), and everything valued |
| **Watchlist** | Everything you track in one place. Add by SKU, name, or any supported store link; pick alert types per item (back in stock, Early Access, price drop, deal, new, restock date, sold out); set a target price; mute; change check priority; bulk mute or remove. **Rules** ping you for anything matching text, category, region, price, or credible profit, including products that don't exist yet, with a live preview of what they cover. Also: "ping me for every important alert", and members' watches (remove only). |
| **Product** | Verdict, profit breakdown, every region's status and price, restock count, sell-out speed, out-of-stock duration, daily eBay price and seller charts, recent events |
| **Radar** | Every event, newest first, filterable (deals, restocks, new, Early Access, sold out, price) |
| **Servers** | Approve or block Discord servers (blocking makes the bot leave) |
| **Audit log** | Every admin action from Discord or the panel, including panel logins and failed attempts |

New deal alerts pop up as toasts while the panel is open.

Watches and rules you make in the panel belong to your Discord account (the owner) and ping you in your home server, the same as `/watch`. If `DISCORD_OWNER_IDS` isn't set, the panel uses the owner the bot learns from Discord.

## Reach it from your phone or another PC (Tailscale)

The panel only listens on `127.0.0.1`. Never open a router port for it. Instead, on the machine
running FlipScout:

```bash
tailscale serve --bg 8765
```

That publishes it over HTTPS to **your tailnet only**, at
`https://<machine-name>.<tailnet>.ts.net`. Then tell FlipScout to accept that name, and
(recommended) which Tailscale accounts may use it, in `.env`:

```dotenv
PANEL_ALLOWED_HOSTS=flipscout.tail1234.ts.net
PANEL_TAILSCALE_LOGINS=you@example.com
```

Restart, and open the `https://…ts.net` address on any of your devices that's signed in to
Tailscale. You still need an authenticator code.

## Security model

| Layer | What it does |
|---|---|
| **Network** | Binds to localhost only (the setting refuses anything else). Remote access is through `tailscale serve`, so it's never on the public internet. |
| **Identity** | Optional allowlist of Tailscale logins (`Tailscale-User-Login`, set by Tailscale). |
| **Login** | Time-based one-time codes (RFC 6238). ±30 s drift allowed; each code works once (no replay). |
| **Brute force** | 5 wrong codes per client, or 20 overall, in 15 minutes locks logins for the rest of the window. Every failure is audited. |
| **Sessions** | Random 256-bit token in an `HttpOnly`, `Secure`, `SameSite=Strict` `__Host-` cookie. Stored server-side as a SHA-256 hash, expires after 12 h (`PANEL_SESSION_HOURS`), and all sessions end on restart. "Sign out everywhere" ends every session now. |
| **Request forgery** | Writes need a custom `X-FlipScout` header plus a same-origin `Origin`, on top of `SameSite=Strict`. |
| **DNS rebinding** | Unknown `Host` headers are refused before any route runs. |
| **Browser hardening** | Strict CSP (`script-src 'self'`, no inline code, no third-party anything), `frame-ancestors 'none'`, `nosniff`, `no-referrer`, `no-store` on API responses. API docs are disabled. |
| **Secrets** | Never sent to the panel: sources show only "configured" or "off". The TOTP secret is masked in logs like every other secret. |
| **Front end** | Plain HTML, CSS, and JavaScript served from the tracker: no CDN, no npm packages, no build step. Data is rendered as text, never as HTML. |
| **Audit** | Logins, failed logins, logouts, sign-out-everywhere, and server decisions go to the append-only audit log. |

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PANEL_TOTP_SECRET` | (none) | Owner login secret. Set by `tracker panel setup`. The panel is off without it. |
| `PANEL_ENABLED` | `true` | Turn the panel off entirely. |
| `PANEL_PORT` | `8765` | Local port. |
| `PANEL_HOST` | `127.0.0.1` | Must be a loopback address. |
| `PANEL_SESSION_HOURS` | `12` | Session lifetime (0.25–168). |
| `PANEL_ALLOWED_HOSTS` | (none) | Extra `Host` names, e.g. your Tailscale name. |
| `PANEL_TAILSCALE_LOGINS` | (none) | Tailscale accounts allowed through `tailscale serve`. |
| `PANEL_SECURE_COOKIES` | `true` | Keep on. Browsers accept secure cookies on `http://127.0.0.1` and over Tailscale HTTPS. |

## Not yet

- **Windows app (Tauri):** a native window, tray icon, and Windows notifications wrapping this
  same panel. It needs the Rust toolchain, which isn't installed yet.
- **Docker:** inside a container, localhost isn't reachable from the host, so the panel
  is for direct (`uv run`) installs until the Linux deployment step adds a container-safe
  binding.
- Ledger and P&L, budgets, update and rollback, and logs pages (Phase 5 and the deployment
  phase).
- Passkey login as an alternative to codes.
