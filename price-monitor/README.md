# Price Monitor

Checks product page URLs once a day and emails you when the price drops
below a threshold you set (either a percent-off or an exact target price).

It's a plain script, not a always-on process — you run it on a schedule
(cron on your own machine, or a free GitHub Actions workflow) and it wakes
up, checks prices, sends emails if needed, and exits.

## Files

| File | Purpose |
|---|---|
| `price_monitor.py` | The script. Run this. |
| `config.example.json` | Template — copy to `config.json` and fill in your products/email. |
| `requirements.txt` | Python dependencies (`pip install -r requirements.txt`). |
| `state.json` | Auto-created. Remembers each product's baseline price and last alert so you don't get emailed every single day once a price is already low. |
| `.github/workflows/price-check.yml` | Optional: runs the script daily on GitHub's free infrastructure instead of your own computer. |

## 1. Set up the config

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "email": {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "your.email@gmail.com",
    "sender_password": "your-16-char-app-password",
    "recipient_email": "your.email@gmail.com"
  },
  "products": [
    {
      "name": "Maeve Pleated Utility Trousers",
      "url": "https://www.anthropologie.com/shop/maeve-pleated-utility-trousers?color=224",
      "drop_percent": 20
    }
  ]
}
```

Each product needs a `url` and **one** of:
- `"drop_percent": 20` — alert once the price is 20% (or more) below its baseline. The baseline is the price the script saw the *first* time it successfully checked the page, saved into `state.json`. You can also set `"baseline_price": 148.00` explicitly if you already know the "full" price and don't want to wait for a first run to set it.
- `"target_price": 45.00` — alert once the price is at or below this exact number.

`name` is optional — if you leave it out, the script uses the page's title.

### Gmail setup (if using Gmail to send)

Gmail won't accept your normal password for this. You need an **App Password**:
1. Turn on 2-Step Verification on the Google account, if it isn't already on.
2. In the account's security settings, find "App Passwords," create one (any label is fine), and use the 16-character code it gives you as `sender_password`.

Any other SMTP provider (Outlook, Fastmail, a work email, etc.) works too — just change `smtp_server`/`smtp_port` to match theirs.

## 1b. Add products from the command line (instead of hand-editing config.json)

You don't have to edit `config.json` by hand — you can add (and remove) tracked
products from the command line:

```bash
# Give it a URL and a target price directly:
python3 price_monitor.py add "https://www.anthropologie.com/shop/some-item" --target-price 45

# Or a percent-off-baseline instead of a fixed price:
python3 price_monitor.py add "https://www.anthropologie.com/shop/some-item" --drop-percent 20 --name "Some Item"

# Or just run it with no arguments and answer the prompts:
python3 price_monitor.py add
```

`add` immediately fetches the page, records the current price as the baseline
(unless you passed `--baseline-price`), and — if the item already qualifies
for an alert right then — emails you right away instead of waiting for the
next scheduled run.

Running `add` again with a URL you're already tracking updates its threshold
instead of creating a duplicate entry.

Other commands:

```bash
python3 price_monitor.py list              # see everything you're tracking and its last known price
python3 price_monitor.py remove "<url>"    # stop tracking a product
python3 price_monitor.py check             # run a check right now (this is also what cron/GitHub Actions calls)
```

If you don't specify a command at all (e.g. just `python3 price_monitor.py`), it behaves exactly like `check` — so your cron line or GitHub Actions workflow doesn't need to change.

## 2. Try it once by hand

```bash
pip install -r requirements.txt
python3 price_monitor.py
```

You should see one line per product with its current price, baseline, and threshold. If it emails you a test alert you don't want yet, just adjust `drop_percent`/`target_price` and re-run.

## 3. Run it daily

### Option A: cron (your own computer or a server)

```bash
crontab -e
```

Add a line (runs daily at 8am, adjust the path):

```
0 8 * * * cd /full/path/to/this/folder && /usr/bin/python3 price_monitor.py >> price_monitor.log 2>&1
```

Note: this only fires if the machine is on at 8am. For something that runs whether or not your laptop is open, use Option B.

### Option B: GitHub Actions (free, runs in the cloud)

This is set up to live inside your `toy-projects` repo as a `price-monitor/` subfolder, with the workflow file at the repo root (GitHub only reads workflows from `.github/workflows/` at the top level, regardless of which subfolder the project itself lives in).

1. Clone the repo and copy these files in:

   ```bash
   git clone https://github.com/rebeccarqli/toy-projects.git
   cd toy-projects
   # copy this whole folder's contents in - you should end up with:
   #   toy-projects/price-monitor/price_monitor.py, config.example.json, requirements.txt, README.md
   #   toy-projects/.github/workflows/price-monitor-check.yml
   git add .
   git commit -m "Add price monitor project"
   git push
   ```

2. Inside `toy-projects/price-monitor/`, copy `config.example.json` to `config.json`, fill in your products, and commit that too:

   ```bash
   cd price-monitor
   cp config.example.json config.json
   # edit config.json: add your product(s), leave the "email" section as placeholders
   git add config.json
   git commit -m "Add tracked products"
   git push
   ```

   Since this repo is **public**, leave real email credentials out of `config.json` — the workflow pulls those from GitHub Secrets instead (next step), which override the config file automatically.

3. In the repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**, and add:
   - `SMTP_SERVER` (e.g. `smtp.gmail.com`)
   - `SMTP_PORT` (e.g. `587`)
   - `SENDER_EMAIL`
   - `SENDER_PASSWORD` (the app password)
   - `RECIPIENT_EMAIL`

4. The workflow runs at 13:00 UTC daily — edit the `cron:` line in `.github/workflows/price-monitor-check.yml` for a different time ([crontab.guru](https://crontab.guru) helps translate).

5. Go to the repo's **Actions** tab and click "Run workflow" once to test it before waiting for the schedule.

The workflow commits the updated `price-monitor/state.json` back to the repo after each run, so it remembers baselines and past alerts between days.

## Limitations to know about

- **Bot protection / JavaScript-rendered prices**: this script does a plain HTTP request, not a full browser. It works well on sites that expose price data in page metadata (Open Graph tags, JSON-LD, microdata) — which covers most retail sites, including Anthropologie. Some sites actively block scripted requests or only load price via JavaScript; if a product's checks fail repeatedly, the script emails you a "this check is broken" alert (rather than failing silently) after 3 failed attempts in a row, so you'll know it needs a look.
- **One check per run**: this isn't a live/real-time monitor — the price could change and change back between daily checks and you'd never see it. Increase the cron/workflow frequency if you want tighter coverage (be reasonable about how often you hit someone else's site).
- **Not legal advice**: some sites' terms of service restrict automated scraping. This script is a personal convenience tool checking a page you'd otherwise visit yourself once a day; use your judgment about the sites you point it at.
