import unittest
from unittest.mock import MagicMock, patch
from sw import (
    get_user_first_and_last_name_as_id,
    get_user_first_and_last_name,
    get_user_first_and_last_name_with_id,
    SW,
    SPLITWISE_ATTEMPTS,
    SPLITWISE_BACKOFF_SECONDS,
    SPLITWISE_MAX_RETRY_AFTER_SECONDS,
)
import requests
from splitwise.exception import SplitwiseException, SplitwiseUnauthorizedException


def _sw_error(status, cls=SplitwiseException, headers=None):
    """Build the exception the splitwise SDK raises for an HTTP status."""
    response = MagicMock(status_code=status, content=b"", headers=headers or {})
    return cls("Unknown error happened", response)

class MockUser:
    def __init__(self, id=None, first_name=None, last_name=None):
        self._id = id
        self._first_name = first_name
        self._last_name = last_name
    
    def getId(self):
        return self._id
    
    def getFirstName(self):
        return self._first_name
    
    def getLastName(self):
        return self._last_name

class MockExpense:
    def __init__(self, id=None, description=None, cost=None, date=None, users=None, payment=False, creation_method=None, group_id=None):
        self._id = id
        self._description = description
        self._cost = str(cost) if cost is not None else None
        self._date = date
        self._users = users or []
        self.payment = payment
        self.creation_method = creation_method
        self._group_id = group_id
    
    def getId(self):
        return self._id
    
    def getDescription(self):
        return self._description
    
    def getCost(self):
        return self._cost
    
    def getDate(self):
        return self._date
    
    def getUsers(self):
        return self._users
    
    def getGroupId(self):
        return self._group_id

class TestSplitwise(unittest.TestCase):
    def test_get_user_first_and_last_name_as_id(self):
        """Test get_user_first_and_last_name_as_id with a user that has no names"""
        user = MockUser(id="123", first_name=None, last_name=None)
        result = get_user_first_and_last_name_as_id(user)
        self.assertEqual(result, "User #123")
    
    def test_get_user_first_and_last_name_as_id_no_id(self):
        """Test get_user_first_and_last_name_as_id with a user that has no ID"""
        user = MockUser(id=None, first_name=None, last_name=None)
        with self.assertRaises(AssertionError):
            get_user_first_and_last_name_as_id(user)

    def test_get_user_first_and_last_name(self):
        """Test get_user_first_and_last_name with various name combinations"""
        test_cases = [
            (MockUser(id="123", first_name="John", last_name="Doe"), "John Doe"),
            (MockUser(id="123", first_name="John", last_name=None), "John"),
            (MockUser(id="123", first_name=None, last_name=None), "User #123"),
        ]
        for user, expected in test_cases:
            with self.subTest(user=user):
                result = get_user_first_and_last_name(user)
                self.assertEqual(result, expected)

    def test_get_user_first_and_last_name_with_id(self):
        """Test get_user_first_and_last_name_with_id with various combinations"""
        test_cases = [
            (MockUser(id="123", first_name="John", last_name="Doe"), "John Doe - 123"),
            (MockUser(id="123", first_name="John", last_name=None), "John - 123"),
            (MockUser(id="123", first_name=None, last_name=None), "User #123"),
        ]
        for user, expected in test_cases:
            with self.subTest(user=user):
                result = get_user_first_and_last_name_with_id(user)
                self.assertEqual(result, expected)

