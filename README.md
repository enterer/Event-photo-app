# Event photo platform

Multi-event version of the wedding selfie photo app, for photographers and event businesses. See `docs/phase1-design.md` for the full design and build order.

**This repo never deploys to eu-west-1.** That region runs the wedding (`wedding-photos` stack, separate repo). The template refuses eu-west-1, and `scripts/deploy.sh` refuses the wedding region and stack name.

## Status: week 2

- Sign-in, studios, events (dashboard)
- Guest page `guest.html?e=<slug>`: event code, selfie, public photos plus the ones the guest is in
- Photographer page `upload.html?e=<slug>#k=<upload key>`: folder upload; "Photos for everyone" and "Private photos" folders are respected, everything else is sorted by AI (Claude Haiku 4.5 on Bedrock, us-east-1)
- Processor: resize, host recognition (once host faces exist), AI sorting with the event's rules, per-event face collection `ev-<eventId>`
- Guest codes and upload keys are shown once when an event is created and stored only as hashes

## Deploy (Git Bash)

```bash
export PATH="/c/Users/fiyin/AppData/Local/Programs/Python/Python312:/c/Users/fiyin/AppData/Local/Programs/Python/Python312/Scripts:$PATH"
cd ~/Event-photo-app
./scripts/deploy.sh
```

Settings (region `us-east-1`, stack `eventphotos-dev`, tags) are in `samconfig.toml`, so there are no prompts beyond confirming the change set.

## Dev dashboard

`frontend/dashboard/` holds the photographer dashboard (`index.html`), the guest page (`guest.html`) and the photographer upload page (`upload.html`), all sharing `config.js`. It talks to Cognito directly, with no extra libraries.

1. Fill in `frontend/dashboard/config.js` from the stack outputs (`ApiUrl`, `UserPoolClientId`).
2. Host it with Amplify (us-east-1): new app → GitHub → `Event-photo-app`, branch `main`, not a monorepo; `amplify.yml` publishes `frontend/dashboard`.
3. Open the Amplify link, create an account, verify with the emailed code, set up your studio, create an event.

Cognito's default email sender allows about 50 emails a day, which is fine for dev.
