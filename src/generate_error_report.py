#!/usr/bin/env python3
import imaplib
import email
import os
import sys
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
import requests
import pandas as pd
import logging
from typing import List, Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Load environment variables from config.env
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../config.env'))


class EmailFetcher:
    """Fetches emails from specified folders using IMAP"""

    def __init__(self, server: str, username: str, password: str):
        self.server = server
        self.username = username
        self.password = password
        self.mail = None

    def connect(self) -> bool:
        """Connect to IMAP server"""
        try:
            logging.info("Connecting to email server...")
            self.mail = imaplib.IMAP4_SSL(self.server)
            self.mail.login(self.username, self.password)
            return True
        except Exception as e:
            logging.error(f"Failed to connect to email server: {e}")
            return False

    def disconnect(self):
        """Close IMAP connection"""
        if self.mail:
            self.mail.close()
            self.mail.logout()
            logging.info("IMAP connection closed successfully")

    def move_message(self, msg_id: str, source_folder: str,
                     destination_folder: str) -> bool:
        """Move a message from source folder to destination folder"""
        try:
            # Select the source folder
            status, _ = self.mail.select(source_folder)
            if status != 'OK':
                logging.error(f"Failed to select source folder "
                              f"{source_folder}")
                return False

            # Copy message to destination folder
            status, _ = self.mail.copy(msg_id, destination_folder)
            if status != 'OK':
                logging.error(f"Failed to copy message {msg_id} to "
                              f"{destination_folder}")
                return False

            # Mark original message for deletion
            status, _ = self.mail.store(msg_id, '+FLAGS', '\\Deleted')
            if status != 'OK':
                logging.error(f"Failed to mark message {msg_id} for deletion")
                return False

            # Expunge to actually delete the message from source folder
            self.mail.expunge()
            
            logging.info(f"Moved message {msg_id} from {source_folder} to "
                         f"{destination_folder}")
            return True

        except Exception as e:
            logging.error(f"Error moving message {msg_id}: {e}")
            return False

    def fetch_messages_from_folders(self, folders: List[str]) -> List[tuple]:
        """Fetch all messages from specified folders"""
        all_messages = []

        for folder in folders:
            messages = self.fetch_messages_from_folder(folder)
            all_messages.extend(messages)

        return all_messages

    def fetch_messages_from_folder(self, folder: str) -> List[tuple]:
        """Fetch all messages from a single folder"""
        messages = []

        logging.info(f"Reading emails in {folder}")

        # Select folder
        status, folder_messages = self.mail.select(folder)
        if status != 'OK':
            logging.error(f"Failed to select {folder}: {status}")
            return messages

        logging.info(f"{folder} contains {int(folder_messages[0])} messages")

        # Search for all messages
        status, message_ids = self.mail.search(None, 'ALL')
        if status != 'OK':
            logging.error(f"Failed to search messages in {folder}")
            return messages

        message_id_list = message_ids[0].split()

        # Fetch each message
        for msg_id in message_id_list:
            status, msg_data = self.mail.fetch(msg_id, '(RFC822)')
            if status == 'OK':
                email_body = msg_data[0][1]
                email_message = email.message_from_bytes(email_body)
                # Return tuple: (email_message, msg_id, folder)
                messages.append((email_message, msg_id.decode(), folder))

        return messages


