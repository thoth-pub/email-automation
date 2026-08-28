#!/usr/bin/env python3
"""
Tests for the Crossref error report automation.

These tests cover the Gmail label configuration and the folder checks the
processor performs before it touches any message. No real mail server,
credentials or network access are involved.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import crossref_error_report
from crossref_error_report import Config, CrossrefEmailProcessor
from test_email_utils import FakeIMAP, make_fetcher

# Labels created by the Gmail filters that classify incoming Crossref mail
GMAIL_SOURCE_LABELS = [
    'Crossref_submissions/Error_reports/ISBN_already_assigned',
    'Crossref_submissions/Error_reports/ISSN_already_assigned',
]
GMAIL_CHECKED_LABEL = 'Crossref_submissions/Checked'

# Migrated Fastmail hierarchy. Present in the mailbox, holding historical
# mail only, and never a valid production source or destination.
LEGACY_LABEL_PREFIX = 'INBOX/Crossref_submissions'

# Modules that run in production. None of them may reference the legacy
# Fastmail hierarchy.
PRODUCTION_MODULES = [
    'crossref_error_report.py',
    'email_utils.py',
    'email_automator.py',
]

FAKE_ENV = {
    'IMAP_SERVER': 'imap.gmail.com',
    'IMAP_USERNAME': 'automation@thoth.pub',
    'IMAP_PASSWORD': 'not-a-real-app-password',
    'THOTH_SMTP': 'smtp://user:pass@smtp.example.com:587',
    'CROSSREF_EMAIL': 'crossref@example.com',
}


def make_config():
    """Build a Config without reading config.env or the real environment"""
    with patch.object(crossref_error_report, 'load_dotenv'), \
            patch.dict(os.environ, FAKE_ENV, clear=True):
        return Config()


def make_processor(mail=None):
    """Build a processor with a fetcher wired to a fake Gmail mailbox"""
    with patch.object(crossref_error_report, 'CSVWriter'):
        processor = CrossrefEmailProcessor(make_config())
    processor.email_fetcher = make_fetcher(mail if mail else FakeIMAP())
    processor.csv_writer = MagicMock()
    return processor


class CrossrefLabelConfigTests(unittest.TestCase):
    """Configuration must name Gmail's native labels, not the migrated ones"""

    def setUp(self):
        self.config = make_config()

    def test_source_folders_are_the_gmail_native_labels(self):
        self.assertEqual(self.config.source_folders, GMAIL_SOURCE_LABELS)

    def test_source_folders_are_not_the_migrated_inbox_labels(self):
        for folder in self.config.source_folders:
            self.assertFalse(
                folder.startswith('INBOX/'),
                f"{folder} is a migrated Fastmail label holding historical "
                f"mail and must not be a production source")

    def test_checked_folder_is_the_gmail_native_label(self):
        self.assertEqual(self.config.checked_folder, GMAIL_CHECKED_LABEL)

    def test_checked_folder_is_not_the_migrated_inbox_label(self):
        self.assertFalse(self.config.checked_folder.startswith('INBOX/'))


class LegacyLabelStaticTests(unittest.TestCase):
    """No production module may still refer to the migrated hierarchy"""

    def test_no_production_module_references_the_legacy_hierarchy(self):
        for module in PRODUCTION_MODULES:
            with open(module, encoding='utf-8') as source_file:
                source = source_file.read()
            offending = [
                line.strip() for line in source.splitlines()
                if LEGACY_LABEL_PREFIX in line
                and not line.lstrip().startswith('#')
            ]
            self.assertEqual(
                offending, [],
                f"{module} still refers to {LEGACY_LABEL_PREFIX} outside of "
                f"an explanatory comment")


