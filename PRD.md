# FM2K Storefront Product Requirements

Status: Implemented

## Product Summary

FM2K Storefront is a low-maintenance direct sales website for the seller behind the eBay account `fm2k244`. It presents the seller's active fixed-price inventory in a polished independent storefront while keeping eBay as the catalog and inventory authority.

The product is intended for a small, known customer group. Customers can browse current items, complete a guest purchase through PayPal, and follow the resulting order and shipment without creating an account. The seller manages exceptions and fulfillment from a compact private administration area.

## Goals

- Keep products, availability, prices, descriptions, condition, and images synchronized with active eBay listings.
- Verify and reduce eBay inventory before payment capture, minimizing cross-channel sale conflicts and surfacing every mismatch.
- Make direct ordering simple for customers and require little routine seller administration.
- Use PayPal for payment authorization, capture, refunds, and payment records.
- Use Pirate Ship's PayPal connection to import paid orders and return shipment tracking.
- Provide a calm, premium, product-first experience on mobile and desktop.
- Package the complete application and persistent database for portable Docker deployment.

## Non-Goals

- Replacing eBay as the seller's listing management tool.
- Supporting auctions, offers, bidding, or eBay messaging.
- Advertising the direct store through eBay listings, messages, or eBay-derived customer contact data.
- Customer accounts, loyalty programs, reviews, wish lists, or other marketplace features.
- Stripe or other payment processors.
- A custom Pirate Ship integration.

## Users

### Customer

A known or referred buyer who wants to inspect available products, purchase without creating an account, and retrieve order and tracking status securely.

### Seller

One administrator who wants a clear view of inventory sync health, paid orders, fulfillment state, and exceptions without maintaining duplicate listings.

## Customer Experience

### Catalog

- Show only active, purchasable fixed-price eBay listings.
- Support search, category filtering, and clear availability.
- Preserve the source listing's title, price, condition, stock, primary details, and imagery.
- Remove or mark unavailable items promptly when an eBay listing ends or runs out of stock.

### Product Detail

- Prioritize large, inspectable product photography.
- Present price, condition, available quantity, item specifics, description, and shipping information clearly.
- Sanitize imported listing content before display.
- Provide an obvious purchase action without marketing clutter.

### Cart And Checkout

- Allow guest checkout without a customer account.
- Support multiple products in one order.
- Accept shipping addresses in the United States only.
- Apply one seller-configured flat shipping amount to each order.
- Revalidate and reserve inventory before creating a PayPal order.
- Build the authoritative payment amount on the server from synchronized product data.
- Show item subtotal, shipping, and final total before PayPal approval.
- Confirm an order only from a server-verified PayPal capture.
- Release inventory when a payment attempt expires or is cancelled.

### Order Status

- Issue a short human-readable order reference and a separate unguessable status link.
- Show payment, fulfillment, and shipment status without exposing customer information publicly.
- Display carrier and tracking information when available.

## Seller Experience

### Dashboard

- Show orders requiring action, recent orders, catalog sync health, and integration errors.
- Make failures visible and actionable rather than silently serving stale state.
- Support manual catalog synchronization.

### Orders

- Show customer, payment, item, shipping address, and fulfillment information in one view.
- Support explicit order states from awaiting payment through paid, shipped, cancelled, expired, and refunded.
- Allow manual tracking entry when an external update is unavailable.
- Retain an event history for payment, inventory, and fulfillment changes.

### Catalog

- Show synchronized products and their eBay source status.
- Keep merchandising fields read-only locally so eBay remains the single editing surface.
- Highlight inventory mismatches and failed write-backs.

## Integration Requirements

### eBay

- Authenticate the seller account using eBay's supported seller authorization flow.
- Import all supported active fixed-price listings and their complete customer-facing content.
- Synchronize automatically on a regular schedule and on administrator request.
- Treat missing, ended, or zero-quantity listings as unavailable.
- Reserve or reduce eBay inventory during direct checkout so cross-channel sales cannot oversell.
- Restore inventory for expired or cancelled reservations when appropriate.
- Reconcile local and eBay inventory regularly and surface every mismatch.

