from splitwise import Splitwise
from splitwise.expense import Expense
from splitwise.user import ExpenseUser
import os
import logging
from utils import construct_memo_swid_tag, setup_environment_vars

# https://github.com/namaggarwal/splitwise

class SW():
    def __init__(self, consumer_key, consumer_secret, api_key) -> None:
        # Initialize the Splitwise object with the API key
        self.sw = Splitwise(consumer_key, consumer_secret, api_key=api_key)

        self.limit = 50
        self.current_user = f"{self.sw.getCurrentUser().getFirstName()} {self.sw.getCurrentUser().getLastName()} - {self.sw.getCurrentUser().getId()}"
        self.current_user_id = self.sw.getCurrentUser().getId()
        self.logger = logging.getLogger(__name__)

    def get_friends(self):
        friends_fullnames = []
        friends_ids = []
        for friend in self.sw.getFriends():
            id = friend.getId()
            full_name = friend.getFirstName()
            if friend.getLastName() is not None:
                full_name = " ".join([full_name, friend.getLastName()])
            friends_fullnames.append(full_name)
            friends_ids.append(id)
        return friends_fullnames, friends_ids

    def get_expenses(self, dated_before=None, dated_after=None, use_update: bool=False):
        def _fetch_expenses(offset: int=0):
            # get all expenses between 2 dates
            if use_update:
                self.logger.info("Using updated_at instead of expense date for finding expenses.")
                expenses = self.sw.getExpenses(limit=self.limit, offset=offset, updated_before=dated_before, updated_after=dated_after)
            else:
                expenses = self.sw.getExpenses(limit=self.limit, offset=offset, dated_before=dated_before, dated_after=dated_after)
            return expenses
        cur_offset = 0
        cur_expenses = _fetch_expenses()
        while cur_expenses:
            for expense in cur_expenses:
                # How do I treat "payments"?
                if expense.payment:
                    continue
                # Skip logging debt consolidation expenses.
                # Because we have all the transactions leading up to here, we don't need another transaction
                # to duplicate outflow/inflow.
                if expense.creation_method == 'debt_consolidation':
                    self.logger.info(f"Skipping debt consolidation expense: {expense.getDate()}: {expense.getDescription()}")
                    continue
                expense_date = datetime.strptime(expense.getDate(), "%Y-%m-%dT%H:%M:%SZ")

                if expense_date > datetime.now():
                    self.logger.info(f"Skipping future expense: {expense.getDate()}: {expense.getDescription()}")
                    continue
                owed_expense = {}
                users = expense.getUsers()
                user_names = []
                expense_cost = float(expense.getCost())
                what_other_users_paid = 0.0
                owed_expense['cost'] = expense_cost
                owed_expense['description'] = expense.getDescription()
                owed_expense['swid'] = construct_memo_swid_tag(expense.getId(), expense.getUpdatedAt())

                # Determine if the current use paid first.
                current_user_paid = False
                for user in users:
                    user_first_last_name = f"{user.getFirstName()} {user.getLastName()} - {user.getId()}"
                    if float(user.getPaidShare()) == expense_cost:
                        current_user_paid = user_first_last_name == self.current_user
                owed_expense['current_user_paid'] = current_user_paid
                group_name = ''
                if expense.getGroupId() is not None and int(expense.getGroupId()) > 0:
                    group = self.sw.getGroup(id=expense.getGroupId())
                    group_name = group.getName()
                owed_expense['group_name'] = group_name
                for user in users:
                    user_first_last_name = f"{user.getFirstName()} {user.getLastName()} - {user.getId()}"

                    if user_first_last_name == self.current_user:
                        # When a user split expenses with others, the user paid the full amount and they "owe" the amount
                        # they actually were supposed to pay.
                        paid = float(user.getOwedShare())
                        owed_expense['owed'] = expense_cost - paid
                        owed_expense['date'] = expense.getDate()
                        owed_expense['created_time'] = expense.getCreatedAt()
                        owed_expense['updated_time'] = expense.getUpdatedAt()
                        owed_expense['deleted_time'] = expense.getDeletedAt()
                    else:       # get user names other than current_user
                        # If the user paid the expense cost, then they are owed.
                        user_names.append(f"{user.getFirstName()} {user.getLastName()}")
                        what_other_users_paid += float(user.getOwedShare())
                # Make a string list for passing around jsons.
                owed_expense['users'] = user_names
                yield owed_expense
            cur_offset += self.limit
            cur_expenses = _fetch_expenses(offset=cur_offset)
        return None
    
    def create_expense(self, expense):
        e = Expense()
        e.setCost(expense['cost'])
        e.setDate(expense['date'])
        e.setDescription(expense['description'])

        users = []
        for user in expense['users']:
            u = ExpenseUser()
            u.setId(user['id'])
            u.setPaidShare(user['paid'])
            u.setOwedShare(user['owed'])

            users.append(u)

        e.setUsers(users)
        expense, errors = self.sw.createExpense(e)
        if errors:
            print(errors.getErrors())
        return expense, errors


if __name__ == "__main__":
    # load environment variables from yaml file (locally)
    setup_environment_vars()
    
    # splitwise creds
    consumer_key = os.environ.get('sw_consumer_key')
    consumer_secret = os.environ.get('sw_consumer_secret')
    api_key = os.environ.get('sw_api_key')

    a = SW(consumer_key, consumer_secret, api_key)
    # e = a.get_expenses(dated_after="2023-11-29", dated_before="2023-12-01")
    
    a.create_expense()
    # a.get_friends()
