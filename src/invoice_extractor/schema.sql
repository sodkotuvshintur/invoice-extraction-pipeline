-- AP state store. SQLite here so the project runs with zero setup; the shape
-- (state on the invoice, one row per check, an outbox instead of direct ledger
-- writes, append-only audit) is what matters and ports to any RDBMS.

-- ---------- master data: what the business already knows ----------

CREATE TABLE vendors (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN ('inventory', 'expense')),
    terms_days  INTEGER NOT NULL DEFAULT 30,
    status      TEXT NOT NULL DEFAULT 'active'      -- active | blocked
);

-- One real supplier, many printed names ("Cascade Fastener Co." on the PO,
-- "CASCADE FASTENER COMPANY, INC." on the invoice).
CREATE TABLE vendor_alias (
    vendor_id   INTEGER NOT NULL REFERENCES vendors(id),
    alias       TEXT NOT NULL,                       -- normalised form
    PRIMARY KEY (alias, vendor_id)
);

CREATE TABLE purchase_orders (
    po_number   TEXT PRIMARY KEY,
    vendor_id   INTEGER NOT NULL REFERENCES vendors(id),
    order_date  TEXT NOT NULL
);

-- The three legs of the 3-way match live on one row: what was ordered (PO),
-- what arrived (receipt), and what has already been billed against it.
CREATE TABLE po_lines (
    po_number     TEXT NOT NULL REFERENCES purchase_orders(po_number),
    sku           TEXT NOT NULL,
    description   TEXT NOT NULL,
    qty_ordered   NUMERIC NOT NULL,
    unit_price    NUMERIC NOT NULL,
    qty_received  NUMERIC NOT NULL DEFAULT 0,
    qty_billed    NUMERIC NOT NULL DEFAULT 0,
    PRIMARY KEY (po_number, sku)
);

-- Posted bills for non-PO vendors: the baseline an expense invoice is judged against.
CREATE TABLE bill_history (
    id          INTEGER PRIMARY KEY,
    vendor_id   INTEGER NOT NULL REFERENCES vendors(id),
    bill_date   TEXT NOT NULL,
    amount      NUMERIC NOT NULL
);
CREATE INDEX ix_bill_history_vendor ON bill_history(vendor_id);

-- ---------- workflow state: what this system owns ----------

CREATE TABLE invoices (
    id              INTEGER PRIMARY KEY,
    source_file     TEXT NOT NULL,
    content_hash    TEXT NOT NULL,                   -- sha256 of the document bytes
    vendor_id       INTEGER REFERENCES vendors(id),
    vendor_name     TEXT,                            -- as printed
    invoice_number  TEXT,
    invoice_date    TEXT,
    due_date        TEXT,
    po_number       TEXT,
    subtotal        NUMERIC,
    tax             NUMERIC,
    total           NUMERIC,
    category        TEXT,
    engine          TEXT,                            -- rules | llm | none
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'ingested',
        -- ingested | auto_pass | flag | hold | not_applicable
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_invoices_status ON invoices(status);
CREATE INDEX ix_invoices_vendor_number ON invoices(vendor_id, invoice_number);
CREATE INDEX ix_invoices_hash ON invoices(content_hash);

CREATE TABLE invoice_lines (
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
    line_no     INTEGER NOT NULL,
    sku         TEXT,
    description TEXT,
    qty         NUMERIC,
    unit_price  NUMERIC,
    amount      NUMERIC,
    PRIMARY KEY (invoice_id, line_no)
);

-- The raw engine output is kept verbatim so any decision can be replayed.
CREATE TABLE extractions (
    id               INTEGER PRIMARY KEY,
    invoice_id       INTEGER NOT NULL REFERENCES invoices(id),
    engine           TEXT NOT NULL,
    raw_json         TEXT NOT NULL,
    field_conf_json  TEXT NOT NULL
);

-- One row per rule per invoice, pass or fail: the evidence behind a decision.
CREATE TABLE validations (
    id          INTEGER PRIMARY KEY,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
    rule        TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    expected    TEXT,
    actual      TEXT,
    detail      TEXT
);
CREATE INDEX ix_validations_invoice ON validations(invoice_id);

-- The human work queue.
CREATE TABLE exceptions (
    id          INTEGER PRIMARY KEY,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
    type        TEXT NOT NULL,     -- price_variance | qty_over_receipt | duplicate | ...
    queue       TEXT NOT NULL,     -- purchasing | receiving | accounting | ap_review | vendor_onboarding
    reason      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',        -- open | resolved | rejected
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_exceptions_state ON exceptions(state, queue);

-- Nothing writes to the ledger directly. An approved bill becomes an outbox
-- row; a separate connector drains it. The unique idempotency_ref makes a
-- retried or replayed run physically unable to post the same bill twice.
CREATE TABLE ledger_outbox (
    id               INTEGER PRIMARY KEY,
    invoice_id       INTEGER NOT NULL REFERENCES invoices(id),
    intent           TEXT NOT NULL,                  -- bill_create
    payload_json     TEXT NOT NULL,
    idempotency_ref  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'pending' -- pending | sent | acked | failed
);

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id),
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    actor        TEXT NOT NULL,                      -- component or human
    detail_json  TEXT,
    ts           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_audit_invoice ON audit_log(invoice_id);
