import hashlib
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
