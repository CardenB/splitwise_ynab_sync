import json
import os
import logging
import secrets
import sys
from datetime import datetime, timedelta, date, timezone
from typing import Optional

from sw import SW
from ynab import YNABClient
from utils import (
    setup_environment_vars,
    check_if_needs_to_update,
    combine_names,
    extract_swid_from_memo,
)


def _add_transaction_to_swid_map(swid_transaction_map: dict, transaction: dict) -> None:
    # check the memo for 'splitwise' keyword
    memo = transaction.get("memo", "")
    if not memo:
        return
    _, sw_id, _ = extract_swid_from_memo(memo)
    if sw_id is not None:
        swid_transaction_map[sw_id] = transaction
    return


class ynab_splitwise_transfer:
    def __init__(
        self,
        sw_consumer_key,
        sw_consumer_secret,
        sw_api_key,
        ynab_personal_access_token,
        ynab_budget_name,
        ynab_account_name,
        use_update_date: bool = False,
    ) -> None:
        self.sw = SW(sw_consumer_key, sw_consumer_secret, sw_api_key)
        self.ynab = YNABClient(ynab_personal_access_token)

        self.ynab_budget_id = self.ynab.get_budget_id(ynab_budget_name)
        self.ynab_account_id = self.ynab.get_account_id(
            self.ynab_budget_id, ynab_account_name
        )

        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
        )
        self.logger = logging.getLogger(__name__)

        # # timestamps
        # now = datetime.now(timezone.utc)
        # Prefer to just take all future transactions
        now = datetime.now(timezone.utc)
        self.end_date = datetime(now.year, now.month, now.day) + timedelta(days=1)
        # self.end_date = None
        self.sw_start_date = datetime(now.year, now.month, now.day) - timedelta(days=90)
        self.ynab_start_date = datetime(now.year, now.month, now.day) - timedelta(
            days=90
        )

        self.use_update_date = use_update_date

    def ynab_swid_to_transaction_mapping(
        self, since_date: Optional[date] = None
    ) -> dict[str, dict]:
        """
        Gets all Splitwise expense IDs from YNAB transactions in the splitwise account.
        Uses expense ID as key in the dict and then maps to value for updates.
        WARNING: Will process ALL transactions in the Splitwise account. Hopefully rate limits will not become an issue.

        This is used to determine if the splitwise transaction is already added so it will not be added again.
        Or it is used to determine if a ynab transaction needs to be updated.

        Returns:
        Set of all Splitwise expense IDs in YNAB transactions.
        """
        ynab_splitwise_transactions_response = self.ynab.get_transactions(
            self.ynab_budget_id, self.ynab_account_id, since_date=since_date
        )
        expense_id_to_transaction_mapping = dict()
        ynab_splitwise_scheduled_transactions_response = (
            self.ynab.get_scheduled_transactions(self.ynab_budget_id)
        )
        all_transactions = ynab_splitwise_transactions_response.get("data", {}).get(
            "transactions", []
        ) + ynab_splitwise_scheduled_transactions_response.get("data", {}).get(
            "scheduled_transactions", []
        )
        for transaction in all_transactions:
            _add_transaction_to_swid_map(expense_id_to_transaction_mapping, transaction)
        return expense_id_to_transaction_mapping

    def get_swids_in_ynab(self, since_date: Optional[date] = None):
        """
        Gets all Splitwise expense IDs from YNAB transactions in the splitwise account.
        WARNING: Will process ALL transactions in the Splitwise account. Hopefully rate limits will not become an issue.

        This is used to determine if the splitwise transaction is already added so it will not be added again.

        Returns:
        Set of all Splitwise expense IDs in YNAB transactions.
        """
        ynab_splitwise_transactions_response = self.ynab.get_transactions(
            self.ynab_budget_id, self.ynab_account_id, since_date=since_date
        )
        splitwise_expense_ids = set()
        ynab_splitwise_scheduled_transactions_response = (
            self.ynab.get_scheduled_transactions(self.ynab_budget_id)
        )
        all_transactions = ynab_splitwise_transactions_response.get("data", {}).get(
            "transactions", []
        ) + ynab_splitwise_scheduled_transactions_response.get("data", {}).get(
            "scheduled_transactions", []
        )
        for transaction in all_transactions:
            # check the memo for 'splitwise' keyword
            memo = transaction.get("memo", "")
            if not memo:
                continue
            sw_id, _, _ = extract_swid_from_memo(memo)
            if sw_id is not None:
                splitwise_expense_ids.add(sw_id)
        return splitwise_expense_ids

    def sw_to_ynab(self):
        self.logger.info("Moving transactions from Splitwise to YNAB...")
        self.logger.info(
            f"Getting all Splitwise expenses from {self.sw_start_date} to {self.end_date}"
        )
        expenses = self.sw.get_expenses(
            dated_after=self.sw_start_date,
            dated_before=self.end_date,
            use_update=self.use_update_date,
        )
        expenses = list(expenses)

        if not expenses:
            self.logger.info("No transactions to write to YNAB.")
            return 0

        # Filter out expenses without dates and log them
        valid_expenses = []
        # Track latest transaction for each swid
        latest_expenses_by_swid = {}

        for expense in expenses:
            if "date" not in expense:
                self.logger.warning(
                    f"Skipping expense without date: {expense.get('description', 'No description')} ID: {expense.get('id', 'No ID')}"
                )
                continue

            # If the expense has a swid, track the latest one
            # This handles the case where there are multiple expenses found, perhaps one expense got updated multiple
            # times before being sync'd, and we would upload duplicates.
            if expense.get("swid"):
                _, expense_swid, _ = extract_swid_from_memo(expense["swid"])
                expense_date = datetime.strptime(expense["date"], "%Y-%m-%dT%H:%M:%SZ")

                if expense_swid in latest_expenses_by_swid:
                    existing_date = datetime.strptime(
                        latest_expenses_by_swid[expense_swid]["date"],
                        "%Y-%m-%dT%H:%M:%SZ",
                    )
                    if expense_date > existing_date:
                        self.logger.info(
                            f"Found newer version of expense {expense_swid}"
                        )
                        latest_expenses_by_swid[expense_swid] = expense
                else:
                    latest_expenses_by_swid[expense_swid] = expense
            else:
                # If no swid, always include it
                valid_expenses.append(expense)

        # Add the latest version of each swid transaction
        valid_expenses.extend(latest_expenses_by_swid.values())

        if not valid_expenses:
            self.logger.info("No valid transactions with dates to write to YNAB.")
            return 0

        earliest_splitwise_date = min(
            [
                datetime.strptime(expense["date"], "%Y-%m-%dT%H:%M:%SZ").date()
                for expense in valid_expenses
            ]
        )
        swid_to_transaction_mapping = self.ynab_swid_to_transaction_mapping(
            since_date=earliest_splitwise_date
        )
        ynab_transactions = []
        scheduled_transactions = []
        updated_transactions = []
        for expense in valid_expenses:
            # don't import deleted expenses
            if expense["deleted_time"]:
                _, expense_swid, _ = extract_swid_from_memo(expense["swid"])
                if expense_swid in swid_to_transaction_mapping:
                    _, transaction_swid, _ = extract_swid_from_memo(
                        swid_to_transaction_mapping[expense_swid]["memo"]
                    )
                    if expense_swid == transaction_swid:
                        self.logger.info(
                            f"Deleting expense {expense['date']} {expense['description']} {expense['swid']} from YNAB as it has been deleted from Splitwise"
                        )
                        self.ynab.delete_transaction(
                            self.ynab_budget_id,
                            swid_to_transaction_mapping[expense_swid]["id"],
                        )
                else:
                    self.logger.info(
                        f"Skipping deleted expense {expense['date']} {expense['description']} {expense['swid']} as it is not found in YNAB"
                    )
                continue
            if expense.get("swid", ""):
                _, expense_swid, _ = extract_swid_from_memo(expense["swid"])
                # Check if the expense is already in YNAB
                if expense_swid in swid_to_transaction_mapping:
                    ynab_transaction = swid_to_transaction_mapping[expense_swid]
                    needs_to_update = check_if_needs_to_update(
                        sw_expense=expense, ynab_transaction=ynab_transaction
                    )
                    if not needs_to_update:
                        self.logger.info(
                            f"Skipping Splitwise expense {expense['date']} {expense['description']} {expense['swid']} as it is already in YNAB."
                        )
                        continue

                    expense["transaction_id_to_update"] = ynab_transaction["id"]
            total_cost = -int(expense["cost"] * 1000)
            what_i_paid = -(int(expense["cost"] * 1000) - int(expense["owed"] * 1000))
            # This value will be negative (and thus inflow) if other people paid.
            what_i_am_owed = int(expense["owed"] * 1000)
            self.logger.info(
                f"Importing Splitwise expense {expense['date']} {expense['description']} {expense['swid']}"
            )
            if expense["current_user_paid"]:
                transaction = {
                    "account_id": self.ynab_account_id,
                    "date": expense["date"],
                    "amount": int(what_i_am_owed),
                    "payee_name": (
                        expense["group_name"] if expense["group_name"] else "Splitwise"
                    ),
                    "memo": f"{expense['description']}",
                    "cleared": "cleared",
                }
            else:
                transaction = {
                    "account_id": self.ynab_account_id,
                    "date": expense["date"],
                    "amount": int(what_i_paid),
                    "payee_name": (
                        expense["group_name"] if expense["group_name"] else "Splitwise"
                    ),
                    "subtransactions": [
                        {
                            "amount": int(total_cost),
                            "payee_name": expense["description"],
                            "memo": "Total Cost",
                        },
                        {
                            "amount": int(what_i_am_owed),
                            "payee_name": combine_names(expense["users"]),
                            "memo": "What others owe.",
                        },
                    ],
                    "memo": " ".join(
                        [
                            expense["description"].strip(),
                            "with",
                            combine_names(expense["users"]),
                        ]
                    ),
                    "cleared": "cleared",
                }
            if expense.get("swid", ""):
                transaction["memo"] = f"{transaction['memo']} {expense['swid']}"

            import_id_date = datetime.strptime(
                expense["date"], "%Y-%m-%dT%H:%M:%SZ"
            ).strftime("%Y-%m-%d")
            # Generate a random 4-byte hex string for the import hash
            import_hash = secrets.token_hex(4)
            transaction["import_id"] = self.ynab.create_import_id(
                amount=transaction["amount"],
                date=import_id_date,
                import_hash=import_hash,
            )

            transaction_date = datetime.strptime(expense["date"], "%Y-%m-%dT%H:%M:%SZ")
            if transaction_date > datetime.now():
                # Scheduled transactions get uncleared.
                # TODO(carden): Make sure status is updated to cleared once the date passes.
                transaction["cleared"] = "uncleared"
                scheduled_transactions.append(transaction)
            elif expense.get("transaction_id_to_update", None):
                transaction["id"] = expense["transaction_id_to_update"]
                # TODO(carden): We need to remove transactions from other transactions if we update.
                updated_transactions.append(transaction)

            else:
                ynab_transactions.append(transaction)
            # Update swid to transaction mapping so that we don't duplicate transactions if we see multiple of the same
            # transactions in a batch.
            _add_transaction_to_swid_map(swid_to_transaction_mapping, transaction)
        # export to ynab
        if not (ynab_transactions or scheduled_transactions or updated_transactions):
            self.logger.info("No transactions to write to YNAB.")
            return 0
        self.logger.info(f"Writing {len(ynab_transactions)} record(s) to YNAB.")
        self.logger.info(
            f"Writing {len(scheduled_transactions)} scheduled transactions to YNAB."
        )
        self.logger.info(
            f"Writing {len(updated_transactions)} updated transactions to YNAB."
        )
        try:
            if ynab_transactions:
                _ = self.ynab.create_transaction(self.ynab_budget_id, ynab_transactions)
            if updated_transactions:
                _ = self.ynab.update_transactions(
                    self.ynab_budget_id, updated_transactions
                )
            for scheduled_transaction in scheduled_transactions:
                if "cleared" in scheduled_transaction:
                    del scheduled_transaction["cleared"]
                scheduled_transaction["frequency"] = "never"
                _ = self.ynab.create_scheduled_transaction(
                    self.ynab_budget_id, scheduled_transaction
                )
        except Exception as e:
            self.logger.error(f"Error writing transactions to YNAB: {e}")
            # log the transactions that failed
            for transaction in ynab_transactions:
                self.logger.error(
                    f"Failed transaction: {transaction['date']} - {transaction['memo']} - {transaction['amount']}"
                )
            return 1
        return 0

    def ynab_to_sw(self):
        def extract_names(s):
            # Make sure we surround "and" with spaces so that we don't replace names with "and" in them
            s = s.replace(" and ", ",").replace(" ", "")
            names = s.split(",")
            return names

        def update_ynab(transaction, friends):
            amount = transaction["amount"]
            category1_id = transaction[
                "category_id"
            ]  # category already classified by the user
            category1_amount = amount / (len(friends) + 1) * 100

            category2_id = self.ynab.get_category_id(
                self.ynab_budget_id, "Splitwise"
            )  # Splitwise catgeory
            category2_amount = amount * 100 - category1_amount
            transaction["subtransactions"] = [
                {"amount": round(category1_amount / 100), "category_id": category1_id},
                {"amount": round(category2_amount / 100), "category_id": category2_id},
            ]
            transaction["memo"] = "Added to " + transaction["memo"]
            update_transaction = {"transaction": transaction}
            self.ynab.update_transaction(
                self.ynab_budget_id, transaction["id"], update_transaction
            )

        def update_splitwise(transaction, transaction_friends):
            amount = transaction["amount"]
            category1_amount = amount / (len(transaction_friends) + 1) * 100
            expense_friends_ids = []
            sw_friends, sw_friends_ids = (
                self.sw.get_friends()
            )  # get all friends list from Splitwise
            for friend in transaction_friends:
                for sw_friend, friend_id in zip(sw_friends, sw_friends_ids):
                    if friend.lower() in sw_friend.lower():
                        expense_friends_ids.append(friend_id)

            total_amount = -amount / 1000
            expense = {
                "cost": total_amount,
                "date": self.end_date.strftime("%Y-%m-%d %H:%M:%S"),
                "description": transaction["payee_name"],
                "users": [],
            }
            # add current user
            current_user_owed = -round(category1_amount / 100000, 2)
            current_user_expense = {
                "id": self.sw.current_user_id,
                "owed": current_user_owed,
                "paid": total_amount,
            }
            expense["users"].append(current_user_expense)

            # add friends
            total_friends_share = 0
            for i, friend_id in enumerate(expense_friends_ids):
                if i == len(expense_friends_ids) - 1:
                    friends_share = (
                        total_amount - total_friends_share - current_user_owed
                    )
                else:
                    friends_share = round(
                        (total_amount - current_user_owed) / len(expense_friends_ids), 2
                    )
                total_friends_share += friends_share
                user_expense = {"id": friend_id, "owed": friends_share, "paid": 0}
                expense["users"].append(user_expense)

            expense, error = self.sw.create_expense(expense)
            return expense, error

        self.logger.info("Moving transactions from YNAB to Splitwise...")
        # get all accounts linked
        accounts = self.ynab.get_accounts(self.ynab_budget_id)

        for account in accounts["data"]["accounts"]:
            account_id = self.ynab.get_account_id(self.ynab_budget_id, account["name"])
            # get all transactions in last one day
            self.logger.info(
                f"Getting all {account['name']} transactions from {self.ynab_start_date} to {self.end_date}"
            )
            response = self.ynab.get_transactions(
                self.ynab_budget_id,
                account_id,
                since_date=self.ynab_start_date,
                before_date=self.end_date,
            )
            for transaction in response["data"]["transactions"]:
                # check the memo for 'splitwise' keyword
                if not transaction["memo"]:
                    continue
                memo = transaction["memo"].lower()
                if "splitwise" in memo and not "added to splitwise" in memo:
                    # transaction_friends = transaction['memo'].split('with')[1].strip()
                    # Use "with" keyword to imply splitting.
                    # Handle the case where "with" isn't inside the memo, or friends were not noted properly.
                    # Also surround 'with' by spaces so that we don't replace names with 'with' in them
                    memo_split_string = " ".join(
                        transaction.get("memo", "").split(" with ")[1:]
                    ).strip()

                    transaction_friends = extract_names(memo_split_string)

                    # update Splitwise
                    expense, error = update_splitwise(transaction, transaction_friends)

                    # update YNAB
                    if expense and not error:
                        self.logger.info("Added a transaction on Splitwise")
                        update_ynab(transaction, transaction_friends)
                        self.logger.info("Updated YNAB transaction")


