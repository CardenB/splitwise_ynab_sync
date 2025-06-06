import unittest
import os
from unittest.mock import patch, mock_open
from utils import (
    setup_environment_vars,
    combine_names,
    extract_swid_from_memo,
    check_if_needs_to_update,
    generate_truncated_hash_for_updated_time,
    construct_memo_swid_tag
)

class TestUtils(unittest.TestCase):
    def test_combine_names_empty_list(self):
        """Test combine_names with empty list"""
        self.assertEqual(combine_names([]), "")

    def test_combine_names_single_name(self):
        """Test combine_names with a single name"""
        self.assertEqual(combine_names(["Alice"]), "Alice")

    def test_combine_names_two_names(self):
        """Test combine_names with two names"""
        self.assertEqual(combine_names(["Alice", "Bob"]), "Alice and Bob")

    def test_combine_names_multiple_names(self):
        """Test combine_names with multiple names"""
        self.assertEqual(combine_names(["Alice", "Bob", "Charlie"]), "Alice, Bob and Charlie")

    def test_extract_swid_from_memo_valid(self):
        """Test extract_swid_from_memo with valid memo"""
        memo = "Dinner [SWID:12345-abc1]"
        full_match, id_num, hash_val = extract_swid_from_memo(memo)
        self.assertEqual(full_match, "[SWID:12345-abc1]")
        self.assertEqual(id_num, 12345)
        self.assertEqual(hash_val, "abc1")

    def test_extract_swid_from_memo_invalid(self):
        """Test extract_swid_from_memo with invalid memo"""
        memo = "Dinner without SWID"
        full_match, id_num, hash_val = extract_swid_from_memo(memo)
        self.assertIsNone(full_match)
        self.assertIsNone(id_num)
        self.assertIsNone(hash_val)

    def test_generate_truncated_hash_consistent(self):
        """Test generate_truncated_hash_for_updated_time produces consistent results"""
        timestamp = "2025-06-06T12:00:00Z"
        hash1 = generate_truncated_hash_for_updated_time(timestamp)
        hash2 = generate_truncated_hash_for_updated_time(timestamp)
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 4)  # Should be 4 characters long

    def test_construct_memo_swid_tag(self):
        """Test construct_memo_swid_tag creates correct format"""
        expense_id = 12345
        updated_at = "2025-06-06T12:00:00Z"
        tag = construct_memo_swid_tag(expense_id, updated_at)
        # The hash will be consistent for the same timestamp
        hash_val = generate_truncated_hash_for_updated_time(updated_at)
        expected = f"[SWID:{expense_id}-{hash_val}]"
        self.assertEqual(tag, expected)

    @patch('builtins.open', new_callable=mock_open, read_data='SW_CONSUMER_KEY: "test_key"\nSW_CONSUMER_SECRET: "test_secret"')
    @patch.dict('os.environ', {}, clear=True)
    def test_setup_environment_vars_local(self, mock_file):
        """Test setup_environment_vars in local development"""
        setup_environment_vars()
        mock_file.assert_called_once_with('creds.yaml', 'r')

    @patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}, clear=True)
    def test_setup_environment_vars_github_actions(self):
        """Test setup_environment_vars in GitHub Actions"""
        setup_environment_vars()
        # Should return early without reading file
        self.assertTrue('GITHUB_ACTIONS' in os.environ)

    def test_check_if_needs_update_different_swids(self):
        """Test check_if_needs_to_update with different SWIDs"""
        sw_expense = {
            'id': '12345',
            'swid': '[SWID:12345-abc1]',
            'updated_time': '2025-06-06T12:00:00Z'
        }
        ynab_transaction = {
            'id': '67890',
            'memo': 'Test [SWID:67890-def2]'
        }
        self.assertFalse(check_if_needs_to_update(sw_expense, ynab_transaction))

    def test_check_if_needs_update_same_swid_different_hash(self):
        """Test check_if_needs_to_update with same SWID but different hash"""
        sw_expense = {
            'id': '12345',
            'swid': '[SWID:12345-abc1]',
            'updated_time': '2025-06-06T12:00:00Z'
        }
        ynab_transaction = {
            'id': '12345',
            'memo': 'Test [SWID:12345-def2]'
        }
        self.assertTrue(check_if_needs_to_update(sw_expense, ynab_transaction))

    def test_check_if_needs_update_missing_swid(self):
        """Test check_if_needs_to_update with missing SWID"""
        sw_expense = {
            'id': '12345',
            'updated_time': '2025-06-06T12:00:00Z'
        }
        ynab_transaction = {
            'id': '12345',
            'memo': 'Test transaction'
        }
        self.assertFalse(check_if_needs_to_update(sw_expense, ynab_transaction))

if __name__ == '__main__':
    unittest.main()
