"""
Email utilities for IMAP and SMTP operations.

This module provides reusable email utilities that can be used
across different email automation workflows.
"""

import imaplib
import smtplib
import os
import email.mime.text
import email.mime.multipart
import email.mime.base
from email import encoders
import pandas as pd
import logging
from typing import List, Dict, Any
from urllib.parse import urlparse

# General constants
DEFAULT_SMTP_PORT = 587


class EmailFetcher:
    """Fetches emails from specified folders using IMAP"""

    def __init__(self, server: str, username: str, password: str):
        self.server = server
        self.username = username
        self.password = password
        self.mail = None

    def connect(self) -> bool:
        """
        Establish connection to IMAP server using SSL.

        Returns:
            bool: True if connection successful, False otherwise

        Logs connection status and any errors encountered.
        """
        try:
            logging.info("Connecting to email server...")
            self.mail = imaplib.IMAP4_SSL(self.server)
            self.mail.login(self.username, self.password)
            return True
        except Exception as e:
            logging.error(f"Failed to connect to email server: {e}")
            return False

    def disconnect(self):
        """
        Safely close IMAP connection and logout.

        Handles cleanup even if connection is already closed or errors occur.
        Always sets self.mail to None to prevent reuse of stale connection.
        """
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
        """Move a message from source folder to destination folder using UID"""
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
                # Upgrade to TLS
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)

            logging.info(f"Email sent successfully to {recipient}")
            return True

        except Exception as e:
            logging.error(f"Failed to send email: {e}")
            return False


class CSVWriter:
    """Handles CSV file operations"""

    def __init__(self, csv_path: str):
        self.csv_path = csv_path

    def write_row(self, row_data: Dict[str, Any]):
        """Write a single row to CSV"""
        try:
            df = pd.read_csv(self.csv_path)
            df = pd.concat([df, pd.DataFrame([row_data])], ignore_index=True)
        except FileNotFoundError:
            df = pd.DataFrame([row_data])

        df.to_csv(self.csv_path, index=False)


def parse_smtp_url(smtp_url: str) -> tuple[str, int, str, str]:
    """Parse SMTP URL format: smtp://username:password@server:port"""
    parsed = urlparse(smtp_url)
    server = parsed.hostname
    port = parsed.port or DEFAULT_SMTP_PORT
    username = parsed.username
    password = parsed.password

    return server, port, username, password
