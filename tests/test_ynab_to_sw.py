"""Tests for the flag-driven YNAB -> Splitwise path. Mocks only: no real API calls."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import main
from sw import SW
from main import (
    YnabToSwConfig,
    build_splitwise_expense,
    load_state,
    state_path_for_budget,
    compute_shares,
    parse_ynab_to_sw_config,
    run_all_users,
    run_for_secrets_dict,
    ynab_splitwise_transfer,
    SPLIT_MODE_HALF,
    SPLIT_MODE_PARTNER_PAYS,
    YNAB_MEMO_MAX_LENGTH,
)
from utils import append_memo_tag, extract_swexp_from_memo, extract_ynab_markers

SW_ACCOUNT_ID = "sw-account"
TODAY = date.today().isoformat()


def txn(tid, amount, flag, **kw):
    t = {
        "id": tid,
        "date": TODAY,
        "amount": amount,
        "flag_color": flag,
        "payee_name": "Payee " + tid,
        "memo": kw.pop("memo", None),
        "deleted": False,
        "transfer_account_id": None,
        "subtransactions": [],
        "account_id": "card-account",
    }
    t.update(kw)
    return t


def make_fake_sw(current_user_id, marker_map=None, next_expense_id=1000):
    sw = MagicMock(name=f"SW({current_user_id})")
    sw.current_user_id = current_user_id
    sw.current_user = f"User - {current_user_id}"
    sw.get_expenses.return_value = []  # sw_to_ynab: nothing to import
    sw.get_ynab_marker_expense_ids.return_value = dict(marker_map or {})
    counter = {"n": next_expense_id}

    def create(expense):
        counter["n"] += 1
        created = MagicMock()
        created.getId.return_value = counter["n"]
        return created, None

    sw.create_expense.side_effect = create
    # Existing expenses by id, for association lookups (missing -> 404 -> None).
    sw.expenses = {}
    sw.get_expense.side_effect = lambda expense_id: sw.expenses.get(int(expense_id))
    sw.expense_shares_cents.side_effect = SW.expense_shares_cents
    sw.update_expense.return_value = (MagicMock(), None)
    return sw


def sw_user(uid, paid, owed):
    u = MagicMock()
    u.getId.return_value = uid
    u.getPaidShare.return_value = paid
    u.getOwedShare.return_value = owed
    return u


def sw_expense(cost, users, deleted=None, details="ynab:x", date=None, description="Payee a"):
    """users: list of (id, paid, owed) decimal strings, as the SDK returns them."""
    e = MagicMock()
    e.getDate.return_value = date or f"{TODAY}T12:00:00Z"
    e.getDescription.return_value = description
    e.getCost.return_value = cost
    e.getUsers.return_value = [sw_user(*u) for u in users]
    e.getDeletedAt.return_value = deleted
    e.getDetails.return_value = details
    return e


def make_fake_ynab(delta, baseline=(), knowledge=200, budget_id="budget"):
    """Fake YNAB client. A call without last_knowledge_of_server is the
    baseline (full) read; a call with it returns `ynab.delta`."""
    ynab = MagicMock(name=f"YNAB({budget_id})")
    ynab.get_budget_id.return_value = budget_id
    ynab.get_account_id.return_value = SW_ACCOUNT_ID
    ynab.delta = list(delta)
    ynab.baseline = list(baseline)
    ynab.knowledge = knowledge

    def get_transactions(budget, account_id=None, since_date=None, before_date=None,
                         last_knowledge_of_server=None):
        assert account_id is None and since_date is None, "ynab_to_sw reads budget-wide"
        rows = ynab.baseline if last_knowledge_of_server is None else ynab.delta
        return {"data": {"transactions": list(rows), "server_knowledge": ynab.knowledge}}

    ynab.get_transactions.side_effect = get_transactions
    ynab.get_scheduled_transactions.return_value = {"data": {"scheduled_transactions": []}}
    return ynab


def seed_state(state_dir, budget_id="budget", knowledge=100, preexisting=None):
    path = state_path_for_budget(state_dir, budget_id)
    with open(path, "w") as f:
        json.dump({"server_knowledge": knowledge, "preexisting_flags": preexisting or {}}, f)
    return path


def read_knowledge(state_dir, budget_id="budget"):
    state = load_state(state_path_for_budget(state_dir, budget_id))
    return None if state is None else state["server_knowledge"]


def config(**kw):
    base = dict(
        enabled=True,
        partner_user_id=222,
        flag_split="purple",
        flag_partner_pays="green",
        flag_self_pays="blue",
        dry_run=False,
    )
    base.update(kw)
    return YnabToSwConfig(**base)


class TransferTestBase(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="ynab-to-sw-test-")
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)

    def cfg(self, **kw):
        kw.setdefault("state_dir", self.state_dir)
        return config(**kw)

    def make_transfer(self, transactions, marker_map=None, current_user_id=111, seed=True,
                      baseline=()):
        """Transfer whose next delta is `transactions`. seed=True pre-writes
        state at knowledge 100 so the run processes the delta."""
        if seed:
            seed_state(self.state_dir)
        self.fake_sw = make_fake_sw(current_user_id, marker_map)
        self.fake_ynab = make_fake_ynab(transactions, baseline=baseline)
        with patch("main.SW", return_value=self.fake_sw), patch(
            "main.YNABClient", return_value=self.fake_ynab
        ):
            return ynab_splitwise_transfer("k", "s", "a", "t", "budget", "Splitwise")

    def created_expenses(self):
        return [c.args[0] for c in self.fake_sw.create_expense.call_args_list]

    def patches(self):
        """List of the single-transaction payloads sent to YNAB."""
        out = []
        for c in self.fake_ynab.update_transactions.call_args_list:
            budget_id, payload = c.args
            self.assertEqual(len(payload), 1)
            out.append(payload[0])
        return out


class TestShares(unittest.TestCase):
    def test_half_even(self):
        self.assertEqual(compute_shares(10000, SPLIT_MODE_HALF), (5000, 5000))

    def test_half_odd_extra_cent_owed_by_payer(self):
        owner, partner = compute_shares(1001, SPLIT_MODE_HALF)
        self.assertEqual((owner, partner), (501, 500))
        self.assertEqual(owner + partner, 1001)

    def test_partner_pays(self):
        self.assertEqual(compute_shares(4321, SPLIT_MODE_PARTNER_PAYS), (0, 4321))

    def test_build_expense_odd_cents(self):
        e = build_splitwise_expense(txn("t1", -10010, "purple"), SPLIT_MODE_HALF, 111, 222)
        self.assertEqual(e["cost"], "10.01")
        owner, partner = e["users"]
        self.assertEqual(owner, {"id": 111, "paid": "10.01", "owed": "5.01"})
        self.assertEqual(partner, {"id": 222, "paid": "0.00", "owed": "5.00"})
        self.assertEqual(e["date"], f"{TODAY}T12:00:00Z")
        self.assertEqual(extract_ynab_markers(e["details"]), ["t1"])
        self.assertEqual(e["description"], "Payee t1")

    def test_build_expense_payee_fallback(self):
        e = build_splitwise_expense(txn("t1", -1000, "purple", payee_name=None), SPLIT_MODE_HALF, 1, 2)
        self.assertEqual(e["description"], "YNAB transaction")

    def test_build_expense_rejects_sub_cent(self):
        with self.assertRaises(ValueError):
            build_splitwise_expense(txn("t1", -1005, "purple"), SPLIT_MODE_HALF, 1, 2)


class TestMemo(unittest.TestCase):
    def test_append(self):
        self.assertEqual(append_memo_tag("lunch", "[SWEXP:5]", 200), "lunch [SWEXP:5]")
        self.assertEqual(append_memo_tag(None, "[SWEXP:5]", 200), "[SWEXP:5]")

    def test_truncates_memo_never_tag(self):
        tag = "[SWEXP:123456789]"
        out = append_memo_tag("x" * 500, tag, YNAB_MEMO_MAX_LENGTH)
        self.assertEqual(len(out), YNAB_MEMO_MAX_LENGTH)
        self.assertTrue(out.endswith(" " + tag))
        self.assertEqual(extract_swexp_from_memo(out), 123456789)


class TestConfig(unittest.TestCase):
    GOOD = {
        "ynab_to_sw_enabled": "true",
        "sw_partner_user_id": "222",
        "ynab_to_sw_flag_partner_pays": "green",
        "ynab_to_sw_flag_self_pays": "blue",
    }

    def test_disabled_by_default(self):
        cfg, errors = parse_ynab_to_sw_config({})
        self.assertFalse(cfg.enabled)
        self.assertEqual(errors, [])

    def test_valid(self):
        cfg, errors = parse_ynab_to_sw_config(dict(self.GOOD))
        self.assertEqual(errors, [])
        self.assertEqual(
            (cfg.partner_user_id, cfg.flag_split, cfg.flag_partner_pays, cfg.flag_self_pays,
             cfg.dry_run, cfg.state_dir),
            (222, "purple", "green", "blue", False, main.DEFAULT_STATE_DIR),
        )

    def test_uppercase_env_keys(self):
        cfg, errors = parse_ynab_to_sw_config({k.upper(): v for k, v in self.GOOD.items()})
        self.assertEqual(errors, [])
        self.assertTrue(cfg.enabled)

    def test_missing_partner(self):
        d = dict(self.GOOD); del d["sw_partner_user_id"]
        _, errors = parse_ynab_to_sw_config(d)
        self.assertTrue(any("SW_PARTNER_USER_ID" in e for e in errors))

    def test_missing_flags(self):
        for key in ("ynab_to_sw_flag_partner_pays", "ynab_to_sw_flag_self_pays"):
            d = dict(self.GOOD); del d[key]
            _, errors = parse_ynab_to_sw_config(d)
            self.assertTrue(any(key.upper() in e for e in errors), key)

    def test_duplicate_colours(self):
        d = dict(self.GOOD, ynab_to_sw_flag_self_pays="purple")
        _, errors = parse_ynab_to_sw_config(d)
        self.assertTrue(any("different" in e for e in errors))

    def test_invalid_colour(self):
        d = dict(self.GOOD, ynab_to_sw_flag_split="pink")
        _, errors = parse_ynab_to_sw_config(d)
        self.assertEqual(len(errors), 1)

    def test_state_dir_key(self):
        cfg, _ = parse_ynab_to_sw_config(dict(self.GOOD, ynab_to_sw_state_dir="/x/y"))
        self.assertEqual(cfg.state_dir, "/x/y")

    def test_default_state_dir_is_gitignored(self):
        self.assertEqual(os.path.basename(main.DEFAULT_STATE_DIR), ".state")
        root = os.path.dirname(os.path.abspath(main.__file__))
        with open(os.path.join(root, ".gitignore")) as f:
            self.assertIn(".state/", f.read().split())


class TestYnabToSw(TransferTestBase):
    def test_split_even_and_partner_pays(self):
        t = self.make_transfer([txn("a", -200000, "purple"), txn("b", -75000, "green")])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        split, partner_pays = self.created_expenses()
        self.assertEqual([u["owed"] for u in split["users"]], ["100.00", "100.00"])
        self.assertEqual(split["users"][0]["paid"], "200.00")
        self.assertEqual([u["owed"] for u in partner_pays["users"]], ["0.00", "75.00"])
        self.assertEqual(partner_pays["users"][1]["id"], 222)
        self.assertEqual(len(self.patches()), 2)

    def test_split_odd_cents(self):
        t = self.make_transfer([txn("a", -10010, "purple")])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        (e,) = self.created_expenses()
        self.assertEqual([u["owed"] for u in e["users"]], ["5.01", "5.00"])

    def test_self_pays_ignored_flag_left(self):
        t = self.make_transfer([txn("a", -1000, "blue")])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()
        self.fake_sw.get_ynab_marker_expense_ids.assert_not_called()

    def test_filters(self):
        transactions = [
            txn("inflow", 5000, "purple"),
            txn("zero", 0, "purple"),
            txn("transfer", -5000, "purple", transfer_account_id="x"),
            txn("split", -5000, "purple", subtransactions=[{"amount": -5000}]),
            txn("deleted", -5000, "purple", deleted=True),
            txn("swacct", -5000, "purple", account_id=SW_ACCOUNT_ID),
            txn("swid", -5000, "purple", memo="x [SWID:42-abcd]"),
            txn("unflagged", -5000, None),
            txn("red", -5000, "red"),
        ]
        t = self.make_transfer(transactions)
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_delta_fetch_uses_saved_knowledge(self):
        t = self.make_transfer([])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.fake_ynab.get_transactions.assert_called_once_with(
            "budget", last_knowledge_of_server=100
        )
        self.assertEqual(read_knowledge(self.state_dir), 200)

    def test_idempotent_reuse_via_marker(self):
        """Marker-only association: no second expense, tag added once."""
        t = self.make_transfer([txn("a", -1000, "purple", memo="lunch")], marker_map={"a": 777})
        self.fake_sw.expenses[777] = sw_expense("1.00", [(111, "1.00", "0.50"), (222, "0.00", "0.50")])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_sw.update_expense.assert_not_called()
        (p,) = self.patches()
        self.assertEqual(p, {"id": "a", "flag_color": None, "memo": "lunch [SWEXP:777]"})

    def test_patch_payload_only_memo_and_flag(self):
        t = self.make_transfer([txn("a", -1000, "purple", memo="groceries")])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        (p,) = self.patches()
        self.assertEqual(set(p), {"id", "memo", "flag_color"})
        self.assertIsNone(p["flag_color"])
        self.assertEqual(p["id"], "a")
        self.assertEqual(p["memo"], "groceries [SWEXP:1001]")

    def test_long_memo_truncated_in_patch(self):
        t = self.make_transfer([txn("a", -1000, "purple", memo="m" * 400)])
        t.ynab_to_sw(self.cfg())
        (p,) = self.patches()
        self.assertLessEqual(len(p["memo"]), YNAB_MEMO_MAX_LENGTH)
        self.assertTrue(p["memo"].endswith("[SWEXP:1001]"))

    def test_splitwise_failure_leaves_flag_and_continues(self):
        t = self.make_transfer([txn("a", -1000, "purple"), txn("b", -2000, "purple")])
        ok = MagicMock(); ok.getId.return_value = 55
        self.fake_sw.create_expense.side_effect = [RuntimeError("boom"), (ok, None)]
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        patched_ids = [p["id"] for p in self.patches()]
        self.assertEqual(patched_ids, ["b"])  # "a" not patched: flag stays

    def test_splitwise_errors_object_is_failure(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.fake_sw.create_expense.side_effect = None
        self.fake_sw.create_expense.return_value = (None, MagicMock())
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        self.fake_ynab.update_transactions.assert_not_called()

    def test_ynab_patch_failure_returns_nonzero(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.fake_ynab.update_transactions.side_effect = RuntimeError("http")
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)

    def test_marker_read_failure_no_writes(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.fake_sw.get_ynab_marker_expense_ids.side_effect = RuntimeError("down")
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_partner_is_self_aborts(self):
        t = self.make_transfer([txn("a", -1000, "purple")], current_user_id=222)
        self.assertEqual(t.ynab_to_sw(self.cfg(partner_user_id=222)), 1)
        self.fake_sw.create_expense.assert_not_called()

    def test_dry_run_writes_nothing(self):
        t = self.make_transfer(
            [txn("a", -1000, "purple"), txn("b", -1000, "green")], marker_map={"b": 9}
        )
        self.assertEqual(t.ynab_to_sw(self.cfg(dry_run=True)), 0)
        self.fake_sw.get_ynab_marker_expense_ids.assert_called_once()
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_logs_have_no_payee_amount_or_memo(self):
        t = self.make_transfer([txn("a", -12345, "purple", memo="secret memo")])
        with self.assertLogs("main", level="INFO") as cm:
            t.ynab_to_sw(self.cfg())
        text = "\n".join(cm.output)
        for needle in ("Payee a", "secret memo", "12345", "123.45", "61.72"):
            self.assertNotIn(needle, text)


MONTHS_AGO = (date.today() - timedelta(days=200)).isoformat()
HALF_DETAILS = "ynab:a split:half"
PP_DETAILS = "ynab:a split:partner_pays"


class TestDeltaState(TransferTestBase):
    def test_first_run_records_baseline_and_processes_nothing(self):
        old_flagged = txn("old", -5000, "purple", date=MONTHS_AGO)
        t = self.make_transfer([], seed=False, baseline=[old_flagged, txn("plain", -100, None)])
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.fake_ynab.get_transactions.assert_called_once_with("budget")
        self.fake_sw.create_expense.assert_not_called()
        self.fake_sw.get_ynab_marker_expense_ids.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()
        state = load_state(state_path_for_budget(self.state_dir, "budget"))
        self.assertEqual(state["server_knowledge"], 200)
        self.assertEqual(state["preexisting_flags"], {"old": "purple"})

    def test_baseline_saved_in_dry_run(self):
        t = self.make_transfer([], seed=False)
        self.assertEqual(t.ynab_to_sw(self.cfg(dry_run=True)), 0)
        self.assertEqual(read_knowledge(self.state_dir), 200)

    def test_second_run_processes_newly_flagged_old_transaction(self):
        t = self.make_transfer([], seed=False)
        t.ynab_to_sw(self.cfg())  # baseline at 200
        self.fake_ynab.delta = [txn("a", -3000, "purple", date=MONTHS_AGO)]
        self.fake_ynab.knowledge = 300
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        (e,) = self.created_expenses()
        self.assertEqual(e["date"], f"{MONTHS_AGO}T12:00:00Z")
        self.fake_ynab.get_transactions.assert_called_with("budget", last_knowledge_of_server=200)
        self.assertEqual(read_knowledge(self.state_dir), 300)

    def test_preexisting_flag_unrelated_edit_skipped_until_flag_changes(self):
        seed_state(self.state_dir, preexisting={"old": "purple"})
        t = self.make_transfer([txn("old", -5000, "purple", date=MONTHS_AGO)], seed=False)
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.fake_sw.create_expense.assert_not_called()
        # Flag cleared, then set again later: now it counts as new.
        self.fake_ynab.delta = [txn("old", -5000, None, date=MONTHS_AGO)]
        self.fake_ynab.knowledge = 300
        t.ynab_to_sw(self.cfg())
        self.fake_ynab.delta = [txn("old", -5000, "purple", date=MONTHS_AGO)]
        self.fake_ynab.knowledge = 400
        t.ynab_to_sw(self.cfg())
        self.assertEqual(len(self.created_expenses()), 1)

    def test_state_not_advanced_on_failure_and_retried(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        ok = MagicMock(); ok.getId.return_value = 55
        self.fake_sw.create_expense.side_effect = [RuntimeError("boom"), (ok, None)]
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        self.assertEqual(read_knowledge(self.state_dir), 100)
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)  # same delta seen again
        self.assertEqual(self.fake_ynab.get_transactions.call_args_list[-1].kwargs,
                         {"last_knowledge_of_server": 100})
        self.assertEqual([p["id"] for p in self.patches()], ["a"])
        self.assertEqual(read_knowledge(self.state_dir), 200)

    def test_dry_run_does_not_advance(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.assertEqual(t.ynab_to_sw(self.cfg(dry_run=True)), 0)
        self.assertEqual(read_knowledge(self.state_dir), 100)

    def test_own_patch_echo_is_skipped(self):
        t = self.make_transfer([txn("a", -1000, "purple", memo="m")])
        t.ynab_to_sw(self.cfg())
        (p,) = self.patches()
        echo = txn("a", -1000, p["flag_color"], memo=p["memo"])
        self.fake_sw.expenses[1001] = sw_expense(
            "1.00", [(111, "1.00", "0.50"), (222, "0.00", "0.50")], details=HALF_DETAILS)
        self.fake_ynab.delta = [echo]
        self.fake_ynab.knowledge = 300
        self.assertEqual(t.ynab_to_sw(self.cfg()), 0)
        self.assertEqual(self.fake_sw.create_expense.call_count, 1)
        self.fake_sw.update_expense.assert_not_called()
        self.assertEqual(len(self.patches()), 1)
        self.assertEqual(read_knowledge(self.state_dir), 300)

    def test_splitwise_dated_after_from_earliest_candidate(self):
        t = self.make_transfer([
            txn("a", -1000, "purple", date="2026-03-10"),
            txn("b", -1000, "green", date="2025-12-31"),
            txn("c", -1000, "blue", date="2020-01-01"),  # self-pays: not a candidate
        ])
        t.ynab_to_sw(self.cfg())
        self.fake_sw.get_ynab_marker_expense_ids.assert_called_once_with(
            dated_after="2025-12-30T00:00:00Z")

    def test_corrupt_state_fails_safe(self):
        with open(state_path_for_budget(self.state_dir, "budget"), "w") as f:
            f.write("{not json")
        t = self.make_transfer([txn("a", -1000, "purple")], seed=False)
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        self.fake_ynab.get_transactions.assert_not_called()


class TestAssociatedReflag(TransferTestBase):
    """Flagged transaction that already has an expense: update in place."""

    def run_one(self, flag, amount, existing, memo="x [SWEXP:900]", marker_map=None, **cfg):
        t = self.make_transfer([txn("a", amount, flag, memo=memo)], marker_map=marker_map)
        if existing is not None:
            self.fake_sw.expenses[900] = existing
        return t.ynab_to_sw(self.cfg(**cfg))

    def test_reflag_half_to_partner_pays_updates_same_expense(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              details=HALF_DETAILS)
        self.assertEqual(self.run_one("green", -10000, existing), 0)
        self.fake_sw.create_expense.assert_not_called()
        (call,) = self.fake_sw.update_expense.call_args_list
        expense_id, req = call.args
        self.assertEqual(expense_id, 900)
        self.assertEqual([(u["id"], u["paid"], u["owed"]) for u in req["users"]],
                         [(111, "10.00", "0.00"), (222, "0.00", "10.00")])
        self.assertEqual(req["details"], PP_DETAILS)
        self.assertNotIn("group_id", req)
        (p,) = self.patches()
        self.assertEqual(p, {"id": "a", "flag_color": None})  # tag already present

    def test_reflag_identical_split_no_splitwise_write(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_one("purple", -10000, existing), 0)
        self.fake_sw.update_expense.assert_not_called()
        self.fake_sw.create_expense.assert_not_called()
        self.assertEqual(self.patches(), [{"id": "a", "flag_color": None}])

    def test_amount_changed_updates_cost_and_shares(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_one("purple", -12010, existing), 0)
        _, req = self.fake_sw.update_expense.call_args.args
        self.assertEqual(req["cost"], "12.01")
        self.assertEqual([u["owed"] for u in req["users"]], ["6.01", "6.00"])

    def test_marker_only_association_adds_tag_once(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_one("purple", -10000, existing, memo="lunch",
                                      marker_map={"a": 900}), 0)
        self.fake_sw.create_expense.assert_not_called()
        (p,) = self.patches()
        self.assertEqual(p["memo"], "lunch [SWEXP:900]")
        self.assertEqual(p["memo"].count("[SWEXP:"), 1)

    def test_deleted_expense_is_orphaned(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              deleted="2026-01-01T00:00:00Z")
        self.assertEqual(self.run_one("purple", -10000, existing), 0)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_sw.update_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()  # flag left

    def test_missing_expense_404_is_orphaned(self):
        self.assertEqual(self.run_one("purple", -10000, None), 0)
        self.fake_sw.get_expense.assert_called_once_with(900)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_foreign_user_set_is_failure(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (333, "0.00", "5.00")])
        self.assertEqual(self.run_one("purple", -12000, existing), 1)
        self.fake_sw.update_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_update_failure_leaves_flag_and_state(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        t = self.make_transfer([txn("a", -12000, "purple", memo="[SWEXP:900]")])
        self.fake_sw.expenses[900] = existing
        self.fake_sw.update_expense.return_value = (None, MagicMock())
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        self.fake_ynab.update_transactions.assert_not_called()
        self.assertEqual(read_knowledge(self.state_dir), 100)

    def test_dry_run_counts_only(self):
        changed = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_one("purple", -12000, changed, dry_run=True), 0)
        self.fake_sw.update_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_self_pays_on_associated_is_ignored(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_one("blue", -12000, existing), 0)
        self.fake_sw.get_expense.assert_not_called()
        self.fake_sw.update_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()


class TestSyncPass(TransferTestBase):
    """Tagged transaction with no actionable flag, edited later."""

    def run_sync(self, transaction, existing, **cfg):
        t = self.make_transfer([transaction])
        if existing is not None:
            self.fake_sw.expenses[900] = existing
        ret = t.ynab_to_sw(self.cfg(**cfg))
        self.fake_ynab.update_transactions.assert_not_called()  # sync never writes YNAB
        self.fake_sw.create_expense.assert_not_called()
        return ret

    def update_req(self):
        (call,) = self.fake_sw.update_expense.call_args_list
        self.assertEqual(call.args[0], 900)
        return call.args[1]

    def test_amount_edit_half_mode(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              details=HALF_DETAILS)
        self.assertEqual(self.run_sync(txn("a", -20010, None, memo="[SWEXP:900]"), existing), 0)
        req = self.update_req()
        self.assertEqual(req["cost"], "20.01")
        self.assertEqual([u["owed"] for u in req["users"]], ["10.01", "10.00"])
        self.assertEqual(req["details"], HALF_DETAILS)

    def test_amount_edit_partner_pays_mode(self):
        existing = sw_expense("10.00", [(111, "10.00", "0.00"), (222, "0.00", "10.00")],
                              details=PP_DETAILS)
        self.assertEqual(self.run_sync(txn("a", -15000, None, memo="[SWEXP:900]"), existing), 0)
        req = self.update_req()
        self.assertEqual([u["owed"] for u in req["users"]], ["0.00", "15.00"])

    def test_unchanged_echo_makes_no_write(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              details=HALF_DETAILS)
        self.assertEqual(self.run_sync(txn("a", -10000, None, memo="x [SWEXP:900]"), existing), 0)
        self.fake_sw.update_expense.assert_not_called()

    def test_manual_split_survives_unrelated_edit(self):
        existing = sw_expense("10.00", [(111, "10.00", "3.00"), (222, "0.00", "7.00")],
                              details=HALF_DETAILS)
        edited = txn("a", -10000, None, memo="new memo [SWEXP:900]", category_id="other")
        self.assertEqual(self.run_sync(edited, existing), 0)
        self.fake_sw.update_expense.assert_not_called()

    def test_date_change_syncs(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              details=HALF_DETAILS)
        self.run_sync(txn("a", -10000, None, memo="[SWEXP:900]", date=MONTHS_AGO), existing)
        self.assertEqual(self.update_req()["date"], f"{MONTHS_AGO}T12:00:00Z")

    def test_payee_change_syncs(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              details=HALF_DETAILS)
        self.run_sync(txn("a", -10000, None, memo="[SWEXP:900]", payee_name="New"), existing)
        self.assertEqual(self.update_req()["description"], "New")

    def test_mode_inferred_without_split_token(self):
        existing = sw_expense("10.00", [(111, "10.00", "0.00"), (222, "0.00", "10.00")],
                              details="ynab:a")
        self.run_sync(txn("a", -20000, None, memo="[SWEXP:900]"), existing)
        req = self.update_req()
        self.assertEqual([u["owed"] for u in req["users"]], ["0.00", "20.00"])
        self.assertEqual(req["details"], PP_DETAILS)

    def test_later_split_syncs_on_parent_total(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              details=HALF_DETAILS)
        split = txn("a", -30000, None, memo="[SWEXP:900]",
                    subtransactions=[{"amount": -10000}, {"amount": -20000}])
        self.assertEqual(self.run_sync(split, existing), 0)
        self.assertEqual(self.update_req()["cost"], "30.00")

    def test_deleted_ynab_transaction_orphaned(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_sync(txn("a", -10000, None, memo="[SWEXP:900]", deleted=True),
                                       existing), 0)
        self.fake_sw.get_expense.assert_not_called()
        self.fake_sw.update_expense.assert_not_called()
        self.assertEqual(read_knowledge(self.state_dir), 200)

    def test_now_inflow_orphaned(self):
        self.assertEqual(self.run_sync(txn("a", 10000, None, memo="[SWEXP:900]"), None), 0)
        self.fake_sw.update_expense.assert_not_called()

    def test_deleted_expense_orphaned_no_recreate(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")],
                              deleted="2026-01-01T00:00:00Z")
        self.assertEqual(self.run_sync(txn("a", -20000, None, memo="[SWEXP:900]"), existing), 0)
        self.fake_sw.update_expense.assert_not_called()

    def test_foreign_users_failure(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (333, "0.00", "5.00")])
        self.assertEqual(self.run_sync(txn("a", -20000, None, memo="[SWEXP:900]"), existing), 1)
        self.fake_sw.update_expense.assert_not_called()

    def test_failure_does_not_advance_state(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        t = self.make_transfer([txn("a", -20000, None, memo="[SWEXP:900]")])
        self.fake_sw.expenses[900] = existing
        self.fake_sw.update_expense.side_effect = RuntimeError("boom")
        self.assertEqual(t.ynab_to_sw(self.cfg()), 1)
        self.assertEqual(read_knowledge(self.state_dir), 100)

    def test_dry_run_no_write(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_sync(txn("a", -20000, None, memo="[SWEXP:900]"), existing,
                                       dry_run=True), 0)
        self.fake_sw.update_expense.assert_not_called()
        self.assertEqual(read_knowledge(self.state_dir), 100)

    def test_self_pays_flagged_tagged_not_synced(self):
        existing = sw_expense("10.00", [(111, "10.00", "5.00"), (222, "0.00", "5.00")])
        self.assertEqual(self.run_sync(txn("a", -20000, "blue", memo="[SWEXP:900]"), existing), 0)
        self.fake_sw.get_expense.assert_not_called()

    def test_new_expense_details_store_mode(self):
        t = self.make_transfer([txn("a", -1000, "green")])
        t.ynab_to_sw(self.cfg())
        (e,) = self.created_expenses()
        self.assertEqual(e["details"], PP_DETAILS)


USER_A_ID, USER_B_ID = 111, 222


def user_secrets(name, token, partner_id, partner_pays, self_pays, state_dir, **extra):
    d = {
        "USER_NAME": name,
        "SW_CONSUMER_KEY": f"ck-{token}",
        "SW_CONSUMER_SECRET": "cs",
        "SW_API_KEY": f"api-{token}",
        "YNAB_PERSONAL_ACCESS_TOKEN": token,
        "YNAB_BUDGET_NAME": f"budget-{token}",
        "YNAB_TO_SW_ENABLED": "true",
        "SW_PARTNER_USER_ID": str(partner_id),
        "YNAB_TO_SW_FLAG_SPLIT": "purple",
        "YNAB_TO_SW_FLAG_PARTNER_PAYS": partner_pays,
        "YNAB_TO_SW_FLAG_SELF_PAYS": self_pays,
        "YNAB_TO_SW_STATE_DIR": state_dir,
    }
    d.update(extra)
    return d


class TestMultiUser(unittest.TestCase):
    """End-to-end over run_all_users() with two mirrored household configs."""

    KNOWLEDGE = {"A": 500, "B": 900}

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="ynab-to-sw-multi-")
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        self.sw_by_key = {}
        self.ynab_by_token = {}

    def users(self, a_extra=None, b_extra=None):
        return [
            user_secrets("A", "A", USER_B_ID, "green", "blue", self.state_dir, **(a_extra or {})),
            user_secrets("B", "B", USER_A_ID, "blue", "green", self.state_dir, **(b_extra or {})),
        ]

    def run_users(self, users, broken_tokens=()):
        ids = {"A": USER_A_ID, "B": USER_B_ID}

        def sw_factory(consumer_key, consumer_secret, api_key):
            token = consumer_key.split("-", 1)[1]
            if token not in self.sw_by_key:
                self.sw_by_key[token] = make_fake_sw(ids[token], next_expense_id=ids[token] * 10)
            return self.sw_by_key[token]

        def ynab_factory(token):
            if token in broken_tokens:
                raise RuntimeError("cannot reach YNAB")
            if token not in self.ynab_by_token:
                self.ynab_by_token[token] = make_fake_ynab(
                    [
                        txn(f"{token}-purple", -100010, "purple"),
                        txn(f"{token}-blue", -40000, "blue"),
                        txn(f"{token}-green", -60000, "green"),
                    ],
                    baseline=[txn(f"{token}-old", -5000, "purple", date=MONTHS_AGO)],
                    knowledge=self.KNOWLEDGE[token],
                    budget_id=f"budget-{token}",
                )
            return self.ynab_by_token[token]

        env = {
            "multi_user_secrets_json": json.dumps(users),
            "ynab_account_name": "Splitwise",
            "sync_ynab_to_sw": "true",
        }
        with patch.dict(os.environ, env), patch("main.SW", side_effect=sw_factory), patch(
            "main.YNABClient", side_effect=ynab_factory
        ):
            return run_all_users()

    def results(self, token):
        """Map YNAB txn id -> (payer ids, {user id: owed}) for created expenses."""
        out = {}
        for c in self.sw_by_key[token].create_expense.call_args_list:
            e = c.args[0]
            (tid,) = extract_ynab_markers(e["details"])
            payer = [u["id"] for u in e["users"] if u["paid"] != "0.00"]
            out[tid] = (payer, {u["id"]: u["owed"] for u in e["users"]})
        return out

    def patched_ids(self, token):
        ids = []
        for c in self.ynab_by_token[token].update_transactions.call_args_list:
            ids.extend(p["id"] for p in c.args[1])
        return ids

    def baseline_then_run(self, users, **kw):
        self.assertEqual(self.run_users(users), 0)  # baseline only
        for fake in self.sw_by_key.values():
            fake.create_expense.assert_not_called()
        return self.run_users(users, **kw)

    def test_mirrored_household_flags(self):
        users = self.users()
        self.assertEqual(self.baseline_then_run(users), 0)

        a = self.results("A")
        self.assertEqual(set(a), {"A-purple", "A-green"})  # blue ignored for A
        self.assertEqual(a["A-purple"], ([USER_A_ID], {USER_A_ID: "50.01", USER_B_ID: "50.00"}))
        self.assertEqual(a["A-green"], ([USER_A_ID], {USER_A_ID: "0.00", USER_B_ID: "60.00"}))
        self.assertEqual(sorted(self.patched_ids("A")), ["A-green", "A-purple"])

        b = self.results("B")
        self.assertEqual(set(b), {"B-purple", "B-blue"})  # green ignored for B
        self.assertEqual(b["B-purple"], ([USER_B_ID], {USER_B_ID: "50.01", USER_A_ID: "50.00"}))
        self.assertEqual(b["B-blue"], ([USER_B_ID], {USER_B_ID: "0.00", USER_A_ID: "40.00"}))
        self.assertEqual(sorted(self.patched_ids("B")), ["B-blue", "B-purple"])

    def test_per_budget_state_isolation(self):
        users = self.users()
        self.assertEqual(self.run_users(users), 0)
        files = sorted(os.listdir(self.state_dir))
        self.assertEqual(files, ["ynab_to_sw_budget-A.json", "ynab_to_sw_budget-B.json"])
        self.assertEqual(read_knowledge(self.state_dir, "budget-A"), 500)
        self.assertEqual(read_knowledge(self.state_dir, "budget-B"), 900)
        self.assertEqual(
            load_state(state_path_for_budget(self.state_dir, "budget-A"))["preexisting_flags"],
            {"A-old": "purple"})
        self.assertEqual(self.run_users(users), 0)
        self.ynab_by_token["A"].get_transactions.assert_called_with(
            "budget-A", last_knowledge_of_server=500)
        self.ynab_by_token["B"].get_transactions.assert_called_with(
            "budget-B", last_knowledge_of_server=900)

    def test_misconfigured_user_does_not_block_other(self):
        users = self.users()
        self.run_users(users)  # baseline both
        del users[0]["SW_PARTNER_USER_ID"]
        self.assertEqual(self.run_users(users), 1)
        self.sw_by_key["A"].create_expense.assert_not_called()
        self.ynab_by_token["A"].update_transactions.assert_not_called()
        self.assertEqual(set(self.results("B")), {"B-purple", "B-blue"})

    def test_crashing_user_does_not_block_other(self):
        users = self.users()
        self.assertEqual(self.baseline_then_run(users, broken_tokens={"A"}), 1)
        self.assertEqual(set(self.results("B")), {"B-purple", "B-blue"})

    def test_unwritable_state_dir_only_affects_that_user(self):
        locked = os.path.join(self.state_dir, "locked")
        os.makedirs(locked)
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        if os.access(locked, os.W_OK):
            self.skipTest("running as a user that ignores permissions")
        users = self.users(a_extra={"YNAB_TO_SW_STATE_DIR": locked})
        self.assertEqual(self.baseline_then_run_allowing_failure(users), 1)
        self.sw_by_key["A"].create_expense.assert_not_called()
        self.ynab_by_token["A"].update_transactions.assert_not_called()
        self.assertEqual(set(self.results("B")), {"B-purple", "B-blue"})

    def baseline_then_run_allowing_failure(self, users):
        self.run_users(users)
        return self.run_users(users)

    def test_disabled_user_untouched(self):
        users = self.users(a_extra={"YNAB_TO_SW_ENABLED": "false"})
        self.assertEqual(self.baseline_then_run(users), 0)
        self.sw_by_key["A"].create_expense.assert_not_called()
        self.sw_by_key["A"].get_ynab_marker_expense_ids.assert_not_called()
        self.assertFalse(os.path.exists(state_path_for_budget(self.state_dir, "budget-A")))
        self.assertEqual(len(self.results("B")), 2)


class TestRunGates(unittest.TestCase):
    BASE = {
        "sw_consumer_key": "k", "sw_consumer_secret": "s", "sw_api_key": "a",
        "ynab_budget_name": "b", "ynab_personal_access_token": "t",
    }

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="ynab-to-sw-gates-")
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        seed_state(self.state_dir)
        self.enabled = dict(self.BASE, ynab_to_sw_state_dir=self.state_dir,
                            **{k.lower(): v for k, v in TestConfig.GOOD.items()})

    def _run(self, secrets, global_gate="true", fake_sw=None):
        fake_sw = fake_sw or make_fake_sw(111)
        fake_ynab = make_fake_ynab([txn("a", -1000, "purple")])
        env = {"ynab_account_name": "Splitwise", "sync_ynab_to_sw": global_gate}
        with patch.dict(os.environ, env), patch("main.SW", return_value=fake_sw), patch(
            "main.YNABClient", return_value=fake_ynab
        ):
            ret = run_for_secrets_dict(secrets)
        return ret, fake_sw, fake_ynab

    def test_disabled_by_default(self):
        ret, fake_sw, fake_ynab = self._run(dict(self.BASE))
        self.assertEqual(ret, 0)
        fake_sw.create_expense.assert_not_called()
        fake_ynab.update_transactions.assert_not_called()

    def test_global_gate_off(self):
        ret, fake_sw, _ = self._run(self.enabled, global_gate="false")
        self.assertEqual(ret, 0)
        fake_sw.create_expense.assert_not_called()

    def test_enabled_runs(self):
        ret, fake_sw, _ = self._run(self.enabled)
        self.assertEqual(ret, 0)
        fake_sw.create_expense.assert_called_once()

    def test_invalid_config_nonzero_no_writes(self):
        secrets = dict(self.BASE, ynab_to_sw_enabled="true", ynab_to_sw_state_dir=self.state_dir)
        ret, fake_sw, fake_ynab = self._run(secrets)
        self.assertEqual(ret, 1)
        fake_sw.create_expense.assert_not_called()
        fake_sw.get_ynab_marker_expense_ids.assert_not_called()
        fake_ynab.update_transactions.assert_not_called()

    def test_state_dir_not_a_directory_is_config_error(self):
        not_dir = os.path.join(self.state_dir, "file")
        open(not_dir, "w").close()
        ret, fake_sw, fake_ynab = self._run(dict(self.enabled, ynab_to_sw_state_dir=not_dir))
        self.assertEqual(ret, 1)
        fake_sw.create_expense.assert_not_called()
        fake_ynab.update_transactions.assert_not_called()

    def test_ynab_to_sw_failure_propagates(self):
        fake_sw = make_fake_sw(111)
        fake_sw.create_expense.side_effect = RuntimeError("boom")
        ret, _, _ = self._run(self.enabled, fake_sw=fake_sw)
        self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
