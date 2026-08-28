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
import csv
from email import encoders
import logging
from typing import List, Dict, Any
from urllib.parse import urlparse

# General constants
DEFAULT_SMTP_PORT = 587
IMAP_OK_STATUS = 'OK'
# Server capabilities we take advantage of when the server advertises them.
# Gmail advertises both.
IMAP_MOVE_CAPABILITY = 'MOVE'
IMAP_UIDPLUS_CAPABILITY = 'UIDPLUS'


def quote_mailbox(mailbox: str) -> str:
    """Return an IMAP-safe quoted form of a mailbox name.

    imaplib does not quote the mailbox arguments it is given, so any name
    containing an atom-special (most commonly a space) has to be quoted by
    the caller. Gmail exposes labels as hierarchical mailbox names such as
    Crossref_submissions/Checked; these happen to be valid unquoted atoms,
    but quoting unconditionally means a label later renamed to something
    containing a space cannot silently break SELECT, LIST, CREATE or MOVE.
    """
    escaped = mailbox.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


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

        UNSELECT is used in preference to CLOSE. CLOSE permanently expunges
        every message already carrying \\Deleted in the selected folder,
        which is exactly the side effect the move logic goes out of its way
        to avoid on Gmail; UNSELECT returns the connection to the
        authenticated state without expunging anything. There is
        deliberately no fall back to CLOSE. It is only issued when a folder
        is actually selected, since it is illegal in the authenticated
        state, and a run that aborts before selecting anything must still be
        able to log out cleanly.
        """
        if not self.mail:
            return

        try:
            if getattr(self.mail, 'state', None) == 'SELECTED':
                self.mail.unselect()
        except Exception as e:
            logging.warning(f"Error unselecting folder: {e}")

        try:
            self.mail.logout()
            logging.info("IMAP connection closed successfully")
        except Exception as e:
            logging.warning(f"Error during IMAP disconnect: {e}")
        finally:
            self.mail = None

    def _server_supports(self, capability: str) -> bool:
        """Check whether the connected server advertises a capability"""
        return capability in getattr(self.mail, 'capabilities', ())

    def folder_exists(self, folder: str) -> bool:
        """Check whether a folder (Gmail label) exists on the server.

        Uses LIST rather than SELECT so the check is side-effect free and
        leaves the currently selected folder alone.
        """
        try:
            status, data = self.mail.list('""', quote_mailbox(folder))
        except Exception as e:
            logging.error(f"Error listing folder {folder}: {e}")
            return False

        if status != IMAP_OK_STATUS:
            logging.error(f"Failed to list folder {folder}: {status}")
            return False

        # A LIST with no matches yields a single empty (None) response line
        return any(line for line in data)

    def ensure_folder_exists(self, folder: str) -> bool:
        """Ensure a destination folder (Gmail label) exists, creating it if
        it does not.

        Gmail's filters create the labels that incoming mail is classified
        into, but nothing creates the label processed messages are filed
        under, so it is created on demand. This is idempotent: an existing
        folder is left untouched, and a folder created concurrently is
        reported by Gmail as NO [ALREADYEXISTS] and treated as success.
        """
        if self.folder_exists(folder):
            return True

        logging.info(f"Folder {folder} not found, attempting to create it")

        try:
            status, data = self.mail.create(quote_mailbox(folder))
        except Exception as e:
            logging.error(f"Error creating folder {folder}: {e}")
            return False

        if status == IMAP_OK_STATUS:
            logging.info(f"Created folder {folder}")
            return True

        detail = b' '.join(line for line in data if line).decode(
            'utf-8', 'replace')
        if 'ALREADYEXISTS' in detail.upper():
            return True

        logging.error(f"Failed to create folder {folder}: {detail}. "
                      f"Create this label manually in the mailbox and "
                      f"re-run.")
        return False

    def move_message(self, msg_uid: str, source_folder: str,
                     destination_folder: str) -> bool:
        """Move a message from source folder to destination folder using UID.

        UID MOVE (RFC 6851) is used wherever the server advertises it, which
        includes Gmail. Under Gmail's IMAP mapping, folders are labels and a
        MOVE between two labels simply removes the source label and adds the
        destination one; the underlying message is untouched and remains in
        All Mail. Crucially it never sets \\Deleted, so Gmail's "when a
        message is expunged from the last visible IMAP folder" setting -
        which can archive, bin or permanently delete - is never triggered.

        Servers without MOVE fall back to COPY, then \\Deleted, then expunge.
        The copy is always verified before the source message is touched, so
        a failure leaves the message in the source folder. UID EXPUNGE
        (RFC 4315) is preferred over EXPUNGE where UIDPLUS is advertised,
        because a bare EXPUNGE removes every \\Deleted message in the folder,
        not just this one.
        """
        try:
            # Select the source folder
            status, _ = self.mail.select(quote_mailbox(source_folder))
            if status != IMAP_OK_STATUS:
                logging.error(f"Failed to select source folder "
                              f"{source_folder}")
                return False

            if self._server_supports(IMAP_MOVE_CAPABILITY):
                moved = self._move_via_move(msg_uid, destination_folder)
            else:
                moved = self._move_via_copy(msg_uid, destination_folder)

            if not moved:
                return False

            logging.info(f"Moved message {msg_uid} from {source_folder} to "
                         f"{destination_folder}")
            return True

        except Exception as e:
            logging.error(f"Error moving message {msg_uid}: {e}")
            return False

    def _move_via_move(self, msg_uid: str, destination_folder: str) -> bool:
        """Relocate a message with UID MOVE (preferred; used by Gmail)"""
        status, data = self.mail.uid('move', msg_uid,
                                     quote_mailbox(destination_folder))
        if status != IMAP_OK_STATUS:
            logging.error(f"Failed to move message {msg_uid} to "
                          f"{destination_folder}: {data}")
            return False
        return True

    def _move_via_copy(self, msg_uid: str, destination_folder: str) -> bool:
        """Relocate a message with COPY then \\Deleted then expunge.

        Only used against servers that do not advertise MOVE. The source
        message is only ever flagged once the copy has been confirmed.
        """
        # Copy message to destination folder using UID
        status, data = self.mail.uid('copy', msg_uid,
                                     quote_mailbox(destination_folder))
        if status != IMAP_OK_STATUS:
            logging.error(f"Failed to copy message {msg_uid} to "
                          f"{destination_folder}: {data}. Leaving message in "
                          f"place.")
            return False

        # Mark original message for deletion using UID
        status, _ = self.mail.uid('store', msg_uid, '+FLAGS', '\\Deleted')
        if status != IMAP_OK_STATUS:
            logging.error(f"Failed to mark message {msg_uid} for deletion")
            return False

        # Expunge to actually remove the message from the source folder,
        # scoped to this UID where the server supports it
        if self._server_supports(IMAP_UIDPLUS_CAPABILITY):
            self.mail.uid('expunge', msg_uid)
        else:
            self.mail.expunge()

        return True

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
        status, folder_messages = self.mail.select(quote_mailbox(folder))
        if status != IMAP_OK_STATUS:
            logging.error(f"Failed to select {folder}: {status}")
            return messages

        logging.info(f"{folder} contains {int(folder_messages[0])} messages")

        # Search for all messages using UID
        status, message_uids = self.mail.uid('search', None, 'ALL')
        if status != IMAP_OK_STATUS:
            logging.error(f"Failed to search messages in {folder}")
            return messages

        message_uid_list = message_uids[0].split()

        # Fetch each message using UID
        for msg_uid in message_uid_list:
            status, msg_data = self.mail.uid('fetch', msg_uid, '(RFC822)')
            if status == IMAP_OK_STATUS:
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
                                   body: str, sender: str, cc: str | None,
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
            msg['Cc'] = cc

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
                # Don't send email if attachment path was given but no attachment was found there
                logging.error(f"Attachment file not found: "
                              f"{attachment_path}")
                return False

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
    """Handles CSV file operations using standard library only"""

    def __init__(self, csv_path: str):
        self.csv_path = csv_path

    def write_row(self, row_data: Dict[str, Any]):
        """Write a single row to CSV using standard library csv module"""

        # Determine if file exists and has content
        file_exists = os.path.exists(self.csv_path)
        file_has_content = file_exists and os.path.getsize(self.csv_path) > 0

        # Get column headers from the row data
        headers = list(row_data.keys())

        with open(self.csv_path, 'a', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)

            # Write headers only if file is new or empty
            if not file_has_content:
                writer.writeheader()

            writer.writerow(row_data)


def parse_smtp_url(smtp_url: str) -> tuple[str, int, str, str]:
    """Parse SMTP URL format: smtp://username:password@server:port"""
    parsed = urlparse(smtp_url)
    server = parsed.hostname
    port = parsed.port or DEFAULT_SMTP_PORT
    username = parsed.username
    password = parsed.password

    return server, port, username, password
