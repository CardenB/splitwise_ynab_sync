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


# --- YNAB -> Splitwise (flag-driven) helpers ---------------------------------
#
# Tag appended to a YNAB transaction's memo once the flag-driven YNAB ->
# Splitwise path has created (or found) its Splitwise expense. Deliberately
# distinct from the [SWID:<id>-<hash>] tag: SWID tags mark rows that
# sw_to_ynab *imported* into the Splitwise account and carry an updated_at
# hash that sw_to_ynab uses for change detection. Reusing that format on the
# originating card transaction would let sw_to_ynab mistake it for one of its
# own imports (e.g. if the row were ever moved into the Splitwise account).
SWEXP_TAG_RE = re.compile(r"\[SWEXP:(\d+)\]")

# Marker written into the Splitwise expense's `details` so a rerun can find an
# expense it already created for a YNAB transaction (idempotency).
YNAB_MARKER_PREFIX = "ynab:"
YNAB_MARKER_RE = re.compile(r"ynab:([0-9A-Za-z-]+)")


def construct_memo_swexp_tag(expense_id) -> str:
    return f"[SWEXP:{int(expense_id)}]"


def extract_swexp_from_memo(memo):
    """Return the Splitwise expense id from a [SWEXP:<id>] tag, or None."""
    if not memo:
        return None
    match = SWEXP_TAG_RE.search(memo)
    return int(match.group(1)) if match else None


def construct_ynab_marker(transaction_id: str) -> str:
    return f"{YNAB_MARKER_PREFIX}{transaction_id}"


def extract_ynab_markers(details) -> list:
    """Return every YNAB transaction id referenced by markers in `details`."""
    if not details:
        return []
    return YNAB_MARKER_RE.findall(details)


def append_memo_tag(memo, tag: str, max_len: int) -> str:
    """Append `tag` to `memo`, truncating the original memo (never the tag)
    so the result fits in `max_len` characters."""
    memo = (memo or "").strip()
    if not memo:
        return tag
    room = max_len - len(tag) - 1  # one space separator
    if room <= 0:
        return tag
    if len(memo) > room:
        memo = memo[:room].rstrip()
    return f"{memo} {tag}" if memo else tag


# Split mode remembered in the Splitwise expense details next to the marker,
# e.g. "ynab:<transaction id> split:half", so later YNAB edits can be synced
# with the mode the user originally chose.
SPLIT_MODE_RE = re.compile(r"\bsplit:(half|partner_pays)\b")


def extract_split_mode(details):
    """Return the stored split mode ('half' / 'partner_pays') or None."""
    if not details:
        return None
    match = SPLIT_MODE_RE.search(details)
    return match.group(1) if match else None


def details_with_marker_and_mode(details, transaction_id: str, mode: str) -> str:
    """Return details with exactly one `ynab:<id> split:<mode>` entry for this
    transaction, keeping any other text the user wrote."""
    text = details or ""
    text = re.sub(r"[ \t]*" + re.escape(construct_ynab_marker(transaction_id)) + r"\b", "", text)
    text = SPLIT_MODE_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    lines.append(f"{construct_ynab_marker(transaction_id)} split:{mode}")
    return "\n".join(lines)
