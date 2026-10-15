# Phase 1 design: multi-event platform

Goal: a photographer signs up, pays, creates an event in minutes, and gets a guest QR code, an upload link and a review queue. Everything else (event owner portal, lead capture, landing pages) waits until after launch.

Wedding: November 19 and 21. Freeze for the wedding system: November 5. Platform beta: from the week after the wedding. Paid launch: about 4 to 8 weeks after.

## Separation from the wedding

| | Wedding | Platform |
|---|---|---|
| Repo | `wedding-photos-app` | `Event-photo-app` |
| Region | eu-west-1 | us-east-1 (dev), production account later |
| Stack | `wedding-photos` | `eventphotos-dev` |
| Deploy | `sam deploy` | `scripts/deploy.sh` (refuses eu-west-1 and the wedding stack) |

Guest photos for Nigerian events can later be stored in eu-west-1 in the production account; dev stays in us-east-1.

## Core idea: tenants and events

- **Organisation** = a photographer or event business (the paying customer). Has members, a plan, branding.
- **Event** = one wedding, party or conference, owned by an organisation. Has its own guest code, upload key, admin settings, sorting rules, couple/host reference faces, and a deletion date.
- **Guest session** = one selfie match, scoped to one event.

Every piece of data carries `orgId` and `eventId`, and every API call checks that the caller belongs to that org. One event can never see another's photos or faces.

## Data layout

**DynamoDB, single table `Platform`** (pk / sk):

| Item | pk | sk | Notable fields |
|---|---|---|---|
| Organisation | `ORG#<orgId>` | `META` | name, plan, stripeCustomerId, branding (logo key, colours, booking URL), photoQuotaUsed |
| Member | `ORG#<orgId>` | `USER#<userId>` | role (owner, staff) |
| User lookup | `USER#<userId>` | `ORG#<orgId>` | for "which orgs am I in" |
| Event | `ORG#<orgId>` | `EVENT#<eventId>` | name, dates, slug, guestCodeHash, uploadKeyHash, status, deleteAfter, sortingRules, publicConfidence |
| Event lookup | `SLUG#<slug>` | `EVENT` | orgId, eventId (guest page finds the event from its URL) |
| Photo | `EVENT#<eventId>` | `PHOTO#<photoId>` | visibility, source, reason, confidence, uploader, faceIds |
| Guest session | `SESSION#<token>` | `META` | eventId, privateIds, expiresAt (TTL) |

Codes and keys are stored hashed, never in plain text (unlike the wedding version's stack parameters).

**S3, one bucket, prefixed per event:**
`events/<eventId>/{incoming,public,private}/...`, `events/<eventId>/config/...`, `orgs/<orgId>/branding/...`.
Lifecycle rule plus a nightly job delete everything for events past `deleteAfter`.

**Rekognition, two collections per event:** `ev-<eventId>` (guest faces in private photos) and `ev-<eventId>-host` (couple or host reference faces). Deleted with the event. This keeps face data isolated and makes deletion a single call.

## Accounts and billing

- **Sign-in:** Amazon Cognito user pool (email + password, optional Google). The dashboard sends the Cognito token; API Gateway's JWT authorizer verifies it.
- **Billing:** Stripe Checkout and Customer Portal. A Stripe webhook updates the org's `plan`. Plan limits (events per month, photos per event, AI sorting on or off) are enforced when creating events and issuing upload slots.

## API

Guest and photographer-upload routes keep the wedding version's shape, with the event in the path:

| Route | Who | Purpose |
|---|---|---|
| `GET /e/{slug}` | Guest | Event name, branding, "Photos by" credit |
| `POST /e/{slug}/match` | Guest | Selfie → session + gallery |
| `GET /e/{slug}/gallery` | Guest | Returning guest |
| `POST /e/{slug}/upload-urls` | Photographer (upload key) | Pre-signed uploads |
| `GET/POST /orgs/...` | Signed-in member | Org, members, branding |
| `GET/POST /orgs/{org}/events` | Signed-in member | List and create events |
| `GET/POST /orgs/{org}/events/{event}/...` | Signed-in member | Photos, visibility, re-sort, rules, host faces, QR code |
| `POST /billing/webhook` | Stripe | Plan changes |

## Photographer dashboard (screens)

1. **Sign up / sign in** → choose plan (Stripe Checkout).
2. **Events list:** upcoming and past events, photo counts, days until deletion.
3. **Create event:** name, dates, guest page slug. Generates guest code, upload link and QR code.
4. **Event page tabs:** Share (QR code, guest link, upload link), Review (the wedding review page), Rules, Host faces, Settings (retention, delete event now).
5. **Branding:** logo, colours, booking link, shown on every guest page.

## What carries over from the wedding code

| Wedding piece | Platform change |
|---|---|
| Selfie match, gallery | Event from URL; collection per event |
| Upload page | Same page, event slug + upload key in the link |
| Processor (resize, AI sort, host check) | Reads event from S3 key; rules and confidence from the event item |
| Review page | Becomes the dashboard's Review tab; Cognito instead of admin code |
| Re-sort with AI | Unchanged apart from event scoping |

## Build order (weeks are from today, October 8)

| Week | Work |
|---|---|
| 1 (Oct 8) | Template: table, bucket, Cognito, API skeleton; org and event creation |
| 2 (Oct 15) | Port guest flow and processor with event scoping; per-event collections |
| 3 (Oct 15) | Done: event tabs (share and QR, photos review, rules, hosts, stats) and studio branding |
| 4 (Oct 22) | Stripe checkout with per-event and monthly pricing; plan limits; WhatsApp share button |
| Nov 5 to 21 | Light work only: bug fixes, docs, deletion job; wedding takes priority |
| After Nov 21 | Beta with the wedding photographer; production account; security review |

## Open questions

- Production data region for Nigerian events (eu-west-1 is closest with Rekognition and Bedrock).
- Liveness check: platform-wide from launch, or a Pro feature?
- Retention default per plan (60 days Starter, 6 months Pro).
