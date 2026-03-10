# Google Drive Setup

PageForge does **not** include any Google credentials. To use Drive sync, you create your own Google Cloud project, generate a service account key, and give it access to whichever Drive folder you choose. Your credentials never leave your machine.

This is entirely optional — PageForge works without Drive. See the [main README](../README.md) for the inbox folder and upload alternatives.

---

## Overview

Google Drive sync works through a **service account** — a non-human Google identity your project controls. You share one of your Drive folders with that account (like sharing with a colleague), and PageForge uses its JSON key to authenticate and poll the folder.

---

## Step 1 — Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com/).
2. Click the project selector at the top → **New Project**.
3. Give it any name (e.g. `pageforge`) and click **Create**.
4. Make sure the new project is selected in the top bar before continuing.

---

## Step 2 — Enable the Google Drive API

1. In the left sidebar go to **APIs & Services > Library**.
2. Search for **Google Drive API**.
3. Click it and press **Enable**.

---

## Step 3 — Create a Service Account

1. Go to **APIs & Services > Credentials**.
2. Click **+ Create Credentials > Service account**.
3. Fill in a name (e.g. `pageforge-sync`) — the email address is auto-generated and shown below the name field. Copy it; you'll need it in Step 5.
4. Click **Create and Continue**. Skip the optional role and user steps — click **Done**.

---

## Step 4 — Download the JSON Key

1. On the **Credentials** page, find your new service account and click its name.
2. Go to the **Keys** tab.
3. Click **Add Key > Create new key**.
4. Choose **JSON** and click **Create**. The file downloads automatically.

The downloaded file will look like [`data/credentials.example.json`](../data/credentials.example.json) — same structure, real values.

> **Keep this file private.** It grants full access to anything the service account has been shared on. Do not commit it to source control — `data/credentials.json` is already excluded by `.gitignore`. Only the example file (with no real keys) is tracked.

---

## Step 5 — Share Your Drive Folder

1. In Google Drive, navigate to the folder you want PageForge to watch.
2. Right-click → **Share**.
3. Paste the service account email (from Step 3) into the people field.
4. Set the permission to **Editor** (PageForge needs Editor to delete files after processing).
5. Uncheck "Notify people" and click **Share**.

PageForge will only see files in this specific folder — nothing else in your Drive.

---

## Step 6 — Get the Folder ID

Open the folder in Drive. The URL looks like:

```
https://drive.google.com/drive/folders/1A2B3C4D5E6F7G8H9I0J
                                       ^^^^^^^^^^^^^^^^^^^^
                                       this is your folder ID
```

Copy the alphanumeric string after `/folders/`.

---

## Step 7 — Configure PageForge

**Place the key file:**

```bash
# If using Docker with the default volume mount:
cp ~/Downloads/your-key-file.json ./data/credentials.json
```

The file must be at `/data/credentials.json` inside the container. If you changed the data volume path, adjust accordingly.

**Set the folder ID** — pick one:

- **Environment variable** (recommended for Docker):
  ```yaml
  # docker-compose.yml
  environment:
    DRIVE_FOLDER_ID: 1A2B3C4D5E6F7G8H9I0J
  ```

- **Web UI:** open the Settings tab → Google Drive section → paste the folder ID → Save.

- **.env file:**
  ```bash
  DRIVE_FOLDER_ID=1A2B3C4D5E6F7G8H9I0J
  ```

Once set, PageForge will poll the folder on every sync cycle, download any new PDFs, convert them, and delete the originals from Drive.

---

## Troubleshooting

**"credentials.json not found"**
The key file is missing from `/data/credentials.json`. Double-check the volume mount and file path.

**"The caller does not have permission"**
The service account email was not added as an Editor on the folder. Repeat Step 5.

**"Drive folder ID is set but..."**
The `DRIVE_FOLDER_ID` env var or config value is set, but `credentials.json` is absent. Both are required together.

**Files not being deleted from Drive**
Deletion requires Editor access. If the service account was added as Viewer, re-share with Editor permission.

**API not enabled**
If you see a 403 with `accessNotConfigured`, return to Step 2 and enable the Drive API for your project.

---

## Disabling Drive Sync

To stop Drive sync without removing the credentials file, clear the folder ID:

- **Web UI:** Settings tab → Drive Folder ID → clear the field → Save.
- **Environment:** remove or unset `DRIVE_FOLDER_ID`.

PageForge will continue processing the local inbox and uploads as normal.
