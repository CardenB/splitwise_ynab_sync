# splitwise_ynab_sync

## What does it do?
The code automates the process of importing transactions from Splitwise into your YNAB budget.By following instructions below, you can automate to run it daily using Github Actions. So, your Splitwise transactions will be imported everyday just like your other automated accounts.

## What change do you need in your YNAB workflow to implement this?
You would need to create a new account named 'Splitwise' in your YNAB accounts(mentioned below in Setup #1).
That is the only necessity for this repo.

That said, I would to mention my workflow:
- In addition to a 'Splitwise' account, I also create a 'Splitwise' category. I place this in a 'Don't count' group as it helps in the reports.
- Expenses paid by me: I split the expense between a category corrsponding to the expense and the 'Splitwise' category.
- Expenses paid by others: I add my share as an expense under 'Splitwise' account and the corresponding category.

## Which transactions are imported?
The code imports all the **transactions for which you owe money**.

## Is it free?
Yes. Since you will be deploying your own Github Actions to deploy, you will be using just around 15 minutes from the [free 2000 minutes per month](https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions#included-storage-and-minutes).

## Setup
This repo moves transactions from Splitwise to YNAB.

1. Go to your YNAB budget ([YNAB](https://app.youneedabudget.com/)) and create a new account named `Splitwise`. This is where the imported transactions will flow into.
2. Collect Credentials from YNAB and Splitwise:

    a. YNAB:
     - Go to [YNAB Developer Settings](https://app.ynab.com/settings/developer)
     - Create a new `Personal Access Token`.
     - You will see the token at the top of page, save that in a safe place as you won't be able to access it again.
    
    b. Splitwise:
    - Go to [Splitwise Apps](https://secure.splitwise.com/apps)
    - Click on `Register your application`
    - Fill `application name` (YNAB_Splitwise_sync), `description` and `Homepage URL` (http://api-example.splitwise.com/) and click on `Register and API key`
    - Copy `Consumer Key`, `Consumer Secret` and `API keys`.
3. Fork this repo. (by clicking on the 'Fork' option on the top of this page.)
4. Add the Credentials on Github Actions:
    - Go to the `Settings` tab, then `Secrets and variables` > `Actions`
    - Under `Secrets` tab, using `New repository secret`, you need to add 4 Name-Secret pairs:
        - Name: `YNAB_PERSONAL_ACCESS_TOKEN`, Secret: `Personal Access Token` from 2a.
        - Name: `SW_API_KEY`, Secret: `API keys` from 2b.
        - Name: `SW_CONSUMER_KEY`, Secret: `Consumer Key` from 2b.
        - Name: `SW_CONSUMER_SECRET`, Secret: `Consumer Secret` from 2b.
        - Name: `YNAB_BUDGET_NAME`, Value: your YNAB budget name (check your YNAB app or website, if you don't know, fill 'My Budget')
    - Similarly, Under `Variables` tab, using `New repository variable`, add:
        - Name: `YNAB_ACCOUNT_NAME`, Value: 'Splitwise' (created in step 1).


### NOTE: Support for multiple users
To support multiple users, you can populate key `MULTI_USER_SECRETS_JSON` with a JSON string containing the secrets of all users. The JSON string should be in the following format:

```json
[
    {
        "SW_API_KEY": "splitwise_api_key",
        "SW_CONSUMER_KEY": "splitwise_consumer_key",
        "SW_CONSUMER_SECRET": "splitwise_consumer_secret_key",
        "YNAB_PERSONAL_ACCESS_TOKEN": "ynab_personal_access_token",
        "USER_NAME": "user_name" # This one doesn't matter, just for your reference. Must be non-empty to work.
        "YNAB_BUDGET_NAME": "My Budget"
    },
    {
        "SW_API_KEY": "splitwise_api_key",
        "SW_CONSUMER_KEY": "splitwise_consumer_key",
        "SW_CONSUMER_SECRET": "splitwise_consumer_secret_key",
        "YNAB_PERSONAL_ACCESS_TOKEN": "ynab_personal_access_token",
        "USER_NAME": "user_name" # This one doesn't matter, just for your reference. Must be non-empty to work.
        "YNAB_BUDGET_NAME": "My Budget"
    }
]
```

### YNAB -> Splitwise (flag-driven, opt-in)

Each user can push their own shared purchases from YNAB to Splitwise by
flagging the transaction in **their own budget**. Disabled by default; it
runs only when the global `sync_ynab_to_sw` env var is `true` (its default)
**and** that user's `YNAB_TO_SW_ENABLED` is `true`.

Three flag meanings, configured per user:

| Flag key | Meaning | What happens |
| --- | --- | --- |
| `YNAB_TO_SW_FLAG_SPLIT` (default `purple`) | 50/50 | You paid in full; partner owes half. An odd cent is owed by you (partner owes the floor half). |
| `YNAB_TO_SW_FLAG_PARTNER_PAYS` (required) | partner pays | You paid in full; partner owes 100%. |
| `YNAB_TO_SW_FLAG_SELF_PAYS` (required) | you pay | Nothing. The transaction is ignored and the flag is left in place. |

Each run (hourly), per enabled user:

1. One budget-wide transactions read covering the last `YNAB_TO_SW_LOOKBACK_DAYS`.
   Only outflows with an actionable flag are considered; inflows, transfers,
   split (sub)transactions, deleted rows, rows in the Splitwise account, and
   rows whose memo already has a `[SWEXP:<id>]` or `[SWID:...]` tag are skipped.
2. Splitwise expenses for the window are read once; any expense whose details
   contain `ynab:<transaction id>` is reused instead of creating a duplicate.
3. Otherwise a Splitwise expense is created between you and `SW_PARTNER_USER_ID`:
   description = payee, date = the YNAB transaction date, details =
   `ynab:<transaction id>`.
4. The YNAB transaction is then updated with **only** its memo (original memo +
   ` [SWEXP:<expense id>]`, the original part truncated if YNAB's memo limit
   would be exceeded) and a cleared flag. Category, amount and splits are untouched.
5. If anything fails for a transaction its flag stays, the other transactions
   still run, and the job exits non-zero. A failure or bad config for one user
   never stops the other users from running.

| Key | Default | Notes |
| --- | --- | --- |
| `YNAB_TO_SW_ENABLED` | `false` | Opt-in per user. |
| `SW_PARTNER_USER_ID` | - | Numeric Splitwise user id of your partner. Required when enabled. |
| `YNAB_TO_SW_FLAG_SPLIT` | `purple` | |
| `YNAB_TO_SW_FLAG_PARTNER_PAYS` | - | Required when enabled. |
| `YNAB_TO_SW_FLAG_SELF_PAYS` | - | Required when enabled. |
| `YNAB_TO_SW_LOOKBACK_DAYS` | `14` | |
| `YNAB_TO_SW_DRY_RUN` | `false` | Reads and plans only; logs counts, writes nothing. |

Colours are YNAB's: `red`, `orange`, `yellow`, `green`, `blue`, `purple`; the
three must differ. Enabled with a missing/invalid key, the YNAB -> Splitwise
pass is skipped for that user (no writes) and the run exits non-zero.

Two-person household example (purple = 50/50, blue = "Carden pays",
green = "Pauline pays"). The same colour means opposite things in the two
budgets, because each person flags only their own transactions. Add these keys
to each person's entry in `MULTI_USER_SECRETS_JSON` (existing keys omitted):

```json
[
    {
        "USER_NAME": "carden",
        "YNAB_TO_SW_ENABLED": "true",
        "SW_PARTNER_USER_ID": "<PAULINE_SPLITWISE_USER_ID>",
        "YNAB_TO_SW_FLAG_SPLIT": "purple",
        "YNAB_TO_SW_FLAG_PARTNER_PAYS": "green",
        "YNAB_TO_SW_FLAG_SELF_PAYS": "blue",
        "YNAB_TO_SW_LOOKBACK_DAYS": "14",
        "YNAB_TO_SW_DRY_RUN": "true"
    },
    {
        "USER_NAME": "pauline",
        "YNAB_TO_SW_ENABLED": "true",
        "SW_PARTNER_USER_ID": "<CARDEN_SPLITWISE_USER_ID>",
        "YNAB_TO_SW_FLAG_SPLIT": "purple",
        "YNAB_TO_SW_FLAG_PARTNER_PAYS": "blue",
        "YNAB_TO_SW_FLAG_SELF_PAYS": "green",
        "YNAB_TO_SW_LOOKBACK_DAYS": "14",
        "YNAB_TO_SW_DRY_RUN": "true"
    }
]
```

Finding your partner's Splitwise user id: on splitwise.com open your partner
under Friends; the number at the end of the URL (`.../#/friends/<id>`) is it.
Start with `YNAB_TO_SW_DRY_RUN` = `true`, check the logged counts, then set it
to `false`.

In single-user mode the same keys are read from the environment.

### Cron

The tool will iterate through these values and prefer these to the other secrets that are defined.

The Github Actions now triggers this code repo at `12:00 UTC` everyday and transfers previous day's transactions from Splitwise to YNAB.

If you would like to change the schedule time, change the cron expression in [python-app.yaml](.github/workflows/python-app.yml) file.


## Bugfixes
1. Apr 6, 2024: fixed the `UnboundLocalError: local variable 'paid' referenced before assignment` error.

### How to keep your repo updated to this repo?
1. On your forked repo, you would see something like `This branch is X commits behind amit-hm/splitwise_yanb_sync:main`.
2. Click on `Sync fork`.
3. Click on `Update branch`.

   This should update your forked repo to this repo and hence bring in those new bugfixes or features.

# NOTE
Github Actions, unfortunately, deactivates after 60 days of inactivity. So, you might have to manually enable the workflow again every 60 days.


Contact me at cardenbag@gmail.com
