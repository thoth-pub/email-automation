#!/usr/bin/env python3
"""
Tests for the reusable IMAP utilities in email_utils.

These tests use only the standard library and never touch a real mail
server: every IMAP interaction is either a MagicMock or the small FakeIMAP
stand-in below, which answers LIST and CREATE against an in-memory mailbox
list modelled on the Gmail hierarchy Thoth's mailbox now presents.
"""

import unittest
from unittest.mock import MagicMock

from email_utils import EmailFetcher, quote_mailbox

# Capabilities as advertised by Gmail's IMAP server. Gmail supports both
# MOVE (RFC 6851) and UIDPLUS (RFC 4315).
GMAIL_CAPABILITIES = (
    'IMAP4REV1', 'UNSELECT', 'IDLE', 'NAMESPACE', 'QUOTA', 'ID', 'XLIST',
    'CHILDREN', 'X-GM-EXT-1', 'UIDPLUS', 'COMPRESS=DEFLATE', 'ENABLE',
    'MOVE', 'CONDSTORE', 'ESEARCH', 'UTF8=ACCEPT', 'LIST-EXTENDED',
    'LIST-STATUS', 'LITERAL-', 'SPECIAL-USE', 'APPENDLIMIT',
)

# The mailbox layout Gmail presents after the Fastmail migration: the
# Gmail-native hierarchy created by the imported filters, alongside the
# migrated Fastmail folders which were imported as literal INBOX/... labels.
# Note that Crossref_submissions/Checked is deliberately absent - no
# incoming filter creates it.
GMAIL_MAILBOXES = (
    'INBOX',
    '[Gmail]/All Mail',
    '[Gmail]/Sent Mail',
    '[Gmail]/Trash',
    # Gmail-native hierarchy, created by the imported Gmail filters
    'Crossref_submissions',
    'Crossref_submissions/Error_reports',
    'Crossref_submissions/Error_reports/ISBN_already_assigned',
    'Crossref_submissions/Error_reports/ISSN_already_assigned',
    # Legacy labels migrated from Fastmail: historical mail only, and must
    # never be used as a production source or destination
    'INBOX/Crossref_submissions',
    'INBOX/Crossref_submissions/Error_reports',
    'INBOX/Crossref_submissions/Error_reports/ISBN_already_assigned',
    'INBOX/Crossref_submissions/Error_reports/ISSN_already_assigned',
    'INBOX/Crossref_submissions/Checked',
)


def unquote_mailbox(value: str) -> str:
    """Reverse quote_mailbox, for use by the fake server"""
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.replace('\\"', '"').replace('\\\\', '\\')


class FakeIMAP:
    """Minimal stand-in for imaplib.IMAP4_SSL answering LIST and CREATE"""

    def __init__(self, mailboxes=GMAIL_MAILBOXES,
                 capabilities=GMAIL_CAPABILITIES):
        self.mailboxes = list(mailboxes)
        self.capabilities = capabilities
        self.state = 'AUTH'
        self.calls = []

    def list(self, directory='""', pattern='*'):
        self.calls.append(('list', directory, pattern))
        name = unquote_mailbox(pattern)
        if name in self.mailboxes:
            line = f'(\\HasNoChildren) "/" "{name}"'.encode()
            return 'OK', [line]
        # Gmail returns a single empty response line when nothing matches
        return 'OK', [None]

    def create(self, mailbox):
        name = unquote_mailbox(mailbox)
        self.calls.append(('create', name))
        if name in self.mailboxes:
            return 'NO', [b'[ALREADYEXISTS] Duplicate folder name (Failure)']
        self.mailboxes.append(name)
        return 'OK', [b'Success']


def make_fetcher(mail):
    """Build an EmailFetcher wired to an already-connected fake server"""
    fetcher = EmailFetcher('imap.gmail.com', 'user@thoth.pub', 'app-password')
    fetcher.mail = mail
    return fetcher


