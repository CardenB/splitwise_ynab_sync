from unittest.mock import MagicMock

class MockExpense:
    """Mock class to simulate Splitwise expense objects"""
    def __init__(self, date, description, swid, cost, owed, deleted_time, current_user_paid, group_name):
        self._data = {
            'date': date,
            'description': description,
            'swid': swid,
            'cost': cost,
            'owed': owed,
            'deleted_time': deleted_time,
            'current_user_paid': current_user_paid,
            'group_name': group_name,
            'id': swid.split(":")[1] if swid else None,
            'creation_method': 'equal'
        }
        self.users = []  # Will be set in test

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def getDate(self):
        return self._data['date']
        
    def getDescription(self):
        return self._data['description']
        
    def getId(self):
        return self._data['id']
        
    def getCost(self):
        return str(self._data['cost'])
        
    def getGroup(self):
        return MagicMock(getName=lambda: self._data['group_name']) if self._data['group_name'] else None
        
    def getUserPaidShare(self):
        return self._data['owed']
        
    def getRepayment(self):
        return self._data['owed']
        
    def getPayment(self):
        return False
        
    def getUsers(self):
        return self.users

class MockUser:
    """Mock class to simulate Splitwise user objects"""
    def __init__(self, id="test_user_id", first_name="Test", last_name="User"):
        self._id = id
        self._first_name = first_name
        self._last_name = last_name

    def getId(self):
        return self._id

    def getFirstName(self):
        return self._first_name

    def getLastName(self):
        return self._last_name
