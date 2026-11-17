"""Tests for the flag-driven YNAB -> Splitwise path. Mocks only: no real API calls."""
import json
import os
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import main
from main import (
    YnabToSwConfig,
    build_splitwise_expense,
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
    return sw


def make_fake_ynab(transactions):
    ynab = MagicMock(name="YNAB")
    ynab.get_budget_id.return_value = "budget"
    ynab.get_account_id.return_value = SW_ACCOUNT_ID
    ynab.get_transactions.return_value = {"data": {"transactions": transactions}}
    ynab.get_scheduled_transactions.return_value = {"data": {"scheduled_transactions": []}}
    return ynab


def config(**kw):
    base = dict(
        enabled=True,
        partner_user_id=222,
        flag_split="purple",
        flag_partner_pays="green",
        flag_self_pays="blue",
        lookback_days=14,
        dry_run=False,
    )
    base.update(kw)
    return YnabToSwConfig(**base)


class TransferTestBase(unittest.TestCase):
    def make_transfer(self, transactions, marker_map=None, current_user_id=111):
        self.fake_sw = make_fake_sw(current_user_id, marker_map)
        self.fake_ynab = make_fake_ynab(transactions)
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
             cfg.lookback_days, cfg.dry_run),
            (222, "purple", "green", "blue", 14, False),
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

    def test_invalid_colour_and_lookback(self):
        d = dict(self.GOOD, ynab_to_sw_flag_split="pink", ynab_to_sw_lookback_days="0")
        _, errors = parse_ynab_to_sw_config(d)
        self.assertEqual(len(errors), 2)


