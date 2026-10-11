# Event photo platform

Multi-event version of the wedding selfie photo app, for photographers and event businesses. See `docs/phase1-design.md` for the full design and build order.

**This repo never deploys to eu-west-1.** That region runs the wedding (`wedding-photos` stack, separate repo). The template refuses eu-west-1, and `scripts/deploy.sh` refuses the wedding region and stack name.

## Status: week 1 plus dev dashboard

- Data table, media bucket, Cognito sign-in, HTTP API with a JWT authorizer
- Organisations (create, membership) and events (create, list, view, public guest-page info)
- Event guest codes and upload keys are generated, shown once, and stored only as hashes

## Deploy (Git Bash)

```bash
export PATH="/c/Users/fiyin/AppData/Local/Programs/Python/Python312:/c/Users/fiyin/AppData/Local/Programs/Python/Python312/Scripts:$PATH"
cd ~/Event-photo-app
./scripts/deploy.sh
```

Settings (region `us-east-1`, stack `eventphotos-dev`, tags) are in `samconfig.toml`, so there are no prompts beyond confirming the change set.

## Dev dashboard

`frontend/dashboard/` is the photographer dashboard (sign up, sign in, studio setup, create and view events). It talks to Cognito directly, with no extra libraries.

1. Fill in `frontend/dashboard/config.js` from the stack outputs (`ApiUrl`, `UserPoolClientId`).
2. Host it with Amplify: new app → GitHub → `Event-photo-app`, branch `main`, monorepo root `frontend/dashboard` (it reads `amplify.yml`). Use the us-east-1 console so it sits beside the stack.
3. Open the Amplify link, create an account, verify with the emailed code, set up your studio, create an event.

Cognito's default email sender allows about 50 emails a day, which is fine for dev.
