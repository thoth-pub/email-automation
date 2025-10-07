#!/usr/bin/env python3
import imaplib
import email
import os
import sys
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
import requests
import pandas as pd

# Load environment variables from config.env
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../config.env'))

# TODO:
# Separate the logic for getting the messages vs parsing the messages, for MUSE stuff in the future
# Read from email, send from email separate classes for these parts of the logic
# do the send email logic in Python, rather than in the yml


def fetch_crossref_emails():
    """Fetch messages from Inbox.Crossref_submissions via IMAP"""

    # Get environment variables
    imap_server = os.environ.get('IMAP_SERVER')
    username = os.environ.get('IMAP_USERNAME')
    password = os.environ.get('IMAP_PASSWORD')

    if not all([imap_server, username, password]):
        print("Error: Missing required environment variables")
        print("Required: IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD")
        sys.exit(1)

    try:
        print("Connecting to Thoth email server...")
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(username, password)

        # Select the Crossref_submissions folder
        status, messages = mail.select('INBOX/Crossref_submissions/Error_reports')
        if status != 'OK':
            print(f"Failed to select folder: {status}")
            return False

        print(f"Crossref Error Reports folder contains {int(messages[0])} messages")

        # Search for all messages
        status, message_ids = mail.search(None, 'ALL')
        if status != 'OK':
            print("Failed to search messages")
            return False

        message_id_list = message_ids[0].split()

        # Process each message
        for msg_id in message_id_list:
            status, msg_data = mail.fetch(msg_id, '(RFC822)')
            if status == 'OK':
                email_body = msg_data[0][1]
                email_message = email.message_from_bytes(email_body)

                # Process email content
                process_crossref_email(email_message)
                print("---")

                # TODO: Find out if Hannah wants messages moved to another folder
                # result = mail.copy(msg_id, 'INBOX/Crossref_submissions/Checked')
                # if result[0] == 'OK':
                #     mail.store(msg_id, '+FLAGS', '\\Deleted')
                #     mail.expunge()
                #     print(f"Message {msg_id.decode()} moved to Checked folder.")
                # else:
                #     print(f"Failed to move message {msg_id.decode()} to Checked folder.")

        # Close connection
        mail.close()
        mail.logout()
        print("IMAP connection closed successfully")
        return True

    except Exception as e:
        print(f"Error: {e}")
        return False


def process_crossref_email(email_message):
    """Process individual Crossref email for error reporting"""

    # Extract message body
    body = email_message.get_payload(decode=True).decode()

    # parse the email message body as XML
    try:
        bodyxml = ET.fromstring(body)
    except Exception as e:
        print(f"  -> Failed to parse XML: {e}")
        return

    submission_id = bodyxml.findtext('.//submission_id')
    print(f"  -> Crossref Submission ID: {submission_id}")

    batch_id = bodyxml.findtext('.//batch_id')

    # Extract Thoth Work ID from batch_id
    thoth_work_id = None
    if batch_id and '_' in batch_id:
        thoth_work_id = batch_id.split('_')[0]
        thoth_work_id_url = f"https://thoth.pub/books/{thoth_work_id}"

    diagnostic = bodyxml.find('.//record_diagnostic')
    
    msg_id = diagnostic.attrib.get('msg_id')
    msg = diagnostic.find('msg')
    # GraphQL query for Thoth API
    query = '{ work(workId: "%s") { doi fullTitle } }' % thoth_work_id
    response = requests.post(
        'https://api.thoth.pub/graphql',
        json={'query': query}
    )
    if response.status_code == 200:
        data = response.json()
        work = data.get('data', {}).get('work', {})
        doi = work.get('doi')
        title = work.get('fullTitle')
        print(f"  -> Thoth DOI retrieved from API: {doi}")
    else:
        print(f"  -> Thoth API error: {response.status_code}")

    # Write data to a CSV
    csv_path = 'crossref_error_report.csv'
    row = {
        'submission_id': submission_id,
        'batch_id': batch_id,
        'thoth_record_url': thoth_work_id_url if thoth_work_id else None,
        'work_title': title if 'title' in locals() else None,
        'doi': doi if 'doi' in locals() else None,
        'crossref_error_msg_id': msg_id,
        'crossref_error_message': msg.text if msg is not None else None
    }
    try:
        df = pd.read_csv(csv_path)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    except FileNotFoundError:
        df = pd.DataFrame([row])
    df.to_csv(csv_path, index=False)


if __name__ == "__main__":
    print("Starting Crossref error email fetch...")
    success = fetch_crossref_emails()
    if success:
        print("Email fetch completed successfully")
        sys.exit(0)
    else:
        print("Email fetch failed")
        sys.exit(1)