class TestYnabToSw(TransferTestBase):
    def test_split_even_and_partner_pays(self):
        t = self.make_transfer([txn("a", -200000, "purple"), txn("b", -75000, "green")])
        self.assertEqual(t.ynab_to_sw(config()), 0)
        split, partner_pays = self.created_expenses()
        self.assertEqual([u["owed"] for u in split["users"]], ["100.00", "100.00"])
        self.assertEqual(split["users"][0]["paid"], "200.00")
        self.assertEqual([u["owed"] for u in partner_pays["users"]], ["0.00", "75.00"])
        self.assertEqual(partner_pays["users"][1]["id"], 222)
        self.assertEqual(len(self.patches()), 2)

    def test_split_odd_cents(self):
        t = self.make_transfer([txn("a", -10010, "purple")])
        self.assertEqual(t.ynab_to_sw(config()), 0)
        (e,) = self.created_expenses()
        self.assertEqual([u["owed"] for u in e["users"]], ["5.01", "5.00"])

    def test_self_pays_ignored_flag_left(self):
        t = self.make_transfer([txn("a", -1000, "blue")])
        self.assertEqual(t.ynab_to_sw(config()), 0)
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
            txn("tagged", -5000, "purple", memo="note [SWEXP:42]"),
            txn("swid", -5000, "purple", memo="x [SWID:42-abcd]"),
            txn("unflagged", -5000, None),
            txn("red", -5000, "red"),
        ]
        t = self.make_transfer(transactions)
        self.assertEqual(t.ynab_to_sw(config()), 0)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_single_budget_wide_fetch(self):
        t = self.make_transfer([])
        t.ynab_to_sw(config(lookback_days=7))
        self.fake_ynab.get_transactions.assert_called_once_with(
            "budget", since_date=(date.today() - timedelta(days=7)).isoformat()
        )

    def test_idempotent_reuse(self):
        t = self.make_transfer([txn("a", -1000, "purple")], marker_map={"a": 777})
        self.assertEqual(t.ynab_to_sw(config()), 0)
        self.fake_sw.create_expense.assert_not_called()
        (p,) = self.patches()
        self.assertEqual(extract_swexp_from_memo(p["memo"]), 777)

    def test_patch_payload_only_memo_and_flag(self):
        t = self.make_transfer([txn("a", -1000, "purple", memo="groceries")])
        self.assertEqual(t.ynab_to_sw(config()), 0)
        (p,) = self.patches()
        self.assertEqual(set(p), {"id", "memo", "flag_color"})
        self.assertIsNone(p["flag_color"])
        self.assertEqual(p["id"], "a")
        self.assertEqual(p["memo"], "groceries [SWEXP:1001]")

    def test_long_memo_truncated_in_patch(self):
        t = self.make_transfer([txn("a", -1000, "purple", memo="m" * 400)])
        t.ynab_to_sw(config())
        (p,) = self.patches()
        self.assertLessEqual(len(p["memo"]), YNAB_MEMO_MAX_LENGTH)
        self.assertTrue(p["memo"].endswith("[SWEXP:1001]"))

    def test_splitwise_failure_leaves_flag_and_continues(self):
        t = self.make_transfer([txn("a", -1000, "purple"), txn("b", -2000, "purple")])
        ok = MagicMock(); ok.getId.return_value = 55
        self.fake_sw.create_expense.side_effect = [RuntimeError("boom"), (ok, None)]
        self.assertEqual(t.ynab_to_sw(config()), 1)
        patched_ids = [p["id"] for p in self.patches()]
        self.assertEqual(patched_ids, ["b"])  # "a" not patched: flag stays

    def test_splitwise_errors_object_is_failure(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.fake_sw.create_expense.side_effect = None
        self.fake_sw.create_expense.return_value = (None, MagicMock())
        self.assertEqual(t.ynab_to_sw(config()), 1)
        self.fake_ynab.update_transactions.assert_not_called()

    def test_ynab_patch_failure_returns_nonzero(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.fake_ynab.update_transactions.side_effect = RuntimeError("http")
        self.assertEqual(t.ynab_to_sw(config()), 1)

    def test_marker_read_failure_no_writes(self):
        t = self.make_transfer([txn("a", -1000, "purple")])
        self.fake_sw.get_ynab_marker_expense_ids.side_effect = RuntimeError("down")
        self.assertEqual(t.ynab_to_sw(config()), 1)
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_partner_is_self_aborts(self):
        t = self.make_transfer([txn("a", -1000, "purple")], current_user_id=222)
        self.assertEqual(t.ynab_to_sw(config(partner_user_id=222)), 1)
        self.fake_sw.create_expense.assert_not_called()

    def test_dry_run_writes_nothing(self):
        t = self.make_transfer(
            [txn("a", -1000, "purple"), txn("b", -1000, "green")], marker_map={"b": 9}
        )
        self.assertEqual(t.ynab_to_sw(config(dry_run=True)), 0)
        self.fake_sw.get_ynab_marker_expense_ids.assert_called_once()
        self.fake_sw.create_expense.assert_not_called()
        self.fake_ynab.update_transactions.assert_not_called()

    def test_logs_have_no_payee_amount_or_memo(self):
        t = self.make_transfer([txn("a", -12345, "purple", memo="secret memo")])
        with self.assertLogs("main", level="INFO") as cm:
            t.ynab_to_sw(config())
        text = "\n".join(cm.output)
        for needle in ("Payee a", "secret memo", "12345", "123.45", "61.72"):
            self.assertNotIn(needle, text)


USER_A_ID, USER_B_ID = 111, 222


def user_secrets(name, token, partner_id, partner_pays, self_pays, **extra):
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
    }
    d.update(extra)
    return d


class TestMultiUser(unittest.TestCase):
    """End-to-end over run_all_users() with two mirrored household configs."""

    def run_users(self, users, broken_tokens=()):
        self.sw_by_key = {}
        self.ynab_by_token = {}
        ids = {"A": USER_A_ID, "B": USER_B_ID, "C": 333}

        def sw_factory(consumer_key, consumer_secret, api_key):
            token = consumer_key.split("-", 1)[1]
            fake = make_fake_sw(ids[token], next_expense_id=ids[token] * 10)
            self.sw_by_key[token] = fake
            return fake

        def ynab_factory(token):
            if token in broken_tokens:
                raise RuntimeError("cannot reach YNAB")
            fake = make_fake_ynab([
                txn(f"{token}-purple", -100010, "purple"),
                txn(f"{token}-blue", -40000, "blue"),
                txn(f"{token}-green", -60000, "green"),
            ])
            self.ynab_by_token[token] = fake
            return fake

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
        """Map YNAB txn id -> (payer id, {user id: owed}) for created expenses."""
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

    def test_mirrored_household_flags(self):
        users = [
            user_secrets("A", "A", USER_B_ID, partner_pays="green", self_pays="blue"),
            user_secrets("B", "B", USER_A_ID, partner_pays="blue", self_pays="green"),
        ]
        self.assertEqual(self.run_users(users), 0)

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

    def test_misconfigured_user_does_not_block_other(self):
        bad = user_secrets("A", "A", USER_B_ID, partner_pays="green", self_pays="blue")
        del bad["SW_PARTNER_USER_ID"]
        users = [bad, user_secrets("B", "B", USER_A_ID, partner_pays="blue", self_pays="green")]
        self.assertEqual(self.run_users(users), 1)
        self.sw_by_key["A"].create_expense.assert_not_called()
        self.ynab_by_token["A"].update_transactions.assert_not_called()
        self.assertEqual(set(self.results("B")), {"B-purple", "B-blue"})

    def test_crashing_user_does_not_block_other(self):
        users = [
            user_secrets("A", "A", USER_B_ID, partner_pays="green", self_pays="blue"),
            user_secrets("B", "B", USER_A_ID, partner_pays="blue", self_pays="green"),
        ]
        self.assertEqual(self.run_users(users, broken_tokens={"A"}), 1)
        self.assertEqual(set(self.results("B")), {"B-purple", "B-blue"})

    def test_disabled_user_untouched(self):
        users = [
            user_secrets("A", "A", USER_B_ID, partner_pays="green", self_pays="blue",
                         YNAB_TO_SW_ENABLED="false"),
            user_secrets("B", "B", USER_A_ID, partner_pays="blue", self_pays="green"),
        ]
        self.assertEqual(self.run_users(users), 0)
        self.sw_by_key["A"].create_expense.assert_not_called()
        self.sw_by_key["A"].get_ynab_marker_expense_ids.assert_not_called()
        self.assertEqual(len(self.results("B")), 2)


class TestRunGates(unittest.TestCase):
    BASE = {
        "sw_consumer_key": "k", "sw_consumer_secret": "s", "sw_api_key": "a",
        "ynab_budget_name": "b", "ynab_personal_access_token": "t",
    }

    def _run(self, secrets, global_gate="true"):
        fake_sw = make_fake_sw(111)
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
        secrets = dict(self.BASE, **{k.lower(): v for k, v in TestConfig.GOOD.items()})
        ret, fake_sw, _ = self._run(secrets, global_gate="false")
        self.assertEqual(ret, 0)
        fake_sw.create_expense.assert_not_called()

    def test_invalid_config_nonzero_no_writes(self):
        secrets = dict(self.BASE, ynab_to_sw_enabled="true")
        ret, fake_sw, fake_ynab = self._run(secrets)
        self.assertEqual(ret, 1)
        fake_sw.create_expense.assert_not_called()
        fake_sw.get_ynab_marker_expense_ids.assert_not_called()
        fake_ynab.update_transactions.assert_not_called()

    def test_ynab_to_sw_failure_propagates(self):
        secrets = dict(self.BASE, **{k.lower(): v for k, v in TestConfig.GOOD.items()})
        fake_sw = make_fake_sw(111)
        fake_sw.create_expense.side_effect = RuntimeError("boom")
        fake_ynab = make_fake_ynab([txn("a", -1000, "purple")])
        env = {"ynab_account_name": "Splitwise", "sync_ynab_to_sw": "true"}
        with patch.dict(os.environ, env), patch("main.SW", return_value=fake_sw), patch(
            "main.YNABClient", return_value=fake_ynab
        ):
            self.assertEqual(run_for_secrets_dict(secrets), 1)


if __name__ == "__main__":
    unittest.main()
