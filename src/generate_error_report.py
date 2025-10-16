#!/usr/bin/env python3
import imaplib
import email
import email.mime.text
import email.mime.multipart
import email.mime.base
import os
import sys
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
import requests
import pandas as pd
import logging
import smtplib
from email import encoders
from typing import List, Dict, Any, Optional


class Config:
    """Configuration management for the application"""

    def __init__(self):
        # Load environment variables from config.env
        load_dotenv(dotenv_path=os.path.join(
            os.path.dirname(__file__), '../config.env'))

        # IMAP settings
        self.imap_server = os.environ.get('IMAP_SERVER')
        self.imap_username = os.environ.get('IMAP_USERNAME')
        self.imap_password = os.environ.get('IMAP_PASSWORD')

        # SMTP settings
        self.smtp_url = os.environ.get('THOTH_SMTP')
        self.recipient_email = os.environ.get('CROSSREF_EMAIL')

        # Email folders
        self.source_folders = [
            'INBOX/Crossref_submissions/Error_reports/ISBN_already_assigned',
            'INBOX/Crossref_submissions/Error_reports/ISSN_already_assigned'
        ]
        self.checked_folder = 'INBOX/Crossref_submissions/Checked'

        # Validate required settings
        self._validate()

    def _validate(self):
        """Validate that required configuration is present"""
        if not all([self.imap_server, self.imap_username, self.imap_password]):
            raise ValueError(
                "Missing required IMAP configuration: "
                "IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD")


logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')


# General constants
DEFAULT_THOTH_API_URL = 'https://api.thoth.pub/graphql'
DEFAULT_SMTP_PORT = 587
EMAIL_SENDER = "Thoth Open Metadata <info@thoth.pub>"

# Crossref-specific constants
CROSSREF_CSV_FILENAME = 'crossref_error_report.csv'
CROSSREF_EMAIL_SUBJECT = "Crossref submission error reports from Thoth"
CROSSREF_EMAIL_BODY = "Crossref errors are contained as an attached CSV"


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
            try:
                self.mail.close()
                self.mail.logout()
                logging.info("IMAP connection closed successfully")
            except Exception as e:
                logging.warning(f"Error during IMAP disconnect: {e}")
            finally:
                self.mail = None

    def move_message(self, msg_uid: str, source_folder: str,
                     destination_folder: str) -> bool:
        """Move a message from source folder to destination folder using UID
        """
        try:
            # Select the source folder
            status, _ = self.mail.select(source_folder)
            if status != 'OK':
                logging.error(f"Failed to select source folder "
                              f"{source_folder}")
                return False

            # Copy message to destination folder using UID
            status, _ = self.mail.uid('copy', msg_uid, destination_folder)
            if status != 'OK':
                logging.error(f"Failed to copy message {msg_uid} to "
                              f"{destination_folder}")
                return False

            # Mark original message for deletion using UID
            status, _ = self.mail.uid('store', msg_uid, '+FLAGS', '\\Deleted')
            if status != 'OK':
                logging.error(f"Failed to mark message {msg_uid} for deletion")
                return False

            # Expunge to actually delete the message from source folder
            self.mail.expunge()

            logging.info(f"Moved message {msg_uid} from {source_folder} to "
                         f"{destination_folder}")
            return True

        except Exception as e:
            logging.error(f"Error moving message {msg_uid}: {e}")
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

        # Search for all messages using UID
        status, message_uids = self.mail.uid('search', None, 'ALL')
        if status != 'OK':
            logging.error(f"Failed to search messages in {folder}")
            return messages

        message_uid_list = message_uids[0].split()

        # Fetch each message using UID
        for msg_uid in message_uid_list:
            status, msg_data = self.mail.uid('fetch', msg_uid, '(RFC822)')
            if status == 'OK':
                email_body = msg_data[0][1]
                email_message = email.message_from_bytes(email_body)
                # Return tuple: (email_message, msg_uid, folder)
                messages.append((email_message, msg_uid.decode(), folder))

        return messages


class CrossrefParser:
    """Crossref-specific logic for parsing emailed error messages"""

    def __init__(self, thoth_api_url: str = DEFAULT_THOTH_API_URL):
        self.thoth_api_url = thoth_api_url

    def parse_message(self, email_message: email.message.EmailMessage
                      ) -> Optional[Dict[str, Any]]:
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
        msg_id = (diagnostic.attrib.get('msg_id')
                  if diagnostic is not None else None)
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

    def _fetch_thoth_data(self, thoth_work_id: str
                          ) -> tuple[Optional[str], Optional[str]]:
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

    def __init__(self, csv_path: str = CROSSREF_CSV_FILENAME):
        self.csv_path = csv_path

    def write_row(self, row_data: Dict[str, Any]):
        """Write a single row to CSV"""
        try:
            df = pd.read_csv(self.csv_path)
            df = pd.concat([df, pd.DataFrame([row_data])], ignore_index=True)
        except FileNotFoundError:
            df = pd.DataFrame([row_data])

        df.to_csv(self.csv_path, index=False)


