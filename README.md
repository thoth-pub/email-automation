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
    └── crossref-error-report.yml # Crossref-specific scheduler
```

### Core Components

- **Email Utilities** (`email_utils.py`): Reusable IMAP, SMTP, and CSV operations
- **Automation Orchestrator** (`email_automator.py`): Routes requests to specific automations
- **Crossref Processor** (`crossref_error_report.py`): Handles Crossref submission error emails
- **GitHub Actions**: Automated scheduling and execution in the cloud

## 🚀 Features

- **Modular Design**: Easy to add new automation types
- **UID-based IMAP**: Reliable email operations that avoid message ID conflicts
- **Secure SMTP**: STARTTLS encryption for email sending
- **GitHub Actions Integration**: Automated execution with secrets management
- **Comprehensive Logging**: Detailed execution tracking
- **Error Handling**: Robust error recovery and reporting

## 📧 Current Automations

### Crossref Error Reports
Processes Crossref submission error emails and generates monthly reports:
- Fetches error emails from designated IMAP folders
- Parses XML content to extract submission details
- Enriches data with Thoth API information (DOI, title)
- Generates CSV reports with comprehensive error details
- Emails reports to designated recipients
- Moves processed emails to checked folder

## 🛠️ Setup

### 1. Environment Configuration

Create a `config.env` file (for local development):
```env
# IMAP Configuration
IMAP_SERVER=your.imap.server.com
IMAP_USERNAME=your.email@domain.com
IMAP_PASSWORD=your_password

# SMTP Configuration  
THOTH_SMTP=smtp://username:password@smtp.server.com:587

# Recipients
CROSSREF_EMAIL=crossref@example.com
```

### 2. GitHub Secrets (for production)

Configure these secrets in your GitHub repository:
- `IMAP_SERVER`
- `IMAP_USERNAME` 
- `IMAP_PASSWORD`
- `THOTH_SMTP`
- `CROSSREF_EMAIL`

### 3. Dependencies

```bash
pip install -r requirements.txt
```

## 🔧 Usage

### Local Development

```bash
# Run Crossref automation
python email_automator.py --automation Crossref

# View available automations
python email_automator.py --help
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
