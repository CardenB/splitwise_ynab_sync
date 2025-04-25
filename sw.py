from splitwise import Splitwise
from splitwise.expense import Expense
from splitwise.user import ExpenseUser
import os
import logging
from datetime import datetime
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
            kwargs = dict(
                    limit=self.limit,
                    offset=offset,
            )
            if use_update:
                self.logger.info("Using updated_at instead of expense date for finding expenses.")
                kwargs['updated_after'] = dated_after
                if dated_before is not None:
                    kwargs['updated_before'] = dated_before
            else:
                kwargs['dated_after'] = dated_after
                if dated_before is not None:
                    kwargs['dated_before'] = dated_before
            return self.sw.getExpenses(**kwargs)
        cur_offset = 0
        cur_expenses = _fetch_expenses()
        while cur_expenses:
            for expense in cur_expenses:
                # How do I treat "payments"?
                # If expense is a payment, then it is used to settle a balance.
                # Ex: User pays with Venmo and not splitwise pay.
                # Cost for these payments is POSITIVE. The reason is that the payment shifts
                # the splitwise balance to the payment method. Think of it like a transfer.
                # Thus, we do not need special handling of the payment.
                # NOTE: perhaps some exception is here when settle up is used. May need to debug this in the future.
                if expense.payment:
                    # Useful to debug locally. Uncomment in that circumstance.
                    # Commented out so expense details don't show in prod logs.
                    # self.logger.info(f"Found payment: {expense.getDate()}: {expense.getCost()}, {expense.getDescription()}")
                    # continue
                    self.logger.info("Found payment expense, processing normally.")

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
                # When splitwise logs a debt consolidation expense, it logs the sum of debt consolidation as one, but
                # then also logs individual debt consolidation for each group. We handle this by only keeping group wise
                # debt consolidation expenses.
                if expense.creation_method == 'debt_consolidation' and not group_name:
                    self.logger.info(f"Skipping debt consolidation expense: {expense.getDate()}: {expense.getDescription()} and deferring to other debt consolidation expenses within individual budgets.")
                    continue
                owed_expense['group_name'] = group_name
                for user in users:
                    user_first_last_name = f"{user.getFirstName()} {user.getLastName()} - {user.getId()}"

                    if user_first_last_name == self.current_user:
                        # When a user split expenses with others, the user paid the full amount and they "owe" the amount
                        # they actually were supposed to pay.
                        paid = float(user.getOwedShare())
                        # In the event that the transaction is a "payment" made by the user, owed will be a positive value, since you
                        # are settling the splitwise balance and it must be inverted. This is like the user is being
                        # paid, however, no one is actually paying. It's just a transaction to represent the transfer.
                        # NOTE: if someone else made the payment, this case is completely inverted.
                        # Otherwise, in the typical case, "owed" is likely zero, since the user paid.
                        # If the user is getting paid by someone else in the transaction, owed will again be positive.
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
