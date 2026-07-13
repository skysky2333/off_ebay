# FM2K Storefront

A private, no-index storefront synchronized from the `fm2k244` eBay catalog. Customers use guest PayPal checkout; the seller manages orders, refunds, tracking, store availability, and sync history in Django Admin. Pirate Ship connects to the same PayPal account.

The architecture and product boundaries are in [PRD.md](PRD.md).

## Native Conda Setup

All Python dependencies live in the standalone repository-local Conda environment.

```bash
conda env create --prefix .conda --file environment.yml
cp .env.local.example .env.local
set -a
. ./.env.local
set +a
conda run --prefix .conda python manage.py migrate
conda run --prefix .conda python manage.py createsuperuser
conda run --prefix .conda python manage.py runserver
```

Open `http://127.0.0.1:8000/`. Native development uses SQLite; the portable deployment uses PostgreSQL.

## Integration Setup

1. Create production eBay application keys and authorize the `fm2k244` seller account. Put the client ID, client secret, OAuth refresh token, and current Trading API compatibility level in the environment file used by the current setup (`.env.local` natively or `.env` for Docker). The refresh-token flow is documented by [eBay](https://developer.ebay.com/api-docs/static/oauth-refresh-token-request.html).
2. Enable eBay's out-of-stock control preference. Synchronization intentionally fails if the token belongs to another seller or that preference is disabled.
3. Create a PayPal live REST app in the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/applications/live) and put its client ID and secret in the active environment file.
4. Register `https://YOUR_DOMAIN/webhooks/paypal/` as the PayPal webhook and subscribe to `CHECKOUT.ORDER.APPROVED`, `CHECKOUT.ORDER.VOIDED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`, and the three `SHIPPING.TRACKING.*` events. Put the resulting webhook ID in `.env`.
5. Connect the same PayPal account in Pirate Ship. PayPal receives the order reference, shipping address, and line items; tracking returned through PayPal appears on the private order page. Tracking can also be entered manually in Admin.

Run a one-time verification and import:

```bash
conda run --prefix .conda python manage.py sync_ebay
```

In `/admin/`, open Store settings, set the flat shipping amount, and enable checkout only after the catalog and PayPal credentials are verified. Item `800102146771` remains excluded from checkout by default.

## Docker Deployment

```bash
cp .env.example .env
# Fill every placeholder and set the public domain/hosts/origin.
docker compose --profile tls up -d --build
docker compose exec web python manage.py createsuperuser
docker compose ps
docker compose logs -f worker
```

The optional `tls` profile runs Caddy on ports 80/443 and obtains the certificate for `STORE_DOMAIN`. Set `DJANGO_ALLOWED_HOSTS` to `STORE_DOMAIN,localhost,127.0.0.1` and `DJANGO_CSRF_TRUSTED_ORIGINS` to its `https://` origin. Without that profile, the web port is deliberately bound to `127.0.0.1:${WEB_PORT}` for use behind an existing HTTPS reverse proxy that sets `X-Forwarded-Proto`.

The web container validates production settings, applies migrations, and serves static files. The worker imports the complete eBay catalog every `EBAY_SYNC_SECONDS`, expires abandoned reservations, and fails visibly if synchronization breaks.

## Routine Operations

```bash
docker compose ps
docker compose logs --tail=200 web worker
docker compose exec web python manage.py sync_ebay
docker compose exec web python manage.py check --deploy
```

Orders and inventory operations are immutable audit records in Admin. Add shipments under Shipments. Select a paid order in Orders and use the PayPal refund action when needed.

## Backup And Move

Create a portable PostgreSQL archive:

```bash
./scripts/backup
```

Restore it into a running deployment. This stops the application during replacement and restarts it only after a successful restore:

```bash
./scripts/restore backups/seller-site-YYYYMMDD-HHMMSS.dump
```

To move hosts, transfer the repository, `.env`, and the backup archive; start the database service, run the restore, then start the full Compose project. Keep several dated archives outside the Docker host because the named volume is not a backup.

## Verification

```bash
set -a
. ./.env.local
set +a
conda run --prefix .conda python manage.py check
conda run --prefix .conda python manage.py makemigrations --check --dry-run
conda run --prefix .conda python manage.py test
conda run --prefix .conda python manage.py collectstatic --noinput
ruby -e 'require "yaml"; YAML.load_file("compose.yaml"); YAML.load_file("environment.yml")'
```

Docker is required only for container build and deployment; development, checks, and tests run entirely inside `.conda`.