### PayPal

- Use PayPal Checkout with server-created and server-captured orders.
- Verify asynchronous PayPal notifications before changing payment state.
- Include the local order reference and complete line-item and shipping information in PayPal.
- Store PayPal order and capture references for reconciliation and refunds.
- Never treat a browser redirect or customer-supplied payment reference as proof of payment.

### Pirate Ship

- Rely on Pirate Ship's supported PayPal account connection rather than a custom API.
- Structure PayPal orders so Pirate Ship receives usable recipient and line-item data.
- Accept tracking returned through PayPal when the supported event flow provides it.
- Preserve manual tracking entry as the explicit fulfillment boundary when needed.

## High-Level Architecture

The deployable system consists of:

- A customer storefront and private seller administration interface.
- An application service responsible for catalog, cart, orders, payments, and secure status access.
- A synchronization service responsible for scheduled eBay imports, inventory write-backs, reconciliation, and payment event processing.
- A PostgreSQL database holding synchronized catalog state, reservations, orders, integration references, and event history.
- Docker services with persistent database storage, health checks, migrations, and environment-managed secrets.
- One reproducible Conda environment for application runtime, development, and tests.

The application is a single deployable product with modular domain boundaries. Additional infrastructure services are introduced only when required for correctness.

## Design Direction

- Product-first storefront using the seller's real listing photography in the first viewport.
- Neutral white and soft-gray surfaces, near-black text, restrained functional accents, and crisp separators.
- Modest corner radii, stable image frames, and minimal motion.
- No decorative gradients, promotional clutter, oversized marketing hero, or nested card layouts.
- Dense and operational seller views; quiet and linear customer checkout.
- Responsive from small mobile screens through wide desktop layouts.
- WCAG 2.2 AA semantics, contrast, keyboard access, visible focus, touch targets, and reduced-motion behavior.

## Security And Operations

- Require authenticated access to all seller functions.
- Keep integration credentials out of source control and persisted customer data.
- Verify PayPal notifications and protect state-changing requests.
- Use unguessable customer order-status tokens distinct from short order references.
- Sanitize all imported listing markup.
- Provide database backup and restore procedures suitable for moving the deployment.
- Expose application and synchronization health without exposing secrets or customer data.
- Fail visibly when required integrations or configuration are invalid.

## Success Criteria

- A newly active supported eBay listing appears without local product entry.
- An edited listing is reflected after synchronization.
- An ended or depleted listing cannot be purchased.
- A website checkout does not capture payment until fresh eBay availability is verified and the intended eBay inventory reduction is confirmed.
- A verified PayPal payment creates one paid order even when events are delivered repeatedly.
- A paid PayPal order is available to Pirate Ship through its supported PayPal connection.
- Tracking can reach the customer status page either through the supported PayPal flow or explicit seller entry.
- The complete deployment starts from documented Docker commands on a new host and restores from a database backup.
- Core customer and seller workflows are usable on mobile and desktop with no overlapping or clipped interface elements.

## Confirmed Decisions

- The eBay account is `fm2k244`.
- eBay remains the catalog management and inventory authority.
- The direct store supports active fixed-price listings.
- Checkout uses PayPal only.
- Pirate Ship is connected through PayPal rather than a custom Pirate Ship API.
- Customers check out as guests.
- The storefront is unlisted and excluded from search indexing but does not require customer accounts or an access code.
- The store accepts United States shipping addresses and uses one seller-configured flat shipping amount per order.
- The cart can combine multiple products in one order.
- Direct purchases follow the source store's no-returns policy.
- The application and database are distributed as Docker services.
- Application dependencies are isolated in the standalone `seller-site` Conda environment on both the host and in the container image.

## Open Decisions

- Final store display name and customer-facing contact identity.
- Customer email sender and production domain.
