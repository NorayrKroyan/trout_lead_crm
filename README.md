# TroutLead CRM

A local, browser-based lead discovery and quotation manager for frozen-trout export outreach.

## What it does

- Finds EU seafood / frozen-fish company websites through Brave Search API.
- Visits a small set of public company pages and extracts published business contact emails.
- Scores companies for trout, frozen fish, import, wholesale and distribution relevance.
- Deduplicates contacts in SQLite.
- Shows every lead, source page, score, approval and sent status in a web UI.
- Lets you approve/suppress leads, preview quotes and send through Gmail.
- Supports daily scheduled discovery and optional approved-lead sending.
- Records sends, failures and suppression actions in an activity log.
- Imports/exports CSV.

## Important operating policy

Use the application for legitimate B2B prospecting only. Do not use it to bypass logins, CAPTCHAs, paywalls, robots restrictions, or to harvest non-public personal data. Review applicable marketing/privacy rules for each destination country. Honor opt-outs immediately using the Suppress action.

Automatic sending is OFF by default. Leads must be approved before they are eligible for sending.

## Quick start — Windows

1. Install Python 3.11+ from python.org and tick **Add Python to PATH**.
2. Extract this project.
3. Double-click `run.bat`.
4. Wait for packages to install.
5. Open `http://127.0.0.1:5000` in your browser.

After the first install, `run.bat` starts the application again using the same local database.

## Quick start — macOS / Linux

```bash
cd troutlead_crm
./run.sh
```

Then open `http://127.0.0.1:5000`.

## Gmail configuration

The project uses Gmail SMTP over SSL:

- Server: `smtp.gmail.com`
- Port: `465`
- Security: SSL/TLS
- Username: your full Gmail address
- Password: a Google **App Password**, not the normal Gmail account password

### Create a Google App Password

1. Sign in to the Google Account that will send the offers.
2. Open **Google Account → Security**.
3. Enable **2-Step Verification** if it is not already enabled.
4. Open **App passwords**.
5. Create one named `TroutLead CRM`.
6. Copy the generated 16-character App Password.
7. In TroutLead CRM open **Settings → Gmail sending**.
8. Enter the Gmail address and paste the App Password.
9. Save.
10. Click **Test Gmail login**.

Do not enter the normal Gmail password into this project.

If App Passwords are unavailable on a managed Google Workspace account, your administrator may have disabled them. In that case, replace the SMTP authentication layer with Gmail API OAuth 2.0.

The app stores local secrets in `.env.local`. That file is excluded by `.gitignore` and should never be emailed, uploaded, or committed to a repository.

## Search configuration

The discovery feature intentionally does not scrape Google search result pages. It uses the Brave Search API to obtain public website URLs, then visits company websites.

1. Create a Brave Search API key.
2. Open **Settings → Search provider**.
3. Paste the API key and save it.
4. Go to **Find companies**, choose countries and click **Start discovery**.

The built-in queries contain English and several local-language phrases for fish importers, frozen-fish wholesalers and trout distributors.

## Recommended first-use workflow

1. Configure Gmail and test the connection.
2. Configure Brave Search API.
3. Edit **Quote template** and fill your product details in **Settings**.
4. Run discovery for 10–20 leads first.
5. Review each lead's source and fit score.
6. Click **Approve** only for relevant commercial prospects.
7. Preview one quote and send it to a test/business-safe address first.
8. When satisfied, send approved leads in small batches.
9. Mark every opt-out as **Suppress**.
10. Only after testing should you enable daily automation.

## Automation

Under **Settings → Daily automation** you can set:

- run time;
- daily discovery target;
- daily send cap;
- minimum lead score;
- delay range between emails;
- countries;
- automatic discovery;
- automatic sending.

The scheduler runs inside the Flask process. Therefore the application must be running at the scheduled time. For a production server, run the app as a service or move scheduled jobs to a system scheduler / worker.

## CSV import format

Accepted columns include:

```csv
company_name,country,website,domain,email,email_type,score,evidence,source_url
Example Seafood,Germany,https://example.de,example.de,purchasing@example.de,role,80,"trout, frozen fish, importer",https://example.de/contact
```

## Notes for production deployment

This project is intentionally local-only (`127.0.0.1`). If you deploy it to a server, add authentication, HTTPS, CSRF protection, encrypted secret storage, backups, health monitoring, a proper job queue and a production WSGI server.
