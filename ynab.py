# https://github.com/deanmcgregor/ynab-python
import logging
import os
import re
import requests
from utils import setup_environment_vars

class YNABClient:
    BASE_URL = "https://api.youneedabudget.com/v1"

    def __init__(self, access_token):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        self.logger = logging.getLogger(__name__)

    def _make_request(self, method, endpoint, params=None, data=None):
        url = f"{self.BASE_URL}/{endpoint}"
        response = requests.request(method, url, headers=self.headers, params=params, json=data)
        response.raise_for_status()  # Raise an exception for HTTP errors
        return response.json()

    def get_budgets(self):
        return self._make_request("GET", "budgets")
    
    def get_budget_id(self, budget_name):
        budgets = self.get_budgets()
        for budget in budgets['data']['budgets']:
            if budget['name'] == budget_name:
                return budget['id']
        return None

    def create_import_id(self, amount, date, import_hash=None):
        """
        Create an import ID for a transaction for use with YNAB API.

        Args:
            amount (int): The amount of the transaction in milliunits.
                          This is the same value that you would put in the create transaction api.
            date (str): The date of the transaction in "YYYY-MM-DD" format.
            import_hash (Optional[str]): A unique hash for the transaction.
        """
        # return none if date is not a string with "YYYY-MM-DD" format
        # Check date format with regex.
        if not (isinstance(date, str) and re.match(r"\d{4}-\d{2}-\d{2}", date)):
            self.logger.error(f"Invalid date format: {date}. Expected 'YYYY-MM-DD'.")
            return None
        import_id = f"YNAB:{str(amount)}:{date}"
        if import_hash is not None:
            import_id += f":{import_hash}"
        return import_id

    def create_transaction(self, budget_id, transactions):
        endpoint = f"budgets/{budget_id}/transactions"
        data = {"transactions": transactions}
        return self._make_request("POST", endpoint, data=data)

    def create_scheduled_transaction(self, budget_id, transaction):
        endpoint = f"budgets/{budget_id}/scheduled_transactions"
        data = {"scheduled_transaction": transaction}
        return self._make_request("POST", endpoint, data=data)
    
    def get_accounts(self, budget_id):
        return self._make_request("GET", f"budgets/{budget_id}/accounts")
    
    def get_account_id(self, budget_id, account_name):
        accounts = self.get_accounts(budget_id)
        for account in accounts['data']['accounts']:
            if account['name'].strip() == account_name.strip():
                return account['id']
        return None

    def get_categories(self, budget_id):
        return self._make_request("GET", f"budgets/{budget_id}/categories")
    
    def get_category_id(self, budget_id, category_name):
        categories = self.get_categories(budget_id)
        for category_group in categories['data']['category_groups']:
            for category in category_group['categories']:
                if category['name'].strip() == category_name.strip():
                    return category['id']
        return None

    def get_transactions(self, budget_id, account_id=None, since_date=None, before_date=None):
        endpoint = f"budgets/{budget_id}/transactions"
        if account_id:
            endpoint = f"budgets/{budget_id}/accounts/{account_id}/transactions"
        
        params = {}
        if since_date:
            params['since_date'] = since_date
        if before_date:
            params['before_date'] = before_date

        return self._make_request("GET", endpoint, params=params)
    
    def get_scheduled_transactions(self, budget_id):
        endpoint = f"budgets/{budget_id}/scheduled_transactions"
        return self._make_request("GET", endpoint)

    def get_last_transaction(self, budget_id, account_id):
        transactions = self.get_transactions(budget_id, account_id)
        if not transactions['data']['transactions']:
            return None  # No transactions found

        # Sort transactions by date in descending order and pick the first one
        last_transaction = sorted(
            transactions['data']['transactions'], 
            key=lambda x: x['date'], 
            reverse=True
        )[0]
        return last_transaction
    
    def update_transaction(self, budget_id, transaction_id, transaction_data):
        endpoint = f"budgets/{budget_id}/transactions/{transaction_id}"
        return self._make_request("PATCH", endpoint, data=transaction_data)

    def delete_transaction(self, budget_id, transaction_id):
        endpoint = f"budgets/{budget_id}/transactions/{transaction_id}"
        return self._make_request("DELETE", endpoint)

    def update_transactions(self, budget_id, transactions):
        endpoint = f"budgets/{budget_id}/transactions"
        data = {"transactions": transactions}
        return self._make_request("PATCH", endpoint, data=data)


if __name__ == "__main__":
    # load environment variables from yaml file (locally)
    setup_environment_vars()

    # ynab creds
    ynab_budget_name = os.environ.get('ynab_budget_name')
    ynab_account_name = os.environ.get('ynab_account_name')
    personal_access_token = os.environ.get('ynab_personal_access_token')

    client = YNABClient(personal_access_token)

    budget_id = client.get_budget_id(ynab_budget_name)
    account_id = client.get_account_id(budget_id, ynab_account_name)

    # Create a transaction
    transactions = [
        {
            "account_id": account_id,
            "date": "2023-11-22",
            "amount": 5000,  # Example amount in milliunits
            "payee_name": "Grocery Store",
            "memo": "Weekly groceries",
            "cleared": "cleared",
            "approved": False
        }
        ,{
            "account_id": account_id,
            "date": "2023-11-23",
            "amount": 2000,  # Another example amount
            "payee_name": "Cafe",
            "memo": "Coffee break",
            "cleared": "cleared",
            "approved": False
        }
    ]

    t = client.get_last_transaction(budget_id, account_id)
    print(t)
