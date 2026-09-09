# Changelog

## 0.3.0

- Operator-owned media folders, bounded files/albums/voice notes and exclusive bounded downloads to a separate folder; existing account, write policy and durable outbox retained.
- Explicit numeric destination checks for media and polling, with no caller-controlled roots.
- Poll selected chats after acknowledged message IDs; first poll proposes a current baseline. Durable actor/account-bound batches, atomic acknowledgment and safe replay across restart.
- Installable setup wizard and JSON/TOML client snippets that work outside the checkout.
- English and Russian agency information, integration contact and package installation guide.

## 0.2.0

- Explicit config-file startup and offline configuration doctor.
- Install/client guide, local setup wizard and clean-wheel validation.
- Recipient search marks incomplete results as ambiguous. Messages remain untrusted content. Media and event-feed expansion are future work.
- LicenseRef-ZAI-ONE and Issues-based feedback policy.