class PrepareFoldersTests(unittest.TestCase):
    """The mailbox layout is checked before any message is touched"""

    def test_checked_label_is_created_when_missing(self):
        mail = FakeIMAP()
        processor = make_processor(mail)
        self.assertTrue(processor._prepare_folders())
        self.assertIn(('create', GMAIL_CHECKED_LABEL), mail.calls)
        self.assertIn(GMAIL_CHECKED_LABEL, mail.mailboxes)

    def test_existing_checked_label_is_left_alone(self):
        mail = FakeIMAP(FakeIMAP().mailboxes + [GMAIL_CHECKED_LABEL])
        processor = make_processor(mail)
        self.assertTrue(processor._prepare_folders())
        self.assertEqual(
            [call for call in mail.calls if call[0] == 'create'], [])

    def test_missing_source_label_fails_without_creating_anything(self):
        remaining = [name for name in FakeIMAP().mailboxes
                     if name != GMAIL_SOURCE_LABELS[1]]
        mail = FakeIMAP(remaining)
        processor = make_processor(mail)
        self.assertFalse(processor._prepare_folders())
        # A mis-configured source must not cause any label to be created
        self.assertEqual(
            [call for call in mail.calls if call[0] == 'create'], [])

    def test_uncreatable_checked_label_fails(self):
        mail = MagicMock()
        mail.list.side_effect = [
            ('OK', [b'(\\HasNoChildren) "/" "a"']),  # ISBN source found
            ('OK', [b'(\\HasNoChildren) "/" "b"']),  # ISSN source found
            ('OK', [None]),                          # Checked not found
        ]
        mail.create.return_value = ('NO', [b'[CANNOT] Invalid label name'])
        processor = make_processor(mail)
        self.assertFalse(processor._prepare_folders())

    def test_migrated_labels_are_never_consulted(self):
        mail = FakeIMAP()
        processor = make_processor(mail)
        processor._prepare_folders()
        consulted = [call[-1] for call in mail.calls]
        for name in consulted:
            self.assertNotIn(LEGACY_LABEL_PREFIX, name)


class ProcessEmailsTests(unittest.TestCase):
    """Processing must not start until the mailbox layout is confirmed"""

    def test_no_messages_are_fetched_when_folder_checks_fail(self):
        processor = make_processor()
        processor.email_fetcher = MagicMock()
        processor.email_fetcher.connect.return_value = True
        processor._prepare_folders = MagicMock(return_value=False)

        self.assertFalse(processor.process_emails())
        processor.email_fetcher.fetch_messages_from_folders.assert_not_called()
        processor.email_fetcher.move_message.assert_not_called()
        processor.email_fetcher.disconnect.assert_called_once()

    def test_processed_message_is_moved_to_the_checked_label(self):
        processor = make_processor()
        processor.email_fetcher = MagicMock()
        processor.email_fetcher.connect.return_value = True
        processor.email_fetcher.fetch_messages_from_folders.return_value = [
            ('message', '42', GMAIL_SOURCE_LABELS[0]),
        ]
        processor.email_fetcher.move_message.return_value = True
        processor.parser = MagicMock()
        processor.parser.parse_message.return_value = {'submission_id': '1'}
        processor._prepare_folders = MagicMock(return_value=True)
        processor._send_report = MagicMock()

        self.assertTrue(processor.process_emails())
        processor.csv_writer.write_row.assert_called_once_with(
            {'submission_id': '1'})
        processor.email_fetcher.move_message.assert_called_once_with(
            '42', GMAIL_SOURCE_LABELS[0], GMAIL_CHECKED_LABEL)

    def test_unparseable_message_is_left_in_its_source_label(self):
        processor = make_processor()
        processor.email_fetcher = MagicMock()
        processor.email_fetcher.connect.return_value = True
        processor.email_fetcher.fetch_messages_from_folders.return_value = [
            ('message', '42', GMAIL_SOURCE_LABELS[0]),
        ]
        processor.parser = MagicMock()
        processor.parser.parse_message.return_value = None
        processor._prepare_folders = MagicMock(return_value=True)
        processor._send_report = MagicMock()

        self.assertTrue(processor.process_emails())
        processor.csv_writer.write_row.assert_not_called()
        processor.email_fetcher.move_message.assert_not_called()

    def test_failed_move_is_reported_and_does_not_halt_processing(self):
        processor = make_processor()
        processor.email_fetcher = MagicMock()
        processor.email_fetcher.connect.return_value = True
        processor.email_fetcher.fetch_messages_from_folders.return_value = [
            ('message', '42', GMAIL_SOURCE_LABELS[0]),
            ('message', '43', GMAIL_SOURCE_LABELS[1]),
        ]
        processor.email_fetcher.move_message.side_effect = [False, True]
        processor.parser = MagicMock()
        processor.parser.parse_message.return_value = {'submission_id': '1'}
        processor._prepare_folders = MagicMock(return_value=True)
        processor._send_report = MagicMock()

        with self.assertLogs(level='ERROR') as logs:
            self.assertTrue(processor.process_emails())

        self.assertTrue(
            any('could not be moved out of' in line for line in logs.output))
        # The second message is still processed and reported
        self.assertEqual(processor.csv_writer.write_row.call_count, 2)
        processor._send_report.assert_called_once()


if __name__ == '__main__':
    unittest.main()