class TestSW(unittest.TestCase):
    def setUp(self):
        self.mock_splitwise = MagicMock()
        self.mock_current_user = MockUser(id="123", first_name="Current", last_name="User")
        self.mock_splitwise.getCurrentUser.return_value = self.mock_current_user
        
        with patch('sw.Splitwise', return_value=self.mock_splitwise):
            self.sw = SW("key", "secret", "api_key")
        # Never really sleep through retry backoff in tests.
        self.sw._sleep = MagicMock()

    def test_expense_involves_current_user(self):
        """Test _expense_involves_current_user method"""
        # Create a mock expense with current user
        mock_user = MockUser(id="123", first_name="Current", last_name="User")
        mock_expense = MockExpense(users=[mock_user])
        
        # Test when current user is involved
        self.assertTrue(self.sw._expense_involves_current_user(mock_expense))
        
        # Test when current user is not involved
        other_user = MockUser(id="456", first_name="Other", last_name="User")
        mock_expense = MockExpense(users=[other_user])
        self.assertFalse(self.sw._expense_involves_current_user(mock_expense))

    def test_is_debt_consolidation_expense(self):
        """Test _is_debt_consolidation_expense method"""
        # Test debt consolidation expense
        mock_expense = MockExpense(creation_method="debt_consolidation", group_id=None)
        self.assertTrue(self.sw._is_debt_consolidation_expense(mock_expense))
        
        # Test non-debt consolidation expense
        mock_expense = MockExpense(creation_method="default", group_id="123")
        self.assertFalse(self.sw._is_debt_consolidation_expense(mock_expense))

    def test_current_user_paid(self):
        """Test _current_user_paid method"""
        # Create a mock expense where current user paid
        mock_user = MockUser(id="123", first_name="Current", last_name="User")
        mock_user_paid = MagicMock()
        mock_user_paid.getPaidShare.return_value = "100.0"
        mock_user_paid.getId = mock_user.getId
        mock_user_paid.getFirstName = mock_user.getFirstName
        mock_user_paid.getLastName = mock_user.getLastName
        
        mock_expense = MockExpense(cost="100.0", users=[mock_user_paid])
        self.assertTrue(self.sw._current_user_paid(mock_expense))
        
        # Test when current user didn't pay
        mock_user_paid.getPaidShare.return_value = "0.0"
        self.assertFalse(self.sw._current_user_paid(mock_expense))

    def test_expense_group_name(self):
        """Test _expense_group_name method"""
        # Mock group name retrieval
        mock_group = MagicMock()
        mock_group.getName.return_value = "Test Group"
        self.mock_splitwise.getGroup.return_value = mock_group
        
        # Test expense with group
        mock_expense = MockExpense(group_id="123")
        self.assertEqual(self.sw._expense_group_name(mock_expense), "Test Group")
        
        # Test expense without group
        mock_expense = MockExpense(group_id=None)
        self.assertEqual(self.sw._expense_group_name(mock_expense), "")

    def test_expense_group_name_is_cached(self):
        """_expense_group_name resolves each group_id once, then serves from cache."""
        mock_group = MagicMock()
        mock_group.getName.return_value = "Test Group"
        self.mock_splitwise.getGroup.return_value = mock_group

        # Two expenses in the same group -> one round-trip, not two.
        self.assertEqual(self.sw._expense_group_name(MockExpense(group_id="123")), "Test Group")
        self.assertEqual(self.sw._expense_group_name(MockExpense(group_id="123")), "Test Group")
        self.mock_splitwise.getGroup.assert_called_once_with(id=123)

    def test_init_fetches_current_user_once(self):
        """SW() resolves the current user with a single getCurrentUser call."""
        self.assertEqual(self.mock_splitwise.getCurrentUser.call_count, 1)
        self.assertEqual(self.sw.current_user_id, "123")

    def test_fetch_group_name_retries_on_timeout(self):
        """A transient timeout is retried after a backoff; the success is returned."""
        mock_group = MagicMock()
        mock_group.getName.return_value = "Test Group"
        self.mock_splitwise.getGroup.side_effect = [requests.Timeout("boom"), mock_group]

        self.assertEqual(self.sw._expense_group_name(MockExpense(group_id="123")), "Test Group")
        self.assertEqual(self.mock_splitwise.getGroup.call_count, 2)
        self.sw._sleep.assert_called_once_with(SPLITWISE_BACKOFF_SECONDS[0])

    def test_fetch_group_name_reraises_after_attempts_exhausted(self):
        """Consecutive timeouts exhaust the attempts and re-raise."""
        self.mock_splitwise.getGroup.side_effect = requests.Timeout("boom")

        with self.assertRaises(requests.Timeout):
            self.sw._expense_group_name(MockExpense(group_id="123"))
        self.assertEqual(self.mock_splitwise.getGroup.call_count, SPLITWISE_ATTEMPTS)
        self.assertEqual(self.sw._sleep.call_count, SPLITWISE_ATTEMPTS - 1)

    def test_get_expenses_retries_on_5xx(self):
        """An 'Unknown error' (gateway 5xx) from getExpenses is retried, not fatal."""
        self.mock_splitwise.getExpenses.side_effect = [_sw_error(504), []]

        self.assertEqual(list(self.sw.get_expenses(dated_after="2026-01-01")), [])
        self.assertEqual(self.mock_splitwise.getExpenses.call_count, 2)
        self.sw._sleep.assert_called_once_with(SPLITWISE_BACKOFF_SECONDS[0])

    def test_get_expenses_does_not_retry_auth_errors(self):
        """4xx other than 429 is a real error: no retry, no sleep."""
        self.mock_splitwise.getExpenses.side_effect = _sw_error(
            401, cls=SplitwiseUnauthorizedException)

        with self.assertRaises(SplitwiseUnauthorizedException):
            list(self.sw.get_expenses(dated_after="2026-01-01"))
        self.assertEqual(self.mock_splitwise.getExpenses.call_count, 1)
        self.sw._sleep.assert_not_called()

    def test_429_honours_retry_after(self):
        """429 waits for the server's Retry-After before the single retry."""
        self.mock_splitwise.getExpenses.side_effect = [
            _sw_error(429, headers={"Retry-After": "7"}), []]

        list(self.sw.get_expenses(dated_after="2026-01-01"))
        self.sw._sleep.assert_called_once_with(7.0)

    def test_429_with_long_retry_after_gives_up(self):
        """A Retry-After past the cap means back off entirely, not hammer the API."""
        self.mock_splitwise.getExpenses.side_effect = _sw_error(
            429, headers={"Retry-After": str(SPLITWISE_MAX_RETRY_AFTER_SECONDS + 1)})

        with self.assertRaises(SplitwiseException):
            list(self.sw.get_expenses(dated_after="2026-01-01"))
        self.assertEqual(self.mock_splitwise.getExpenses.call_count, 1)
        self.sw._sleep.assert_not_called()

    def test_retry_budget_caps_retries_per_run(self):
        """Once the per-run budget is spent, further failures raise immediately."""
        self.sw._retry_budget = 1
        self.mock_splitwise.getGroup.side_effect = requests.Timeout("boom")

        with self.assertRaises(requests.Timeout):
            self.sw._expense_group_name(MockExpense(group_id="123"))
        self.assertEqual(self.mock_splitwise.getGroup.call_count, 2)
        self.assertEqual(self.sw._sleep.call_count, 1)
        self.assertEqual(self.sw._retry_budget, 0)

    def test_get_friends(self):
        """Test get_friends method"""
        mock_friends = [
            MockUser(id="456", first_name="Friend1", last_name="One"),
            MockUser(id="789", first_name="Friend2", last_name="Two")
        ]
        self.mock_splitwise.getFriends.return_value = mock_friends
        
        names, ids = self.sw.get_friends()
        self.assertEqual(names, ["Friend1 One", "Friend2 Two"])
        self.assertEqual(ids, ["456", "789"])

if __name__ == '__main__':
    unittest.main()
