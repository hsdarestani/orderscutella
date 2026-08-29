# OrdersCutella

Telegram operations bot for Cutella Shop (`https://cutellashop.ir`), adapted from the working OrdersVesta architecture.

## Features

- WooCommerce connection through the official REST API
- Create simple and variable products from Telegram
- Product price, sale price, stock, weight, categories and description
- Upload multiple product images through WordPress Application Password authentication
- List recent WooCommerce products
- Shopino connection using Shop ID + browser `sessionid`
- Import postal tracking files in XLSX / XLSM / CSV
- Persian/Arabic text normalization and fuzzy recipient matching
- Write tracking codes to Shopino
- Write tracking codes to Cutella WooCommerce orders
- Never automatically overwrite an existing different tracking code
- First Telegram account to run `/start` becomes owner; owner can grant access with `/allow TELEGRAM_ID`

## Server isolation

OrdersCutella is designed to run on the same VPS as OrdersVesta without sharing runtime state:

- app directory: `/opt/orderscutella`
- Docker project: `orderscutella`
- container: `orderscutella-bot`
- health port: `127.0.0.1:8095`
- persistent SQLite/data: `/opt/orderscutella/data`
- optional domain: `orderscutella.smarbiz.sbs`

OrdersVesta remains on its own directory/port/container.

## GitHub Secrets

The repository needs these Actions secrets:

- `BOTTOKEN` — Cutella Telegram bot token
- `HOST` — same VPS host used by OrdersVesta
- `PASS` — same VPS root password used by OrdersVesta

Secrets are repository-scoped, so `HOST` and `PASS` must also exist on this repository even though the VPS is the same.

## Initial Telegram setup

After deployment:

1. Send `/start` from the Telegram account that should own the bot.
2. Open `⚙️ تنظیمات اتصال‌ها`.
3. Configure WooCommerce with:
   - site URL: `https://cutellashop.ir`
   - WooCommerce Consumer Key (`ck_...`)
   - WooCommerce Consumer Secret (`cs_...`)
   - WordPress username (needed for Telegram image upload)
   - WordPress Application Password (needed for Telegram image upload)
4. Configure Shopino with:
   - Cutella Shop ID
   - current browser `sessionid`

WooCommerce keys should have Read/Write permission.

## Tracking display plugin

`wordpress/orderscutella-tracking/orderscutella-tracking.php` displays the `_cutella_tracking_code` stored by the bot in:

- WooCommerce admin order screen
- customer order details
- WooCommerce emails

Install/activate that plugin on Cutella before using website tracking in production.

## Validation and deployment

- `.github/workflows/ci.yml` validates Python syntax/imports on every push.
- `.github/workflows/deploy.yml` deploys the isolated bot to the shared VPS.
- Deployment checks `http://127.0.0.1:8095/health` before configuring nginx.
