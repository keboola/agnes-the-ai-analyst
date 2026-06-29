# Google Workspace connector — GCP setup reference

This page covers every step needed to create an OAuth Desktop app in GCP and configure Agnes to use it. The admin UI at `/admin/datasource-credentials` walks through the same steps inline — this doc is for reference and scripted setups.

## Why you need this

Agnes's Google Workspace connector asks analysts to authenticate with their Google account so it can read Drive, Gmail, Calendar, and Chat on their behalf. The connector needs an OAuth 2.0 **Desktop app** client registered in a GCP project. When the admin configures `AGNES_GWS_CLIENT_ID` + `AGNES_GWS_CLIENT_SECRET` once, every analyst on the instance gets credentials pre-configured and skips this setup entirely.

## Required GCP role

The account performing the setup needs at minimum:

- `roles/serviceusage.serviceUsageAdmin` — to enable APIs
- `roles/oauthconfig.editor` — to configure the consent screen and create OAuth clients

`roles/editor` covers both.

## Step 1 — Create or pick a GCP project

```bash
gcloud projects create <your-project-id> --set-as-default
# or reuse an existing project:
gcloud config set project <existing-project-id>
```

## Step 2 — Enable the required APIs

```bash
gcloud services enable \
  drive.googleapis.com \
  gmail.googleapis.com \
  calendar-json.googleapis.com \
  chat.googleapis.com \
  admin.googleapis.com \
  people.googleapis.com
```

## Step 3 — Configure the OAuth consent screen

Open [APIs & Services → OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent).

- **User type:** choose **Internal** if all analysts are in the same Google Workspace org. This requires no app verification. Choose **External** only if analysts are outside your org (requires verification for production scopes).
- Fill in *App name*, *User support email*, and *Developer contact information*.
- Leave scopes at their defaults — the connector requests scopes at runtime.

## Step 4 — Create a Desktop app OAuth client

Open [APIs & Services → Credentials → Create Credentials → OAuth client ID](https://console.cloud.google.com/apis/credentials/oauthclient).

- **Application type:** Desktop app
- **Name:** anything descriptive, e.g. `Agnes GWS connector`
- Click **Create** → download the JSON file (`client_secret_*.json`).

## Step 5 — Configure Agnes

Open the JSON file:

```json
{
  "installed": {
    "client_id": "123456789-xxxxxxxxxxxx.apps.googleusercontent.com",
    "client_secret": "GOCSPX-xxxxxxxxxxxxxx",
    ...
  }
}
```

**Via admin UI (recommended):** go to `/admin/datasource-credentials`, enter `client_id` and `client_secret`, click **Save**, then **Test**.

**Via environment variables:**

```bash
AGNES_GWS_CLIENT_ID=123456789-xxxxxxxxxxxx.apps.googleusercontent.com
AGNES_GWS_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxx
```

**Via `instance.yaml`** (least preferred — avoid committing secrets):

```yaml
instance:
  gws:
    client_id: "123456789-xxxxxxxxxxxx.apps.googleusercontent.com"
    client_secret: "GOCSPX-xxxxxxxxxxxxxx"
```

Resolution order: environment variable > vault (admin UI) > `instance.yaml`.

## Common failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `redirect_uri_mismatch` | OAuth client type is Web, not Desktop | Re-create client as **Desktop app** |
| `access_blocked: This app is blocked` | Consent screen is External and unverified | Switch to Internal, or complete the Google verification process |
| `Error 400: admin_policy_enforced` | Org policy restricts OAuth | Ask a Workspace admin to allow the app in the Admin Console under *Security → API controls* |
| `AGNES_GWS_CLIENT_ID` format invalid | Incorrect value pasted | Must match `<numeric-project>-<random>.apps.googleusercontent.com` |
| APIs not enabled | Connector returns permission errors | Re-run the `gcloud services enable` command from Step 2 |

## Security notes

- `client_id` is public — it appears in analyst OAuth flows and is safe to share.
- `client_secret` should be kept in the vault (or env var). Do not commit it to `instance.yaml` in a public repo.
- The OAuth consent screen set to **Internal** never requires Google verification and restricts sign-in to your Workspace domain.
