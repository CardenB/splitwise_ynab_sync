from splitwise import Splitwise
from splitwise.expense import Expense
from splitwise.user import ExpenseUser
import os
import logging
from datetime import datetime
from utils import construct_memo_swid_tag, setup_environment_vars

# https://github.com/namaggarwal/splitwise

def get_user_first_and_last_name_as_id(user) -> str:
    """
    Only for use when the user has no first or last name.
    Return the user name as a string with the user ID.
    """
    user_first_name = user.getFirstName() if user.getFirstName() else ''
    user_last_name = user.getLastName() if user.getLastName() else ''
    assert not user_first_name and not user_last_name
    assert user.getId() is not None, "User ID should not be None"
    return f"User #{str(user.getId())}"

def get_user_first_and_last_name(user) -> str:
    """
    Get the first and last name of a Splitwise user.
    If the user has no first or last name, return the user ID.
    """
    user_first_name = user.getFirstName() if user.getFirstName() else ''
    user_last_name = user.getLastName() if user.getLastName() else ''
    if not user_first_name and not user_last_name:
        return get_user_first_and_last_name_as_id(user)
    elif not user_last_name:
        return user_first_name
    return " ".join([user_first_name, user_last_name])

def get_user_first_and_last_name_with_id(user) -> str:
    """
    Get the first and last name of a Splitwise user with their ID.
    """
    assert user.getId() is not None, "User ID should not be None"
    if not user.getFirstName():
        return get_user_first_and_last_name_as_id(user)
    user_first_and_last_name = get_user_first_and_last_name(user)
    return f"{user_first_and_last_name} - {user.getId()}"