def make_mock_mail(capabilities=GMAIL_CAPABILITIES):
    """Build a MagicMock IMAP connection whose commands all succeed"""
    mail = MagicMock()
    mail.capabilities = capabilities
    mail.state = 'SELECTED'
    mail.select.return_value = ('OK', [b'1'])
    mail.uid.return_value = ('OK', [b'Success'])
    return mail


class QuoteMailboxTests(unittest.TestCase):
    """quote_mailbox must produce valid IMAP quoted strings"""

    def test_hierarchical_gmail_label_is_quoted(self):
        self.assertEqual(quote_mailbox('Crossref_submissions/Checked'),
                         '"Crossref_submissions/Checked"')

    def test_label_containing_space_is_quoted(self):
        self.assertEqual(quote_mailbox('[Gmail]/All Mail'),
                         '"[Gmail]/All Mail"')

    def test_special_characters_are_escaped(self):
        self.assertEqual(quote_mailbox('odd"name'), '"odd\\"name"')
        self.assertEqual(quote_mailbox('odd\\name'), '"odd\\\\name"')


class FolderExistsTests(unittest.TestCase):
    """folder_exists must distinguish present from absent Gmail labels"""

    def setUp(self):
        self.mail = FakeIMAP()
        self.fetcher = make_fetcher(self.mail)

    def test_existing_gmail_native_label_is_found(self):
        self.assertTrue(self.fetcher.folder_exists(
            'Crossref_submissions/Error_reports/ISBN_already_assigned'))

    def test_absent_label_is_not_found(self):
        self.assertFalse(
            self.fetcher.folder_exists('Crossref_submissions/Checked'))

    def test_lookup_is_quoted_and_scoped_to_the_exact_name(self):
        self.fetcher.folder_exists('Crossref_submissions/Checked')
        self.assertIn(
            ('list', '""', '"Crossref_submissions/Checked"'), self.mail.calls)

    def test_non_ok_status_is_treated_as_absent(self):
        mail = MagicMock()
        mail.list.return_value = ('NO', [b'Failure'])
        self.assertFalse(make_fetcher(mail).folder_exists('Whatever'))

    def test_exception_is_treated_as_absent(self):
        mail = MagicMock()
        mail.list.side_effect = OSError('connection reset')
        self.assertFalse(make_fetcher(mail).folder_exists('Whatever'))


class EnsureFolderExistsTests(unittest.TestCase):
    """ensure_folder_exists must be idempotent and provider-safe"""

    def setUp(self):
        self.mail = FakeIMAP()
        self.fetcher = make_fetcher(self.mail)

    def test_existing_folder_is_not_recreated(self):
        self.assertTrue(self.fetcher.ensure_folder_exists(
            'Crossref_submissions/Error_reports/ISBN_already_assigned'))
        self.assertEqual(
            [call for call in self.mail.calls if call[0] == 'create'], [])

    def test_missing_checked_folder_is_created(self):
        self.assertTrue(
            self.fetcher.ensure_folder_exists('Crossref_submissions/Checked'))
        self.assertIn(('create', 'Crossref_submissions/Checked'),
                      self.mail.calls)
        self.assertIn('Crossref_submissions/Checked', self.mail.mailboxes)

    def test_creation_is_idempotent(self):
        self.assertTrue(
            self.fetcher.ensure_folder_exists('Crossref_submissions/Checked'))
        creates_before = len(
            [call for call in self.mail.calls if call[0] == 'create'])
        self.assertTrue(
            self.fetcher.ensure_folder_exists('Crossref_submissions/Checked'))
        creates_after = len(
            [call for call in self.mail.calls if call[0] == 'create'])
        # The second call finds the folder via LIST and does not re-create it
        self.assertEqual(creates_before, creates_after)

    def test_already_exists_race_is_treated_as_success(self):
        mail = MagicMock()
        # LIST misses, but CREATE reports the folder already exists
        mail.list.return_value = ('OK', [None])
        mail.create.return_value = (
            'NO', [b'[ALREADYEXISTS] Duplicate folder name (Failure)'])
        self.assertTrue(make_fetcher(mail).ensure_folder_exists(
            'Crossref_submissions/Checked'))

    def test_creation_failure_is_reported(self):
        mail = MagicMock()
        mail.list.return_value = ('OK', [None])
        mail.create.return_value = ('NO', [b'[CANNOT] Invalid label name'])
        self.assertFalse(make_fetcher(mail).ensure_folder_exists(
            'Crossref_submissions/Checked'))

    def test_creation_exception_is_reported(self):
        mail = MagicMock()
        mail.list.return_value = ('OK', [None])
        mail.create.side_effect = OSError('connection reset')
        self.assertFalse(make_fetcher(mail).ensure_folder_exists(
            'Crossref_submissions/Checked'))


