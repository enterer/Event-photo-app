# Event photo platform

Multi-event version of the wedding selfie photo app, for photographers and event businesses. See `docs/phase1-design.md` for the full design and build order.

**This repo never deploys to eu-west-1.** That region runs the wedding (`wedding-photos` stack, separate repo). The template refuses eu-west-1, and `scripts/deploy.sh` refuses the wedding region and stack name.

## Status: week 3 + landing page + live photos + event owners

- **Live photos**: an event setting. Guests' galleries check for new photos every 30 seconds; guests can opt in to "new photos of me", which keeps one face signature (`live_<id>` in the event's collection) until they stop, their session ends or the event's photos are deleted
- **Approve before guests see them**: an event setting. New photos go to `events/<id>/held/` until approved in the Photos tab
- **Event owners**: the studio invites the couple or host by email (Event owners tab, with a WhatsApp message). They sign up with that email and see only that event: event page, photos and approval, live settings, rules, hosts and stats; no deleting, no photographer link, no branding. Hosts can also sign up on their own ("I'm hosting my own event")
- **Event page**: names, venue, welcome note, schedule and dress code, shown on the guest page
- Landing page `index.html` (the first page visitors see): what the product does, privacy comparison, photographer features, pricing (placeholder prices), and Sign in / Get started buttons into the dashboard
- Sign-in, studios, events (dashboard), with studio branding (name, logo, booking link, Instagram) shown to guests as "Photos by"
- Event page tabs: **Share** (QR code with optional guest code, new photographer link or guest code), **Photos** (review queue: share or make private, ask AI again, delete one, re-sort failed ones, delete all), **Sorting rules**, **Hosts** (reference faces; photos with every host are shared automatically), **Stats** (selfies, guests found, downloads, views, photo counts)
- Guest page `guest.html?e=<slug>` and photographer page `upload.html?e=<slug>#k=<upload key>`
- Processor: resize, host recognition, AI sorting with the event's rules, per-event face collections, re-sort and delete-all jobs
- `frontend/dashboard/qrcode.js` is qrcode-generator 1.4.4 by Kazuhiko Arase (MIT licence)

## Deploy (Git Bash)

```bash
export PATH="/c/Users/fiyin/AppData/Local/Programs/Python/Python312:/c/Users/fiyin/AppData/Local/Programs/Python/Python312/Scripts:$PATH"
cd ~/Event-photo-app
./scripts/deploy.sh
```

Settings (region `us-east-1`, stack `eventphotos-dev`, tags) are in `samconfig.toml`, so there are no prompts beyond confirming the change set.

## Dev dashboard

`frontend/dashboard/` holds the landing page (`index.html`), the photographer dashboard (`app.html`; `app.html#signup` opens account creation), the guest page (`guest.html`) and the photographer upload page (`upload.html`), all sharing `config.js`. It talks to Cognito directly, with no extra libraries.

1. Fill in `frontend/dashboard/config.js` from the stack outputs (`ApiUrl`, `UserPoolClientId`).
2. Host it with Amplify (us-east-1): new app → GitHub → `Event-photo-app`, branch `main`, not a monorepo; `amplify.yml` publishes `frontend/dashboard`.
3. Open the Amplify link, create an account, verify with the emailed code, set up your studio, create an event.

Cognito's default email sender allows about 50 emails a day, which is fine for dev.
