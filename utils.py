import hashlib
import logging
import os
import re
import yaml

def setup_environment_vars():
    # Check if running in GitHub Actions
    if os.getenv('GITHUB_ACTIONS'):
        return

    # for local development
    with open('creds.yaml', 'r') as file:
        secrets = yaml.safe_load(file)
        for key, value in secrets.items():
            os.environ[key] = value

def combine_names(string_list):
    if not string_list:
        return ""

    if len(string_list) == 1:
        return string_list[0]

    return ', '.join(string_list[:-1]) + ' and ' + string_list[-1]

def extract_swid_from_memo(memo) -> tuple[str, int, str]:
    """
    Use regex to find the ID tag in brackets, e.g., [SWID12345]

    Args:
        memo (str): The memo field of a transaction

    Returns:
        tuple[str, int, str]: The full match, the ID number, and the truncated hash
    """
    match = re.search(r"\[SWID:(\d+)-(\w+)\]", memo)
    if match:
        return match.group(0), int(match.group(1)), match.group(2) 
    return None, None, None

def check_if_needs_to_update(sw_expense: dict, ynab_transaction: dict) -> bool:
    """
    Parses SWID and updated time hash from both sw and ynab transactions and compares them.
    Determines if ynab transaction should update based on the criteria.

    YNAB transaction should update if the SWID hash changes but the expense ID is the same.
    """
    logger = logging.getLogger(__name__)
    expense_swid = sw_expense.get('swid', '')
    _, expense_swid, _ = extract_swid_from_memo(sw_expense.get('swid', ''))
    if not expense_swid:
        logger.warning(f"No SWID found in Splitwise expense {sw_expense['id']}")
        return False
    _, ynab_swid, ynab_hash = extract_swid_from_memo(ynab_transaction.get('memo', ''))
    if not ynab_swid:
        logger.warning(f"No SWID found in YNAB transaction {ynab_transaction['id']}")
        return False
    if ynab_hash is None:
        logger.error("No hash found in YNAB memo but swid found in memo")
    if ynab_swid != expense_swid:
        logger.error(f"SWID mismatch: {ynab_swid} != {expense_swid}")
        return False

    sw_update_time = sw_expense.get('updated_time', '')
    if not sw_update_time:
        logger.warning(f"No updated time found in Splitwise expense {sw_expense}")
        return False

    generated_hash_for_sw_update_time = generate_truncated_hash_for_updated_time(sw_update_time)
    return ynab_swid == expense_swid and generated_hash_for_sw_update_time != ynab_hash

def generate_truncated_hash_for_updated_time(updated_at: str):
    """
    Useful for generating a unique identifier for a splitwise expense combined with updated date.

    Args:
        updated_at (str): The timestamp of the last update to the expense. From expense.getUpdatedAt()

    Returns:
        str: 4 character hash to represent the updated time
    """
    # Create an MD5 hash of the combined string
    hash_object = hashlib.md5(updated_at.encode())

    # Truncate the hash to 4 characters for conciseness
    # This should still prevent collisions
    short_hash = hash_object.hexdigest()[:4]
    return short_hash

def construct_memo_swid_tag(expense_id: int, updated_at: str):
    """
    Useful for generating a unique identifier for a splitwise expense combined with updated date.
    """
    short_hash = generate_truncated_hash_for_updated_time(updated_at)
    return f"[SWID:{expense_id}-{short_hash}]"