class MoveMessageGmailTests(unittest.TestCase):
    """Against Gmail, moves must go through UID MOVE and never set Deleted"""

    def setUp(self):
        self.mail = make_mock_mail()
        self.fetcher = make_fetcher(self.mail)
        self.source = (
            'Crossref_submissions/Error_reports/ISBN_already_assigned')
        self.destination = 'Crossref_submissions/Checked'

    def uid_commands(self):
        return [call.args[0] for call in self.mail.uid.call_args_list]

    def test_successful_move_uses_uid_move(self):
        self.assertTrue(self.fetcher.move_message(
            '42', self.source, self.destination))
        self.mail.select.assert_called_once_with(
            '"Crossref_submissions/Error_reports/ISBN_already_assigned"')
        self.mail.uid.assert_called_once_with(
            'move', '42', '"Crossref_submissions/Checked"')

    def test_successful_move_does_not_delete_or_expunge(self):
        self.assertTrue(self.fetcher.move_message(
            '42', self.source, self.destination))
        self.assertNotIn('store', self.uid_commands())
        self.assertNotIn('copy', self.uid_commands())
        self.mail.expunge.assert_not_called()

    def test_failure_to_select_source_leaves_message_untouched(self):
        self.mail.select.return_value = ('NO', [b'Unknown Mailbox'])
        self.assertFalse(self.fetcher.move_message(
            '42', self.source, self.destination))
        self.mail.uid.assert_not_called()
        self.mail.expunge.assert_not_called()

    def test_failed_move_leaves_message_in_source(self):
        self.mail.uid.return_value = ('NO', [b'[TRYCREATE] No such folder'])
        self.assertFalse(self.fetcher.move_message(
            '42', self.source, self.destination))
        # The only command issued is the MOVE itself: nothing marks the
        # source message deleted, so it survives for the next run
        self.assertEqual(self.uid_commands(), ['move'])
        self.mail.expunge.assert_not_called()

    def test_unexpected_error_is_reported_as_failure(self):
        self.mail.uid.side_effect = OSError('connection reset')
        self.assertFalse(self.fetcher.move_message(
            '42', self.source, self.destination))
        self.mail.expunge.assert_not_called()


