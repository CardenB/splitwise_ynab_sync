import unittest
from unittest.mock import MagicMock, patch
from sw import (
    get_user_first_and_last_name_as_id,
    get_user_first_and_last_name,
    get_user_first_and_last_name_with_id,
    SW
)

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
