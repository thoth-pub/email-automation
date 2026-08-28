# Email Automation System

A modular system for automating email-based workflows, designed to handle various types of email processing tasks for Thoth.

## Overview

This system processes incoming emails, extracts relevant data, generates reports, and sends automated messages. Currently implements Crossref error report processing, with architecture designed for easy extension to other automation types.

## Architecture

```
email_automator.py          # Main orchestrator and CLI entry point
├── crossref_error_report.py # Crossref-specific automation logic
├── email_utils.py          # Reusable email utilities (IMAP, SMTP, CSV)
└── .github/workflows/      # GitHub Actions for automated execution
    ├── email_automate.yml  # Reusable workflow template
    └── crossref-error-report.yml # Crossref-specific workflow and scheduler
```

### Core Components

- **Email Utilities** (`email_utils.py`): Reusable IMAP, SMTP, and CSV operations
- **Automation Orchestrator** (`email_automator.py`): Routes requests to specific automations
- **Crossref Processor** (`crossref_error_report.py`): Handles Crossref submission error emails
- **GitHub Actions**: (`.github/workflows`): Automated scheduling and execution

## Current Automations

### Crossref Error Reports
Processes Crossref submission error emails and generates monthly reports:
- Fetches error emails from designated IMAP folders (Gmail labels, see
  [Mailbox configuration](#mailbox-configuration))
- Parses XML content to extract submission details
- Enriches data with Thoth API information (DOI, title, subtitle)
- Generates CSV reports with comprehensive error details
- Emails reports to Crossref
- Moves processed emails to the Checked label

## 🛠️ Setup

### 1. Environment Configuration

Create a `config.env` file (for local development):
```env
# IMAP Configuration (inbound mail: Google Workspace)
IMAP_SERVER=imap.gmail.com
IMAP_USERNAME=your.email@domain.com
IMAP_PASSWORD=your_app_password

# SMTP Configuration (outbound mail: unrelated to IMAP, see below)
THOTH_SMTP=smtp://username:password@smtp.server.com:587

# Recipient (for Crossref workflow)
CROSSREF_EMAIL=crossref@example.com
```

`config.env` is git-ignored. Never commit real usernames, passwords or app
passwords to this repository.

### 2. GitHub Secrets (for production)

Configure these secrets in the repository:
- `IMAP_SERVER` — `imap.gmail.com`
- `IMAP_USERNAME` — the Google Workspace mailbox that receives Crossref mail
- `IMAP_PASSWORD` — a Google Workspace **app password** for that mailbox
  (a normal account password will not work over IMAP)
- `THOTH_SMTP`
- `CROSSREF_EMAIL`

IMAP must be enabled on the Google Workspace mailbox, and the account needs
2-Step Verification in order for an app password to be issued.

#### Inbound (IMAP) and outbound (SMTP) are independent

`IMAP_SERVER` / `IMAP_USERNAME` / `IMAP_PASSWORD` control **only** where
error reports are read from. Outbound mail is sent entirely separately via
`THOTH_SMTP`, which is unaffected by the mailbox provider and is not part of
the Google Workspace migration. Changing the IMAP settings does not require
any change to `THOTH_SMTP`.

### 3. Mailbox configuration

Inbound mail is hosted on Google Workspace (previously Fastmail). Gmail
exposes labels to IMAP as folders, with `/` separating levels of the
hierarchy. The canonical labels used by the Crossref automation are:

| Purpose | Label |
| --- | --- |
| Source: ISBN errors | `Crossref_submissions/Error_reports/ISBN_already_assigned` |
| Source: ISSN errors | `Crossref_submissions/Error_reports/ISSN_already_assigned` |
| Destination: processed | `Crossref_submissions/Checked` |

The two source labels are applied by Gmail filters as Crossref messages
arrive. `Crossref_submissions/Checked` has no incoming filter, so the
automation creates it on demand if it is missing; it can equally be created
by hand in Gmail beforehand.

Before processing anything, each run checks that the source labels exist and
that the Checked label exists or can be created. A missing source label
fails the run with an explicit message rather than silently reporting no
errors.

> **Legacy labels.** The Fastmail migration imported the old folder
> hierarchy into Gmail as literal labels prefixed with `INBOX/`, for example
> `INBOX/Crossref_submissions/Error_reports/ISBN_already_assigned`. These
> hold historical mail only. They are deliberately **not** used by the
> automation, so that a backlog of migrated messages is never reprocessed.

#### Moving processed messages

Where the server advertises `MOVE` (Gmail does), processed messages are
relocated with `UID MOVE`. Under Gmail this removes the source label and
adds `Crossref_submissions/Checked`; the underlying message is untouched and
remains in All Mail. No `\Deleted` flag is ever set, so Gmail's "when a
message is expunged from the last visible IMAP folder" setting — which can
archive, bin or permanently delete — is never triggered.

Servers without `MOVE` fall back to `COPY`, then `\Deleted`, then expunge.
The copy is always confirmed before the source message is touched, so a
failure leaves the message in its source label to be retried on the next
run.

### 4. Dependencies

```bash
pip install -r requirements.txt
```

## Usage

### Local Development

```bash
# Run Crossref automation
python email_automator.py --automation Crossref
```

### Tests

The test suite uses only the standard library and never contacts a real
mail server, so it needs no credentials and no network access:

```bash
python -m unittest discover -v
```

### GitHub Actions

The system runs automatically via GitHub Actions:

1. **Manual Triggers**: Use "Run workflow" button on GitHub Actions page (scheduled execution available)
2. **Configurable**: Easy to adjust schedules or add new automations
3. **Artifact Upload**: Generated reports are automatically uploaded and retained

## 📁 Project Structure

```
email-automation/
├── README.md                          # This file
├── requirements.txt                   # Python dependencies
├── config.env.template               # Configuration template
├── email_automator.py                # Main CLI orchestrator
├── crossref_error_report.py          # Crossref automation logic
├── email_utils.py                    # Reusable email utilities
├── test_crossref_error_report.py     # Tests for the Crossref automation
├── test_email_utils.py               # Tests for the IMAP/SMTP utilities
├── .github/workflows/
│   ├── email_automate.yml            # Reusable workflow template
│   └── crossref-error-report.yml     # Crossref scheduler
```

## Adding New Automations

The system is designed for easy extension:

### 1. Create Your Automation Module

```python
# my_automation.py
class MyAutomationProcessor:
    @classmethod
    def run(cls):
        """Main entry point for your automation"""
        # Your automation logic here
        pass
```

### 2. Register in Orchestrator

```python
# email_automator.py
from my_automation import MyAutomationProcessor

AUTOMATORS = {
    "Crossref": CrossrefEmailProcessor,
    "MyAutomation": MyAutomationProcessor,  # Add your automation
}
```

### 3. Create GitHub Actions Workflow

```yaml
# .github/workflows/my-automation.yml
name: my-automation
on:
  schedule:
    - cron: '0 12 * * *'  # Daily at noon
jobs:
  my-automation:
    uses: ./.github/workflows/email_automate.yml
    with:
      automation: 'MyAutomation'
      artifact_path: '*.xlsx'  # Customize output artifacts
    secrets: inherit
```

## 🔍 Monitoring & Debugging

### Logs
- All operations are logged with INFO level
- GitHub Actions logs available in the Actions tab
- Local development logs appear in console

### Error Handling
- Configuration validation on startup
- Graceful handling of email connection issues
- Detailed error messages for troubleshooting

### Artifacts
- GitHub Actions automatically uploads output files
- Reports are retained for 60 days
- Manual download available from Actions page