class MoveMessageFallbackTests(unittest.TestCase):
    """Servers without MOVE fall back to COPY, then Deleted, then expunge"""

    def setUp(self):
        self.capabilities = ('IMAP4REV1', 'UIDPLUS')
        self.mail = make_mock_mail(self.capabilities)
        self.fetcher = make_fetcher(self.mail)
        self.source = (
            'Crossref_submissions/Error_reports/ISSN_already_assigned')
        self.destination = 'Crossref_submissions/Checked'

    def uid_commands(self):
        return [call.args[0] for call in self.mail.uid.call_args_list]

    def test_copy_then_delete_then_scoped_expunge(self):
        self.assertTrue(self.fetcher.move_message(
            '7', self.source, self.destination))
        self.assertEqual(self.uid_commands(), ['copy', 'store', 'expunge'])
        # UID EXPUNGE is scoped to this message, unlike a bare EXPUNGE
        self.mail.uid.assert_any_call('expunge', '7')
        self.mail.expunge.assert_not_called()

    def test_bare_expunge_only_without_uidplus(self):
        mail = make_mock_mail(('IMAP4REV1',))
        fetcher = make_fetcher(mail)
        self.assertTrue(fetcher.move_message(
            '7', self.source, self.destination))
        mail.expunge.assert_called_once()

    def test_copy_is_ordered_before_the_source_is_flagged(self):
        self.fetcher.move_message('7', self.source, self.destination)
        commands = self.uid_commands()
        self.assertLess(commands.index('copy'), commands.index('store'))

    def test_failed_copy_never_flags_or_expunges_the_source(self):
        self.mail.uid.return_value = ('NO', [b'[TRYCREATE] No such folder'])
        self.assertFalse(self.fetcher.move_message(
            '7', self.source, self.destination))
        self.assertEqual(self.uid_commands(), ['copy'])
        self.mail.expunge.assert_not_called()

    def test_failed_flagging_never_expunges(self):
        self.mail.uid.side_effect = [
            ('OK', [b'Success']),        # copy
            ('NO', [b'Failure']),        # store
        ]
        self.assertFalse(self.fetcher.move_message(
            '7', self.source, self.destination))
        self.assertEqual(self.uid_commands(), ['copy', 'store'])
        self.mail.expunge.assert_not_called()


class DisconnectTests(unittest.TestCase):
    """Disconnect must never expunge, and must always log out.

    CLOSE permanently expunges anything already flagged \\Deleted in the
    selected folder, so disconnect uses UNSELECT instead.
    """

    def test_selected_folder_is_unselected_before_logout(self):
        mail = make_mock_mail()
        mail.state = 'SELECTED'
        fetcher = make_fetcher(mail)
        fetcher.disconnect()
        mail.unselect.assert_called_once()
        mail.logout.assert_called_once()
        self.assertIsNone(fetcher.mail)

    def test_close_is_never_called(self):
        for state in ('SELECTED', 'AUTH'):
            with self.subTest(state=state):
                mail = make_mock_mail()
                mail.state = state
                make_fetcher(mail).disconnect()
                mail.close.assert_not_called()
                mail.expunge.assert_not_called()

    def test_logout_still_happens_when_nothing_is_selected(self):
        mail = make_mock_mail()
        mail.state = 'AUTH'
        fetcher = make_fetcher(mail)
        fetcher.disconnect()
        mail.unselect.assert_not_called()
        mail.close.assert_not_called()
        mail.logout.assert_called_once()
        self.assertIsNone(fetcher.mail)

    def test_unselect_failure_does_not_prevent_logout(self):
        mail = make_mock_mail()
        mail.state = 'SELECTED'
        mail.unselect.side_effect = OSError('connection reset')
        fetcher = make_fetcher(mail)
        fetcher.disconnect()
        mail.logout.assert_called_once()
        self.assertIsNone(fetcher.mail)

    def test_unselect_failure_does_not_fall_back_to_close(self):
        mail = make_mock_mail()
        mail.state = 'SELECTED'
        mail.unselect.side_effect = OSError('connection reset')
        make_fetcher(mail).disconnect()
        # Falling back to CLOSE would reintroduce the expunge we are
        # deliberately avoiding on Gmail
        mail.close.assert_not_called()
        mail.expunge.assert_not_called()


class NoImapCloseInProductionTests(unittest.TestCase):
    """No production module may issue IMAP CLOSE during normal disconnect"""

    def test_production_code_does_not_call_imap_close(self):
        for module in ('email_utils.py', 'crossref_error_report.py',
                       'email_automator.py'):
            with self.subTest(module=module):
                with open(module, encoding='utf-8') as source_file:
                    source = source_file.read()
                offending = [
                    line.strip() for line in source.splitlines()
                    if '.close()' in line
                    and not line.lstrip().startswith('#')
                ]
                self.assertEqual(
                    offending, [],
                    f"{module} calls CLOSE, which permanently expunges "
                    f"\\Deleted messages; use UNSELECT instead")


if __name__ == '__main__':
    unittest.main()
