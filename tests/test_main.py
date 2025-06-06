import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from main import ynab_splitwise_transfer
from tests.mocks import MockExpense, MockUser

class TestYnabSplitwiseTransfer(unittest.TestCase):
    @patch('main.SW')
    @patch('main.YNABClient')
    def setUp(self, mock_ynab_client, mock_sw_class):
        # Set up mock instances
        self.mock_sw_instance = mock_sw_class.return_value
        self.mock_ynab_instance = mock_ynab_client.return_value
        
        # Set up mock return values for required methods
        self.mock_ynab_instance.get_budget_id.return_value = "test_budget_id"
        self.mock_ynab_instance.get_account_id.return_value = "test_account_id"
        
        # Mock the getCurrentUser method to return our mock user
        mock_user = MockUser()
        self.mock_sw_instance.getCurrentUser.return_value = mock_user
        self.mock_sw_instance.current_user = f"{mock_user.getFirstName()} {mock_user.getLastName()} - {mock_user.getId()}"
        self.mock_sw_instance.current_user_id = mock_user.getId()
        
        # Create transfer object with dummy credentials since the actual clients are mocked
        self.transfer = ynab_splitwise_transfer(
            sw_consumer_key="dummy",
            sw_consumer_secret="dummy",
            sw_api_key="dummy",
            ynab_personal_access_token="dummy",
            ynab_budget_name="test_budget",
            ynab_account_name="test_account",
            use_update_date=True
        )

    def test_duplicate_expense_handling(self):
        """Test that when multiple expenses with the same swid are received, only the latest one is processed"""
        # Create sample expenses with the same swid but different dates
        older_expense = MockExpense(
            date='2025-06-01T10:00:00Z',
            description='Test Expense',
            swid='splitwise Test Expense SW:12345:ABCDE',  # Same swid, full memo format
            cost=100.0,
            owed=50.0,
            deleted_time=None,
            current_user_paid=True,
            group_name='Test Group'
        )
        
        newer_expense = MockExpense(
            date='2025-06-02T10:00:00Z',
            description='Test Expense Updated',
            swid='splitwise Test Expense Updated SW:12345:ABCDE',  # Same swid, full memo format
            cost=120.0,
            owed=60.0,
            deleted_time=None,
            current_user_paid=True,
            group_name='Test Group'
        )

        # Mock the get_expenses method to return our test expenses
        self.mock_sw_instance.get_expenses.return_value = [older_expense, newer_expense]
        
        # Mock the expense checks
        for expense in [older_expense, newer_expense]:
            expense.users = [MockUser()]  # Add current user as participant
            expense.getUserPaidShare = lambda: expense['owed']
            expense.getRepayment = lambda: expense['owed']
            expense.getPayment = lambda: False
        
        # Set up YNAB transaction mapping mock
        self.transfer.ynab_swid_to_transaction_mapping = MagicMock(return_value={})
        
        # Mock YNAB API calls
        self.mock_ynab_instance.create_transaction = MagicMock()
        self.mock_ynab_instance.create_import_id = MagicMock(return_value="test_import_id")

        # Run the sync
        self.transfer.sw_to_ynab()

        # Get the transactions that were sent to YNAB
        create_transaction_calls = self.mock_ynab_instance.create_transaction.call_args_list
        
        # Verify only one transaction was created
        self.assertEqual(len(create_transaction_calls), 1, "Only one transaction should be created")
        
        # Verify it was the newer transaction that was processed
        created_transactions = create_transaction_calls[0][0][1]  # Get the transactions argument
        self.assertEqual(len(created_transactions), 1, "Only one transaction should be in the batch")

        # Verify the transaction details match the newer expense
        created_transaction = created_transactions[0]
        self.assertEqual(created_transaction['date'], newer_expense['date'])
        self.assertEqual(created_transaction['amount'], int(newer_expense['owed'] * 1000))
        self.assertIn(newer_expense['description'], created_transaction['memo'])
        self.assertIn(newer_expense['swid'], created_transaction['memo'])

    def test_different_expenses_processed_independently(self):
        """Test that two different expenses are processed independently"""
        expense1 = MockExpense(
            date='2025-06-01T10:00:00Z',
            description='Test Expense 1',
            swid='splitwise Test Expense 1 SW:12345:ABCDE',  # Full memo format
            cost=100.0,
            owed=50.0,
            deleted_time=None,
            current_user_paid=True,
            group_name='Test Group'
        )
        
        expense2 = MockExpense(
            date='2025-06-01T10:00:00Z',
            description='Test Expense 2',
            swid='splitwise Test Expense 2 SW:12345:FGHIJ',  # Full memo format
            cost=120.0,
            owed=60.0,
            deleted_time=None,
            current_user_paid=True,
            group_name='Test Group'
        )

        # Mock the get_expenses method
        self.mock_sw_instance.get_expenses.return_value = [expense1, expense2]
        
        # Mock the expense checks
        for expense in [expense1, expense2]:
            expense.users = [MockUser()]  # Add current user as participant
            expense.getUserPaidShare = lambda: expense['owed']
            expense.getRepayment = lambda: expense['owed']
            expense.getPayment = lambda: False
        
        # Mock the ynab_swid_to_transaction_mapping
        self.transfer.ynab_swid_to_transaction_mapping = MagicMock(return_value={})
        
        # Mock YNAB API calls
        self.mock_ynab_instance.create_transaction = MagicMock()
        self.mock_ynab_instance.create_import_id = MagicMock(return_value="test_import_id")

        # Run the sync
        self.transfer.sw_to_ynab()

        # Verify both transactions were created
        create_transaction_calls = self.mock_ynab_instance.create_transaction.call_args_list
        self.assertEqual(len(create_transaction_calls), 1, "One batch of transactions should be created")
        
        created_transactions = create_transaction_calls[0][0][1]
        self.assertEqual(len(created_transactions), 2, "Both transactions should be in the batch")

        # Verify both transactions have different swids
        swids = set()
        for transaction in created_transactions:
            swid = transaction['memo'].split()[-1]  # Get the swid from the memo
            swids.add(swid)
        self.assertEqual(len(swids), 2, "Should have two different swids")

if __name__ == '__main__':
    unittest.main()
