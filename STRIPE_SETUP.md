# Edge Alert — Stripe Billing Setup

## What needs to happen (CEO actions)

All of this happens in the Stripe Dashboard. No code required.

---

## Step 1: Create a Stripe account

Go to https://stripe.com and sign up. Use your business email.

After signup:
- Complete identity verification
- Add a bank account to receive payouts

---

## Step 2: Create two Payment Links

Stripe Payment Links let you accept recurring subscriptions without writing a server.

### Basic Plan ($39/month)

1. In Stripe Dashboard → **Payment Links** → **+ New**
2. Set product: "Edge Alert Basic"
3. Pricing: $39.00 / month (recurring)
4. Enable: "Let customers manage their subscriptions"
5. Create the link — copy the URL (looks like `https://buy.stripe.com/xxxxx`)

### Pro Plan ($79/month)

Same flow, set product "Edge Alert Pro", price $79.00/month.

---

## Step 3: Update the landing page

Open `EdgeAlert/landing/index.html` and find these two lines:

```
<a href="STRIPE_BASIC_PAYMENT_LINK" ...>
<a href="STRIPE_PRO_PAYMENT_LINK" ...>
```

Replace each placeholder with your actual Stripe Payment Link URLs.

Also update the nav CTA href if you want it to link directly to the Basic plan checkout.

---

## Step 4: Host the landing page (GitHub Pages)

1. Create a new repo at github.com — name it `edge-alert` or `northstaraitools`
2. Push the contents of `EdgeAlert/landing/` to the repo root
3. Also copy `EdgeAlert/accuracy_dashboard.html` to the repo root
4. Create a `data/` folder in the repo — the accuracy tracker exports `accuracy_report.json` here
5. In repo Settings → Pages → Source: Deploy from branch (main, / root)
6. Your page will be live at: `https://yourusername.github.io/edge-alert/`

To serve live accuracy data on GitHub Pages:
- Run `python3 accuracy_tracker.py --full` locally on your Mac
- Commit and push `data/accuracy_report.json` to the repo
- Or set up a GitHub Action to auto-pull and publish the JSON on a schedule

---

## Step 5: Point your domain (optional)

If you have a custom domain (e.g., edgealert.io), add a CNAME in your DNS
pointing to `yourusername.github.io`. Then configure it in repo Settings → Pages → Custom domain.

---

## Revenue flow

Subscriber pays via Stripe Payment Link → Stripe takes ~2.9% + 30¢ per charge → remainder hits your bank account on the payout schedule (default 2 business days rolling).

You'll manage subscribers in the Stripe Dashboard → Customers.

---

## Compliance note

The landing page includes the required disclaimer:
> "Edge Alert is an informational signal service. Not investment advice."

Do not remove this. It applies to both the landing page and any marketing content.
