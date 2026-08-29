# OrdersCutella

Telegram operations bot for Cutella Shop (`cutellashop.ir`), adapted from OrdersVesta.

Target features:
- WooCommerce product creation and management from Telegram
- Shopino order integration
- Postal tracking Excel import and matching
- Separate persistent state and deployment on the same VPS as OrdersVesta

Deployment is intentionally isolated from OrdersVesta (separate app directory, container, port and data volume).
