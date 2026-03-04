#!/usr/bin/env python3
import email
import os
import sys
from dotenv import load_dotenv
import logging
import pandas as pd
from io import BytesIO
from typing import Dict, Any, Optional
from email_utils import EmailFetcher
from thothlibrary import ThothClient, ThothError

# General constants
DEFAULT_THOTH_API_URL = 'https://api.thoth.pub/graphql'

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

        # Get Thoth credentials from env
        self.thoth_username = os.environ.get('THOTH_EMAIL')
        self.thoth_password = os.environ.get('THOTH_PWD')

        # TODO folder names TBC
        # Specify email folder for reading MUSE inventory messages
        self.source_folder = 'INBOX/MUSE_inventory'
        # Specify email folder to move Processed messages to after parsing
        self.processed_folder = 'INBOX/MUSE_inventory/Processed'

        # Validate required settings
        self._validate()

    def _validate(self):
        """Validate that required IMAP and Thoth configuration variables are present"""
        if not all([self.imap_server, self.imap_username, self.imap_password]):
            raise ValueError(
                "Missing required IMAP configuration: "
                "IMAP_SERVER, IMAP_USERNAME, IMAP_PASSWORD")
        if not all([self.thoth_username, self.thoth_password]):
            raise ValueError(
                "Missing required Thoth credentials: "
                "THOTH_EMAIL, THOTH_PASSWORD")


class MUSEParser:
    """MUSE-specific logic for parsing inventory messages received by email
    from MUSE, and writing their data to the Thoth API."""

    def __init__(self, thoth, thoth_api_url: str = DEFAULT_THOTH_API_URL):
        self.thoth = thoth
        self.thoth_api_url = thoth_api_url

    def parse_message(self, email_message: email.message.EmailMessage
                      ) -> Optional[Dict[str, Any]]:
        """Parse a single MUSE email message and return extracted data from attachment"""
        for part in email_message.walk():
            if part.get_content_maintype() != 'multipart' and part.get('Content-Disposition') is not None:
                # TODO we could optionally check this if we expect a standard filename
                # filename = part.get_filename()
                file = part.get_payload(decode=True)

        try:
            data = pd.read_excel(BytesIO(file))
        except (ValueError, NameError):
            raise ValueError('Email attachment not found, or not in expected Excel format')

        locations = []
        for row in data.index:
            try:
                isbn = str(data.at[row, 'online_identifier']).strip()
                publications = self.thoth.publications(search=isbn)
                if len(publications) == 0:
                    raise ValueError(f"No publications found for ISBN {isbn}")
                # We may have submitted either PDF or EPUB or both - no way to check
                # Assume that the set of publications remains unchanged since submission
                # and add locations to all relevant publications accordingly
                elif len(publications) > 1:
                    pdfs = [n for n in publications if n.publicationType == 'PDF']
                    epubs = [n for n in publications if n.publicationType == 'EPUB']
                    if not pdfs and not epubs:
                        raise ValueError(f"No PDF or EPUB publications found for {isbn}")
                    elif len(pdfs) > 1 or len(epubs) > 1:
                        raise ValueError(f"Multiple publications of same type found for {isbn}")
                landing_page = data.at[row, 'title_url']
            except KeyError:
                raise ValueError('Excel spreadsheet missing expected column header')

            for publication in publications:
                if publication.publicationType == 'PDF':
                    full_text_url = '{}/pdf/download'.format(landing_page)
                elif publication.publicationType == 'EPUB':
                    full_text_url = '{}/epub'.format(landing_page),
                else:
                    continue
                location = {
                    'publicationId': publication.publicationId,
                    'landingPage': landing_page,
                    'fullTextUrl': full_text_url,
                    'locationPlatform': 'PROJECT_MUSE',
                    'canonical': 'false'
                }
                locations.append(location)

        return locations


class MUSEEmailProcessor:
    """Main application class for processing MUSE inventory emails"""

    def __init__(self, config: Config):
        self.config = config
        self.email_fetcher = EmailFetcher(
            config.imap_server,
            config.imap_username,
            config.imap_password
        )
        self.thoth = ThothClient()
        try:
            self.thoth.login(config.thoth_username, config.thoth_password)
        except ThothError:
            raise ValueError('Thoth login failed: credentials may be incorrect')
        self.parser = MUSEParser(self.thoth)

    @classmethod
    def run(cls):
        """Class method to run the MUSE email processor"""

        try:
            config = Config()
            processor = cls(config)
            if processor.process_emails():
                logging.info("MUSE email processing completed")
            else:
                logging.error("MUSE email processing failed")
                sys.exit(1)
        except ValueError as e:
            logging.error(f"Configuration error: {e}")
            sys.exit(1)
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            sys.exit(1)

    def process_emails(self) -> bool:
        """Main email processing workflow"""

        locations = []
        success = True

        try:
            # Connect to email server
            if not self.email_fetcher.connect():
                return False

            # Fetch and process messages
            messages = self.email_fetcher.fetch_messages_from_folder(
                self.config.source_folder)
            logging.info(f"Total messages fetched: {len(messages)}")

            # Process each message
            for email_message, msg_uid, source_folder in messages:
                parsed_data = self.parser.parse_message(email_message)
                if parsed_data:
                    locations.extend(parsed_data)
                    # Move email to processed folder after successful processing
                    self.email_fetcher.move_message(
                        msg_uid, source_folder, self.config.processed_folder)
                else:
                    logging.warning(f"Failed to parse message {msg_uid}, "
                                    f"leaving in {source_folder}")
                logging.info("---")

            for location in locations:
                try:
                    # TODO check that location hasn't already been created in a previous run?
                    # (Awaiting confirmation whether or not regular spreadsheets might contain duplicates)
                    location_id = self.parser.thoth.create_location(location)
                    logging.info(f"Created Thoth location with ID: {location_id}")
                except Exception as e:
                    logging.error(f"Error creating Thoth location: {e}")
                    success = False

            return success

        except Exception as e:
            logging.error(f"Error during processing: {e}")
            return False
        finally:
            self.email_fetcher.disconnect()