def get_secrets_dict(input_dict: dict) -> dict:
    output_dict = {}
    for key, value in input_dict.items():
        output_dict[key.lower()] = value
    return output_dict


def run_for_secrets_dict(secrets_dict: dict) -> int:
    # splitwise creds
    sw_consumer_key = secrets_dict.get("sw_consumer_key")
    assert sw_consumer_key is not None
    sw_consumer_secret = secrets_dict.get("sw_consumer_secret")
    assert sw_consumer_secret is not None
    sw_api_key = secrets_dict.get("sw_api_key")
    assert sw_api_key is not None

    # ynab creds
    ynab_budget_name = secrets_dict.get("ynab_budget_name")
    assert ynab_budget_name is not None
    ynab_personal_access_token = secrets_dict.get("ynab_personal_access_token")
    assert ynab_personal_access_token is not None
    ynab_account_name = os.environ.get("ynab_account_name")
    assert ynab_account_name is not None

    # Config Options
    use_update_date = os.environ.get("sync_update_date", "false").lower() == "true"
    sync_ynab_to_sw = os.environ.get("sync_ynab_to_sw", "true").lower() == "true"

    a = ynab_splitwise_transfer(
        sw_consumer_key,
        sw_consumer_secret,
        sw_api_key,
        ynab_personal_access_token,
        ynab_budget_name,
        ynab_account_name,
        use_update_date=use_update_date,
    )

    # splitwise to ynab
    ret = a.sw_to_ynab()
    if sync_ynab_to_sw:
        # ynab to splitwise
        a.ynab_to_sw()
    return ret


if __name__ == "__main__":
    # load environment variables from yaml file (locally)
    setup_environment_vars()
    ret = 0
    if multi_user_secrets := json.loads(
        os.environ.get("multi_user_secrets_json", "[]")
    ):
        for user_dict in multi_user_secrets:
            user_dict = get_secrets_dict(user_dict)
            if not user_dict.get("user_name", ""):
                continue
            print(f"Running for user {user_dict['user_name']}")
            cur_ret = run_for_secrets_dict(user_dict)
            if cur_ret != 0:
                ret = 1
    else:
        ret = run_for_secrets_dict(dict(os.environ))
    sys.exit(ret)