class EmailSender:
    """Handles SMTP email operations"""

    def __init__(self, smtp_server: str, smtp_port: int, username: str,
                 password: str):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.username = username
        self.password = password

    def send_email_with_attachment(self, recipient: str, subject: str,
                                   body: str, sender: str,
                                   attachment_path: str) -> bool:
        """Send email with CSV attachment"""
        try:
            logging.info(f"Attempting to send email via {self.smtp_server}:"
                         f"{self.smtp_port}")

            # Create message
            msg = email.mime.multipart.MIMEMultipart()
            msg['From'] = sender
            msg['To'] = recipient
            msg['Subject'] = subject

            # Add body
            msg.attach(email.mime.text.MIMEText(body, 'plain'))

            # Add attachment
            if os.path.exists(attachment_path):
                with open(attachment_path, "rb") as attachment:
                    part = email.mime.base.MIMEBase('application',
                                                    'octet-stream')
                    part.set_payload(attachment.read())

                encoders.encode_base64(part)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename= '
                    f'{os.path.basename(attachment_path)}'
                )
                msg.attach(part)
            else:
                logging.warning(f"Attachment file not found: "
                                f"{attachment_path}")

            # Send email using SMTP with STARTTLS (port 587)
            logging.info(f"Sending email via {self.smtp_server}:587")
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()  # Upgrade to TLS
                server.login(self.username, self.password)
                server.send_message(msg)

            logging.info(f"Email sent successfully to {recipient}")
            return True

        except Exception as e:
            logging.error(f"Failed to send email: {e}")
            return False


def parse_smtp_url(smtp_url: str) -> tuple[str, int, str, str]:
    """Parse SMTP URL format: smtp://username:password@server:port"""
    from urllib.parse import urlparse

    parsed = urlparse(smtp_url)
    server = parsed.hostname
    port = parsed.port or DEFAULT_SMTP_PORT
    username = parsed.username
    password = parsed.password

    return server, port, username, password


class CrossrefEmailProcessor:
    """Main application class for processing Crossref error emails"""

    def __init__(self, config: Config):
        self.config = config
        self.email_fetcher = EmailFetcher(
            config.imap_server,
            config.imap_username,
            config.imap_password
        )
        self.parser = CrossrefParser()
        self.csv_writer = CSVWriter()

    def process_emails(self) -> bool:
        """Main processing workflow"""
        try:
            # Connect to email server
            if not self.email_fetcher.connect():
                return False

            # Fetch and process messages
            messages = self.email_fetcher.fetch_messages_from_folders(
                self.config.source_folders)
            logging.info(f"Total messages fetched: {len(messages)}")

            # Process each message
            for email_message, msg_uid, source_folder in messages:
                parsed_data = self.parser.parse_message(email_message)
                if parsed_data:
                    self.csv_writer.write_row(parsed_data)
                    # Move email to checked folder after successful processing
                    self.email_fetcher.move_message(
                        msg_uid, source_folder, self.config.checked_folder)
                else:
                    logging.warning(f"Failed to parse message {msg_uid}, "
                                    f"leaving in {source_folder}")
                logging.info("---")

            # Send email report if messages were processed
            if messages:
                self._send_report()
            else:
                logging.info("No messages processed, skipping email")

            return True

        except Exception as e:
            logging.error(f"Error during processing: {e}")
            return False
        finally:
            self.email_fetcher.disconnect()

    def _send_report(self):
        """Send email report with CSV attachment"""
        if not (self.config.smtp_url and self.config.recipient_email):
            logging.info("SMTP credentials not provided, skipping email")
            return

        try:
            server, port, smtp_user, smtp_pass = parse_smtp_url(
                self.config.smtp_url)
            email_sender = EmailSender(server, port, smtp_user, smtp_pass)

            success = email_sender.send_email_with_attachment(
                recipient=self.config.recipient_email,
                subject=CROSSREF_EMAIL_SUBJECT,
                body=CROSSREF_EMAIL_BODY,
                sender=EMAIL_SENDER,
                attachment_path=self.csv_writer.csv_path
            )

            if success:
                logging.info("Email report sent successfully")
            else:
                logging.warning("Failed to send email report")

        except Exception as e:
            logging.error(f"Error sending email: {e}")


def fetch_parse_crossref_emails():
    """Main function to fetch and parse Crossref error emails"""
    try:
        config = Config()
        processor = CrossrefEmailProcessor(config)
        return processor.process_emails()
    except ValueError as e:
        logging.error(f"Configuration error: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return False


if __name__ == "__main__":
    logging.info("Starting Crossref error email fetch and processing...")
    success = fetch_parse_crossref_emails()

    # Force flush all outputs
    sys.stdout.flush()
    sys.stderr.flush()

    if success:
        logging.info("Email fetch and processing completed successfully")
        logging.info("Script terminating with success code")
        sys.exit(0)
    else:
        logging.error("Email fetch and processing failed")
        logging.error("Script terminating with error code")
        sys.exit(1)
