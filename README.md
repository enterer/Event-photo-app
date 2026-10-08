# Event photo platform

Multi-event version of the wedding selfie photo app, for photographers and event businesses. See `docs/phase1-design.md` for the full design and build order.

**This repo never deploys to eu-west-1.** That region runs the wedding (`wedding-photos` stack, separate repo). The template refuses eu-west-1, and `scripts/deploy.sh` refuses the wedding region and stack name.

## Status: week 1

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

## Try it

1. Create a test user: Cognito console in us-east-1 → user pool `eventphotos-dev` → Create user.
2. Get a token and call the API (a sign-in page comes in week 3 with the dashboard):

```bash
aws cognito-idp admin-initiate-auth --region us-east-1 --user-pool-id <UserPoolId> \
  --client-id <UserPoolClientId> --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=<email>,PASSWORD=<password>
```

(Enable `ALLOW_ADMIN_USER_PASSWORD_AUTH` on the app client for this test only, or wait for the dashboard.)
