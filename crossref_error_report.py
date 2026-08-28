#!/usr/bin/env python3
import email
import os
import sys
import xml.etree.ElementTree as ET
from datetime import date
from dotenv import load_dotenv
import requests
import logging
from typing import Dict, Any, Optional
from email_utils import EmailFetcher, EmailSender, CSVWriter, parse_smtp_url

# General constants
DEFAULT_THOTH_API_URL = 'https://api.thoth.pub/graphql'
DEFAULT_SMTP_PORT = 587
EMAIL_SENDER = "Thoth Open Metadata <info@thoth.pub>"
EMAIL_CC = "distribution@thoth.pub"

# Crossref-specific constants
# TODO: replace with specific attachment filename, email subject and body based on Crossref feedback
CROSSREF_CSV_FILENAME = 'crossref_error_report'
CROSSREF_EMAIL_SUBJECT = "Crossref submission error reports from Thoth"
CROSSREF_EMAIL_BODY = "Crossref errors are contained as an attached CSV"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class Config:
    """Configuration management for the application"""

    def __init__(self):
        # Load environment variables from config.env
        load_dotenv(dotenv_path='./config.env')

        # Get IMAP settings from env
        self.imap_server = os.environ.get('IMAP_SERVER')
        self.imap_username = os.environ.get('IMAP_USERNAME')
        self.imap_password = os.environ.get('IMAP_PASSWORD')

        # Get SMTP settings from env
        self.smtp_url = os.environ.get('THOTH_SMTP')
        self.recipient_email = os.environ.get('CROSSREF_EMAIL')

        # Specify email folders for reading error messages.
        # These are Gmail's native label names, as created by the Gmail
        # filters that classify incoming Crossref messages. The Fastmail
        # hierarchy migrated into Gmail still exists under INBOX/..., but it
        # holds historical mail only and must not be processed here.
        self.source_folders = [
            'Crossref_submissions/Error_reports/ISBN_already_assigned',
            'Crossref_submissions/Error_reports/ISSN_already_assigned'
        ]
        # Specify email folder to move Checked messages to after parsing
        self.checked_folder = 'Crossref_submissions/Checked'

        # Validate required settings
        self._validate()

    def _validate(self):
        """Validate that required IMAP configuration variables are present"""
        if not all([self.imap_server, self.imap_username, self.imap_password]):
            raise ValueError(
                "Missing required IMAP configuration: "
                "IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD")