class CrossrefParser:
    """Crossref-specific logic for parsing emailed error messages"""

    def __init__(self, thoth_api_url: str = 'https://api.thoth.pub/graphql'):
        self.thoth_api_url = thoth_api_url

    def parse_message(self, email_message: email.message.EmailMessage) -> Optional[Dict[str, Any]]:
        """Parse a single Crossref email message and return extracted data"""

        # Extract message body
        body = email_message.get_payload(decode=True).decode()

        # Parse XML from body
        try:
            bodyxml = ET.fromstring(body)
        except Exception as e:
            logging.error(f"Failed to parse XML: {e}")
            return None

        # Extract relevant submission metadata from email body
        submission_id = bodyxml.findtext('.//submission_id')
        batch_id = bodyxml.findtext('.//batch_id')

        logging.info(f"Processing Crossref Submission ID: {submission_id}")

        # Extract Thoth Work ID from batch_id
        thoth_work_id = None
        thoth_work_id_url = None
        if batch_id and '_' in batch_id:
            thoth_work_id = batch_id.split('_')[0]
            thoth_work_id_url = f"https://thoth.pub/books/{thoth_work_id}"

        # Extract diagnostic error information
        diagnostic = bodyxml.find('.//record_diagnostic')
        msg_id = diagnostic.attrib.get('msg_id') if diagnostic is not None else None
        msg = diagnostic.find('msg') if diagnostic is not None else None

        # Enrich with Thoth API data
        doi, title = self._fetch_thoth_data(thoth_work_id)

        return {
            'submission_id': submission_id,
            'batch_id': batch_id,
            'thoth_record_url': thoth_work_id_url,
            'work_title': title,
            'doi': doi,
            'crossref_error_msg_id': msg_id,
            'crossref_error_message': msg.text if msg is not None else None
        }

    def _fetch_thoth_data(self, thoth_work_id: str) -> tuple[Optional[str], Optional[str]]:
        """Fetch DOI and title from Thoth API"""
        if not thoth_work_id:
            return None, None

        query = '{ work(workId: "%s") { doi fullTitle } }' % thoth_work_id

        try:
            response = requests.post(self.thoth_api_url, json={'query': query})
            if response.status_code == 200:
                data = response.json()
                work = data.get('data', {}).get('work', {})
                doi = work.get('doi')
                title = work.get('fullTitle')
                logging.info(f"Retrieved Thoth data - DOI: {doi}")
                return doi, title
            else:
                logging.error(f"Thoth API error: {response.status_code}")
                return None, None
        except Exception as e:
            logging.error(f"Error fetching Thoth data: {e}")
            return None, None


class CSVWriter:
    """Handles CSV file operations"""

    def __init__(self, csv_path: str = 'crossref_error_report.csv'):
        self.csv_path = csv_path

    def write_row(self, row_data: Dict[str, Any]):
        """Write a single row to CSV"""
        try:
            df = pd.read_csv(self.csv_path)
            df = pd.concat([df, pd.DataFrame([row_data])], ignore_index=True)
        except FileNotFoundError:
            df = pd.DataFrame([row_data])

        df.to_csv(self.csv_path, index=False)


def fetch_parse_crossref_emails():
    """Main function to fetch and parse Crossref error emails"""

    # Get environment variables
    imap_server = os.environ.get('IMAP_SERVER')
    username = os.environ.get('IMAP_USERNAME')
    password = os.environ.get('IMAP_PASSWORD')

    if not all([imap_server, username, password]):
        logging.error("Missing one or more required repository secrets: "
                      "IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD")
        sys.exit(1)

    # Initialize components
    email_fetcher = EmailFetcher(imap_server, username, password)
    parser = CrossrefParser()
    csv_writer = CSVWriter()

    try:
        # Connect to email server
        if not email_fetcher.connect():
            return False

        # Folders in inbox to check
        folders = [
            'INBOX/Crossref_submissions/Error_reports/ISBN_already_assigned',
            'INBOX/Crossref_submissions/Error_reports/ISSN_already_assigned'
        ]

        # Fetch all messages
        messages = email_fetcher.fetch_messages_from_folders(folders)
        logging.info(f"Total messages fetched: {len(messages)}")

        # Destination folder for processed emails
        checked_folder = 'INBOX/Crossref_submissions/Checked'

        # Process each message
        for email_message, msg_id, source_folder in messages:
            parsed_data = parser.parse_message(email_message)
            if parsed_data:
                csv_writer.write_row(parsed_data)
                # Move email to checked folder after successful processing
                email_fetcher.move_message(msg_id, source_folder,
                                           checked_folder)
            else:
                logging.warning(f"Failed to parse message {msg_id}, "
                                f"leaving in {source_folder}")
            logging.info("---")

        return True

    except Exception as e:
        logging.error(f"Error: {e}")
        return False
    finally:
        email_fetcher.disconnect()


if __name__ == "__main__":
    logging.info("Starting Crossref error email fetch and processing...")
    success = fetch_parse_crossref_emails()
    if success:
        logging.info("Email fetch and processing completed successfully")
        sys.exit(0)
    else:
        logging.error("Email fetch and processing failed")
        sys.exit(1)
