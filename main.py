import functools
import json
import os
import logging
import secrets
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, date, timezone
from typing import Optional

import requests

from sw import SW
from ynab import YNABClient
from utils import (
    setup_environment_vars,
    check_if_needs_to_update,
    combine_names,
    extract_swid_from_memo,
    append_memo_tag,
    construct_memo_swexp_tag,
    construct_ynab_marker,
    extract_swexp_from_memo,
)


# (connect, read) timeout applied to any HTTP request whose caller didn't set
# one of its own. Matches the explicit timeout ynab.py already uses.
DEFAULT_HTTP_TIMEOUT = (10, 120)


def install_default_http_timeout(timeout=DEFAULT_HTTP_TIMEOUT):
    """Give every timeout-less requests call in this process a real timeout.

    socket.setdefaulttimeout() does NOT cover requests: requests resolves its
    own default of timeout=None into an explicit sock.settimeout(None), which
    overrides the process-wide socket default and blocks forever. That made
    the 2026-07-16 socket.setdefaulttimeout() fix a no-op and let the run hang
    again on 2026-08-11 until systemd's TimeoutStartSec killed it.

    Patching Session.send is the only hook that reaches the splitwise SDK: it
    builds its Session internally and calls session.send() with no timeout, so
    there is no parameter to pass. Callers that do set a timeout (ynab.py)
    keep theirs.
    """
    original_send = requests.Session.send
    if getattr(original_send, "_default_timeout_installed", False):
        return

    @functools.wraps(original_send)
    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original_send(self, request, **kwargs)

    send._default_timeout_installed = True
    requests.Session.send = send


def _add_transaction_to_swid_map(swid_transaction_map: dict, transaction: dict) -> None:
    # check the memo for 'splitwise' keyword
    memo = transaction.get("memo", "")
    if not memo:
        return
    _, sw_id, _ = extract_swid_from_memo(memo)
    if sw_id is not None:
        swid_transaction_map[sw_id] = transaction
    return


# --- YNAB -> Splitwise (flag-driven) -----------------------------------------

YNAB_FLAG_COLORS = ("red", "orange", "yellow", "green", "blue", "purple")
# YNAB rejects memos longer than this; the original memo is truncated to fit,
# never the [SWEXP:<id>] tag.
YNAB_MEMO_MAX_LENGTH = 200
SPLIT_MODE_HALF = "half"
SPLIT_MODE_PARTNER_PAYS = "partner_pays"
_TRUE_STRINGS = ("true", "1", "yes", "on")
_FALSE_STRINGS = ("false", "0", "no", "off", "")


@dataclass
class YnabToSwConfig:
    enabled: bool = False
    partner_user_id: Optional[int] = None
    flag_split: str = "purple"
    flag_partner_pays: Optional[str] = None
    flag_self_pays: Optional[str] = None
    lookback_days: int = 14
    dry_run: bool = False


def _config_value(secrets_dict: dict, key: str):
    """Look a per-user key up in the user's secrets dict.

    Multi-user dicts are lowercased by get_secrets_dict; the single-user
    fallback passes dict(os.environ), where either case may be used.
    """
    for candidate in (key.lower(), key.upper()):
        if candidate in secrets_dict and secrets_dict[candidate] is not None:
            return secrets_dict[candidate]
    return None