class CrossrefParser:
    """Crossref-specific logic for parsing error messages received by email
    from Crossref, and augmenting them with data from the Thoth API
    for submission back to Crossref as an email with attached CSV. """

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

        # Extract relevant Crossref submission metadata from email body
        submission_id = bodyxml.findtext('submission_id')
        batch_id = bodyxml.findtext('batch_id')

        logging.info(f"Processing Crossref Submission ID: {submission_id}")

        # Extract Thoth Work ID from Crossef batch_id in email
        thoth_work_id = None
        thoth_work_id_url = None
        if batch_id and '_' in batch_id:
            thoth_work_id = batch_id.split('_')[0]
            thoth_work_id_url = f"https://thoth.pub/books/{thoth_work_id}"

        # Extract specific diagnostic error information from email
        # record_diagnostic is a direct child of root element
        diagnostic = bodyxml.find('record_diagnostic')
        msg_id = (diagnostic.attrib.get('msg_id')
                  if diagnostic is not None else None)
        msg = diagnostic.find('msg') if diagnostic is not None else None

        # Get Work DOI, Title and Subtitle from Thoth API
        doi, title, subtitle = self._fetch_thoth_data(thoth_work_id)

        return {
            'submission_id': submission_id,
            'batch_id': batch_id,
            'thoth_record_url': thoth_work_id_url,
            'work_title': title,
            'work_subtitle': subtitle,
            'doi': doi,
            'crossref_error_msg_id': msg_id,
            'crossref_error_message': msg.text if msg is not None else None
        }

    def _fetch_thoth_data(self, thoth_work_id: str) -> tuple[Optional[str],
                                                             Optional[str],
                                                             Optional[str]]:
        """Fetch DOI and title from Thoth API"""

        if not thoth_work_id:
            return None, None, None

        query = '{ work(workId: "%s") { doi title subtitle } }' % thoth_work_id

        try:
            response = requests.post(self.thoth_api_url, json={'query': query})
            response.raise_for_status()
            data = response.json()
            work = data.get('data', {}).get('work', {})
            doi = work.get('doi')
            title = work.get('title')
            subtitle = work.get('subtitle')
            logging.info(f"Retrieved Thoth data - DOI: {doi}")
            return doi, title, subtitle
        except requests.exceptions.HTTPError as e:
            logging.error(f"Thoth API HTTP error: {e}")
            return None, None, None
        except Exception as e:
            logging.error(f"Error fetching Thoth data: {e}")
            return None, None, None


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
        self.csv_writer = CSVWriter("{}_{}.csv".format(
            CROSSREF_CSV_FILENAME,
            date.today().isoformat()
        ))

    @classmethod
    def run(cls):
        """Class method to run the Crossref email processor"""

        try:
            config = Config()
            processor = cls(config)
            if processor.process_emails():
                logging.info("Crossref email processing completed")
            else:
                logging.error("Crossref email processing failed")
                sys.exit(1)
        except ValueError as e:
            logging.error(f"Configuration error: {e}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            sys.exit(1)

    def process_emails(self) -> bool:
        """Main email processing workflow"""

        try:
            # Connect to email server
            if not self.email_fetcher.connect():
                return False

            # Check the mailbox layout before touching any message, so that
            # a mis-configured label cannot silently look like a clean week
            # nor strand processed messages with nowhere to be filed
            if not self._prepare_folders():
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
                    if not self.email_fetcher.move_message(
                            msg_uid, source_folder,
                            self.config.checked_folder):
                        logging.error(
                            f"Message {msg_uid} was added to the report but "
                            f"could not be moved out of {source_folder}; it "
                            f"will be reported again on the next run unless "
                            f"it is filed manually")
                else:
                    logging.warning(f"Failed to parse message {msg_uid}, "
                                    f"leaving in {source_folder}")
                logging.info("---")

            # Send email report to Crossref if messages were processed
            if messages:
                self._send_report()
            else:
                logging.info("No messages in folders to process, "
                             "skipping sending email")
            return True

        except Exception as e:
            logging.error(f"Error during processing: {e}")
            return False
        finally:
            self.email_fetcher.disconnect()

    def _prepare_folders(self) -> bool:
        """Verify the source folders exist and ensure the checked folder does.

        The Gmail filters create the source labels, but no filter creates the
        label processed messages are filed under, so that one is created on
        demand. A missing source label is treated as a hard failure rather
        than as an empty folder: silently processing nothing would be
        indistinguishable from a week with no Crossref errors.
        """
        missing = [folder for folder in self.config.source_folders
                   if not self.email_fetcher.folder_exists(folder)]
        if missing:
            logging.error(
                f"Source folder(s) not found on the mail server: "
                f"{', '.join(missing)}. Check IMAP_SERVER and IMAP_USERNAME, "
                f"and that these labels exist with exactly these names.")
            return False

        if not self.email_fetcher.ensure_folder_exists(
                self.config.checked_folder):
            logging.error(
                f"Cannot use destination folder "
                f"{self.config.checked_folder}. Create this label manually "
                f"in the mailbox and re-run.")
            return False

        return True

    def _send_report(self):
        """Send email report to Crossref with CSV attachment"""

        if not (self.config.smtp_url and self.config.recipient_email):
            logging.error("SMTP credentials not provided - cannot send "
                          "email report")
            sys.exit(1)

        try:
            server, port, smtp_user, smtp_pass = parse_smtp_url(
                self.config.smtp_url)
            email_sender = EmailSender(server, port, smtp_user, smtp_pass)

            success = email_sender.send_email_with_attachment(
                recipient=self.config.recipient_email,
                subject=CROSSREF_EMAIL_SUBJECT,
                body=CROSSREF_EMAIL_BODY,
                sender=EMAIL_SENDER,
                cc=EMAIL_CC,
                attachment_path=self.csv_writer.csv_path
            )

            if success:
                logging.info("Email report sent successfully")
            else:
                logging.error("Failed to send email report")
                sys.exit(1)

        except Exception as e:
            logging.error(f"Error sending email: {e}")
            sys.exit(1)