class SW():
    def __init__(self, consumer_key, consumer_secret, api_key) -> None:
        # Initialize the Splitwise object with the API key
        self.sw = Splitwise(consumer_key, consumer_secret, api_key=api_key)

        self.limit = 50
        self.current_user = get_user_first_and_last_name_with_id(self.sw.getCurrentUser())
        self.current_user_id = self.sw.getCurrentUser().getId()
        self.logger = logging.getLogger(__name__)

    def get_friends(self):
        friends_fullnames = []
        friends_ids = []
        for friend in self.sw.getFriends():
            id = friend.getId()
            full_name = get_user_first_and_last_name(friend)
            friends_fullnames.append(full_name)
            friends_ids.append(id)
        return friends_fullnames, friends_ids

    def _expense_involves_current_user(self, expense) -> bool:
        """
        Check if the expense involves the current user.
        Args:
            expense (Expense): The Splitwise Expense object.
        Returns:
            bool: True if the current user is involved in the expense, False otherwise.
        """
        users = expense.getUsers()
        for user in users:
            user_first_last_name_id = get_user_first_and_last_name_with_id(user)
            if user_first_last_name_id == self.current_user:
                return True
        return False

    def _is_debt_consolidation_expense(self, expense) -> bool:
        """
        Process an expense and determine if it is classified as debt consolidation.

        Debt consolidation expenses may warrant special handling because they require consistency
        with additional expenses.

        At one time we preferred to skip these expenses. This was due to the fact that we also
        skipped payment expenses.

        Now we prefer to handle both payment and debt consolidation expenses, and this results in
        a balanced ledger.

        Debt consolidation expenses have a debt_consolidation creation_method and they do not have a group name.
        """
        return expense.creation_method == 'debt_consolidation' and not self._expense_group_name(expense)

    def _current_user_paid(self, expense) -> bool:
        """
        Check if the current user paid for the expense.
        Args:
            expense (Expense): The Splitwise Expense object.
        Returns:
            bool: True if the current user paid, False otherwise.
        """
        users = expense.getUsers()

        # TODO(carden): How do I handle if there are no users in the expense?
        for user in users:
            user_first_last_name_id = get_user_first_and_last_name_with_id(user)
            if (float(user.getPaidShare()) == float(expense.getCost())
                and user_first_last_name_id == self.current_user):
                return True
        return False

    def _expense_group_name(self, expense) -> str:
        group_id = expense.getGroupId()
        if group_id is not None and int(group_id) > 0:
            return self.sw.getGroup(id=group_id).getName()
        return ''

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
                # Make sure the expenses we process involve the current user in some way.
                # This will hold true for payments, debt consolidation, and regular expenses.
                if not self._expense_involves_current_user(expense):
                    self.logger.info(f"Skipping expense as it does not involve the current user.")
                    continue

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
                    # TODO(carden): Make sure we only log payments that involve the current user.

                # Convert Splitwise Expense object to dict for consistent handling
                expense_dict = {
                    'id': expense.getId(),
                    'description': expense.getDescription(),
                    'cost': float(expense.getCost()),
                    'date': expense.getDate(),
                    'deleted_time': expense.getDeletedAt(),
                    'updated_time': expense.getUpdatedAt(),
                    'group_name': self._expense_group_name(expense),  # This will be None for non-group expenses
                }

                # Add SWID tag to expense
                expense_dict['swid'] = construct_memo_swid_tag(expense_dict['id'], expense_dict['updated_time'])

                if expense_dict['date'] is None:
                    self.logger.warning(
                        f"Expense missing date field: ID={expense_dict['id']}, "
                        f"description={expense_dict['description']}, "
                        f"cost={expense_dict['cost']}, "
                        f"updated_time={expense_dict['updated_time']}"
                    )
                    # Should I skip here or use updated time instead?
                    continue

                # Calculate what current user paid and is owed
                users = expense.getUsers()
                expense_dict['users'] = []
                expense_dict['owed'] = 0

                expense_dict['current_user_paid'] = self._current_user_paid(expense)

                # Determine debt consolidation expense and handle it.
                # When splitwise logs a debt consolidation expense, it logs the sum of debt consolidation as one, but
                # then also logs individual debt consolidation for each group. We handle this by only keeping group wise
                # debt consolidation expenses.
                if self._is_debt_consolidation_expense(expense):
                    self.logger.info(f"Found debt consolidation expense: {expense.getDate()}: {expense.getDescription()} and deferring to other debt consolidation expenses within individual budgets.")
                    # Process debt consolidation expenses normally as they are needed to interact with payment expenses.
                    # Previously, we skipped these, but it was only necessary because we also skipped payment expenses.
                    # continue
                    # TODO(carden): Make sure we only involve debt consolidation expenses with the current user.
                    pass
                what_other_users_paid = 0.0
                for user in users:
                    user_first_last_name_id = get_user_first_and_last_name_with_id(user)

                    # TODO(carden): Can I just do "if user.getId() == self.current_user_id"?
                    if user_first_last_name_id == self.current_user:
                        # When a user split expenses with others, the user paid the full amount and they "owe" the amount
                        # they actually were supposed to pay.
                        paid = float(user.getOwedShare())
                        # In the event that the transaction is a "payment" made by the user, owed will be a positive value, since you
                        # are settling the splitwise balance and it must be inverted. This is like the user is being
                        # paid, however, no one is actually paying. It's just a transaction to represent the transfer.
                        # NOTE: if someone else made the payment, this case is completely inverted.
                        # Otherwise, in the typical case, "owed" is likely zero, since the user paid.
                        # If the user is getting paid by someone else in the transaction, owed will again be positive.
                        expense_dict['owed'] = expense_dict['cost'] - paid
                        expense_dict['date'] = expense.getDate()
                        expense_dict['created_time'] = expense.getCreatedAt()
                        expense_dict['updated_time'] = expense.getUpdatedAt()
                        expense_dict['deleted_time'] = expense.getDeletedAt()
                    else:       # get user names other than current_user
                        # If the user paid the expense cost, then they are owed.
                        # Track what other users owe the current user in the "users" value.
                        expense_dict['users'].append(get_user_first_and_last_name(user))
                        what_other_users_paid += float(user.getOwedShare())
                expense_dict["what_other_users_paid"] = what_other_users_paid
                yield expense_dict

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