def _parse_bool(value, key: str, errors: list, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    errors.append(f"{key} must be true or false")
    return default


def parse_ynab_to_sw_config(secrets_dict: dict):
    """Parse + validate per-user YNAB -> Splitwise settings.

    Returns (config, errors). errors is only populated when the feature is
    enabled; a disabled feature never blocks a run.
    """
    errors: list = []
    config = YnabToSwConfig()
    config.enabled = _parse_bool(
        _config_value(secrets_dict, "YNAB_TO_SW_ENABLED"), "YNAB_TO_SW_ENABLED", errors, False
    )
    config.dry_run = _parse_bool(
        _config_value(secrets_dict, "YNAB_TO_SW_DRY_RUN"), "YNAB_TO_SW_DRY_RUN", errors, False
    )

    partner = _config_value(secrets_dict, "SW_PARTNER_USER_ID")
    if partner is None or str(partner).strip() == "":
        errors.append("SW_PARTNER_USER_ID is required")
    else:
        try:
            config.partner_user_id = int(str(partner).strip())
            if config.partner_user_id <= 0:
                raise ValueError
        except ValueError:
            errors.append("SW_PARTNER_USER_ID must be a positive integer Splitwise user id")
            config.partner_user_id = None

    def _flag(key, default):
        value = _config_value(secrets_dict, key)
        if value is None or str(value).strip() == "":
            return default
        value = str(value).strip().lower()
        if value not in YNAB_FLAG_COLORS:
            errors.append(f"{key} must be one of {', '.join(YNAB_FLAG_COLORS)}")
        return value

    config.flag_split = _flag("YNAB_TO_SW_FLAG_SPLIT", "purple")
    config.flag_partner_pays = _flag("YNAB_TO_SW_FLAG_PARTNER_PAYS", None)
    config.flag_self_pays = _flag("YNAB_TO_SW_FLAG_SELF_PAYS", None)
    if config.flag_partner_pays is None:
        errors.append("YNAB_TO_SW_FLAG_PARTNER_PAYS is required")
    if config.flag_self_pays is None:
        errors.append("YNAB_TO_SW_FLAG_SELF_PAYS is required")
    flags = [f for f in (config.flag_split, config.flag_partner_pays, config.flag_self_pays) if f]
    if len(set(flags)) != len(flags):
        errors.append("YNAB_TO_SW flag colours must all be different")

    lookback = _config_value(secrets_dict, "YNAB_TO_SW_LOOKBACK_DAYS")
    if lookback is not None and str(lookback).strip() != "":
        try:
            config.lookback_days = int(str(lookback).strip())
            if config.lookback_days <= 0:
                raise ValueError
        except ValueError:
            errors.append("YNAB_TO_SW_LOOKBACK_DAYS must be a positive integer")
            config.lookback_days = 14

    if not config.enabled:
        errors = []
    return config, errors


def _cents_to_str(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def compute_shares(cost_cents: int, mode: str) -> tuple:
    """Return (owner_owed_cents, partner_owed_cents); they sum to cost_cents.

    Half: partner owes the floor half, so an odd cent stays with the payer.
    """
    if mode == SPLIT_MODE_HALF:
        partner = cost_cents // 2
    elif mode == SPLIT_MODE_PARTNER_PAYS:
        partner = cost_cents
    else:
        raise ValueError(f"unknown split mode {mode}")
    return cost_cents - partner, partner


def build_splitwise_expense(transaction: dict, mode: str, owner_id, partner_id) -> dict:
    """Build the sw.create_expense() request for a flagged YNAB outflow."""
    milliunits = -int(transaction["amount"])
    if milliunits <= 0 or milliunits % 10 != 0:
        raise ValueError("amount is not a whole-cent outflow")
    cost_cents = milliunits // 10
    owner_owed, partner_owed = compute_shares(cost_cents, mode)
    # Noon UTC keeps the calendar day stable in any US timezone, and
    # sw_to_ynab reads the date back as YYYY-MM-DD.
    date = datetime.strptime(transaction["date"], "%Y-%m-%d").strftime("%Y-%m-%dT12:00:00Z")
    return {
        "cost": _cents_to_str(cost_cents),
        "date": date,
        "description": (transaction.get("payee_name") or "").strip() or "YNAB transaction",
        "details": construct_ynab_marker(transaction["id"]),
        "group_id": 0,
        "users": [
            {"id": owner_id, "paid": _cents_to_str(cost_cents), "owed": _cents_to_str(owner_owed)},
            {"id": partner_id, "paid": _cents_to_str(0), "owed": _cents_to_str(partner_owed)},
        ],
    }


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
            # Splitwise hands back a full ISO timestamp, but YNAB's transaction
            # `date` must be a plain YYYY-MM-DD. The API tolerated the timestamp
            # until 2026-08-01, then began rejecting the whole batch with
            # "date must be formatted as YYYY-MM-DD".
            transaction_date = datetime.strptime(expense["date"], "%Y-%m-%dT%H:%M:%SZ")
            transaction_date_str = transaction_date.strftime("%Y-%m-%d")
            if expense["current_user_paid"]:
                transaction = {
                    "account_id": self.ynab_account_id,
                    "date": transaction_date_str,
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
                    "date": transaction_date_str,
                    "amount": int(what_i_paid),
                    "payee_name": (
                        expense["group_name"] if expense["group_name"] else "Splitwise"
                    ),
                    "memo": " ".join(
                        [
                            expense["description"].strip(),
                            "with",
                            combine_names(expense["users"]),
                        ]
                    ),
                    "cleared": "cleared",
                }
                # Only split when the breakdown is meaningful, i.e. the total cost differs
                # from what the current user owes. For one-way transfers (settle-up
                # payments, debt consolidation, or an expense entirely for the current
                # user), what_i_am_owed is 0 and the split would just be
                # "Total Cost + $0", so a simple transaction is clearer.
                if int(what_i_am_owed) != 0:
                    transaction["subtransactions"] = [
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
                    ]
            if expense.get("swid", ""):
                transaction["memo"] = f"{transaction['memo']} {expense['swid']}"

            # Generate a random 4-byte hex string for the import hash
            import_hash = secrets.token_hex(4)
            transaction["import_id"] = self.ynab.create_import_id(
                amount=transaction["amount"],
                date=transaction_date_str,
                import_hash=import_hash,
            )

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

    def ynab_to_sw(self, config: "YnabToSwConfig") -> int:
        """Flag-driven YNAB -> Splitwise sync. See README "YNAB -> Splitwise".

        For each outflow in this user's budget flagged with an actionable
        colour, create (or reuse) a Splitwise expense with the partner, then
        tag the YNAB memo and clear the flag. Returns 0 on success, 1 if any
        transaction failed (its flag is left in place for the next run).

        Logs carry counts, ids and status only -- never payees, amounts or memos.
        """
        log = self.logger
        modes = {
            config.flag_split: SPLIT_MODE_HALF,
            config.flag_partner_pays: SPLIT_MODE_PARTNER_PAYS,
        }
        since = datetime.now().date() - timedelta(days=config.lookback_days)
        log.info(
            "YNAB->Splitwise: scanning budget since %s (dry_run=%s).",
            since.isoformat(), config.dry_run,
        )
        try:
            response = self.ynab.get_transactions(
                self.ynab_budget_id, since_date=since.isoformat()
            )
            transactions = response["data"]["transactions"]
        except Exception as exc:
            log.error("YNAB->Splitwise: failed to fetch transactions (%s).",
                      exc.__class__.__name__)
            return 1

        counts = {"self_pays": 0, "skipped": 0, "already_tagged": 0}
        candidates = []
        for transaction in transactions:
            flag = transaction.get("flag_color")
            if flag == config.flag_self_pays:
                counts["self_pays"] += 1
                continue
            if flag not in modes:
                continue
            if (
                transaction.get("deleted")
                or transaction.get("amount", 0) >= 0
                or transaction.get("transfer_account_id")
                or transaction.get("subtransactions")
                or transaction.get("account_id") == self.ynab_account_id
            ):
                counts["skipped"] += 1
                continue
            memo = transaction.get("memo") or ""
            if extract_swexp_from_memo(memo) is not None or extract_swid_from_memo(memo)[1] is not None:
                counts["already_tagged"] += 1
                continue
            candidates.append((transaction, modes[flag]))

        log.info(
            "YNAB->Splitwise: %d candidate(s); ignored %d self-pays, %d ineligible, %d already tagged.",
            len(candidates), counts["self_pays"], counts["skipped"], counts["already_tagged"],
        )
        if not candidates:
            return 0

        owner_id = self.sw.current_user_id
        if str(owner_id) == str(config.partner_user_id):
            log.error("YNAB->Splitwise: partner user id equals the current Splitwise user; aborting.")
            return 1

        try:
            # Expense dates are the YNAB transaction date (noon UTC); a day of
            # margin keeps timezone edges inside the window.
            dated_after = (since - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            marker_map = self.sw.get_ynab_marker_expense_ids(dated_after=dated_after)
        except Exception as exc:
            log.error("YNAB->Splitwise: failed to read Splitwise expenses (%s); no writes made.",
                      exc.__class__.__name__)
            return 1

        created = reused = patched = failed = 0
        for transaction, mode in candidates:
            transaction_id = transaction["id"]
            try:
                expense_request = build_splitwise_expense(
                    transaction, mode, owner_id, config.partner_user_id
                )
                expense_id = marker_map.get(transaction_id)
                if config.dry_run:
                    if expense_id is None:
                        created += 1
                    else:
                        reused += 1
                    continue
                if expense_id is None:
                    expense, errors = self.sw.create_expense(expense_request)
                    if errors or expense is None or not expense.getId():
                        raise RuntimeError("Splitwise rejected the expense")
                    expense_id = expense.getId()
                    marker_map[transaction_id] = expense_id
                    created += 1
                    log.info("YNAB->Splitwise: transaction %s -> created expense %s.",
                             transaction_id, expense_id)
                else:
                    reused += 1
                    log.info("YNAB->Splitwise: transaction %s -> reusing expense %s.",
                             transaction_id, expense_id)

                # Only memo + flag_color: never send the whole transaction back.
                self.ynab.update_transactions(
                    self.ynab_budget_id,
                    [{
                        "id": transaction_id,
                        "memo": append_memo_tag(
                            transaction.get("memo"),
                            construct_memo_swexp_tag(expense_id),
                            YNAB_MEMO_MAX_LENGTH,
                        ),
                        "flag_color": None,
                    }],
                )
                patched += 1
            except Exception as exc:
                failed += 1
                log.error(
                    "YNAB->Splitwise: transaction %s failed (%s); flag left in place.",
                    transaction_id, exc.__class__.__name__,
                )

        if config.dry_run:
            log.info("YNAB->Splitwise DRY RUN: would create %d, would reuse %d; no writes made.",
                     created, reused)
        else:
            log.info("YNAB->Splitwise: created %d, reused %d, YNAB updated %d, failed %d.",
                     created, reused, patched, failed)
        return 1 if failed else 0


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

    # Validate the per-user YNAB -> Splitwise config before touching any API.
    ynab_to_sw_config, ynab_to_sw_errors = parse_ynab_to_sw_config(secrets_dict)
    run_ynab_to_sw = sync_ynab_to_sw and ynab_to_sw_config.enabled
    if run_ynab_to_sw and ynab_to_sw_errors:
        for error in ynab_to_sw_errors:
            logging.getLogger(__name__).error("YNAB->Splitwise config error: %s", error)

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
    if run_ynab_to_sw:
        if ynab_to_sw_errors:
            # Invalid config: skip the YNAB -> Splitwise pass entirely (no writes).
            ret = 1
        elif a.ynab_to_sw(ynab_to_sw_config) != 0:
            ret = 1
    return ret


def run_all_users() -> int:
    """Run every configured user; one user's failure never stops the others.

    Each user gets a fresh secrets dict and a fresh ynab_splitwise_transfer
    (no shared per-user state). Returns 1 if any user failed, else 0.
    """
    ret = 0
    if multi_user_secrets := json.loads(
        os.environ.get("multi_user_secrets_json", "[]")
    ):
        for index, user_dict in enumerate(multi_user_secrets):
            user_dict = get_secrets_dict(user_dict)
            if not user_dict.get("user_name", ""):
                continue
            print(f"Running for user {user_dict['user_name']}")
            try:
                cur_ret = run_for_secrets_dict(user_dict)
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "Run for user #%d failed (%s); continuing with the next user.",
                    index, exc.__class__.__name__,
                )
                cur_ret = 1
            if cur_ret != 0:
                ret = 1
    else:
        ret = run_for_secrets_dict(dict(os.environ))
    return ret


if __name__ == "__main__":
    # Network timeouts. The splitwise SDK issues requests with no timeout, so
    # a stalled connection hangs the run until systemd kills it (observed
    # 2026-07-16 and again 2026-08-11).
    install_default_http_timeout()
    # Secondary net for any raw-socket I/O that doesn't go through requests.
    # Does NOT cover requests itself -- see install_default_http_timeout.
    socket.setdefaulttimeout(120)
    # load environment variables from yaml file (locally)
    setup_environment_vars()
    sys.exit(run_all_users())
