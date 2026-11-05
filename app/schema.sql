-- Idempotent schema; applied at startup under an advisory lock.

CREATE TABLE IF NOT EXISTS tickets (
    id               BIGSERIAL PRIMARY KEY,
    event_id         TEXT        NOT NULL UNIQUE,      -- dedup key for webhook deliveries
    ticket_id        TEXT        NOT NULL,
    customer_email   TEXT        NOT NULL,
    message          TEXT        NOT NULL,
    reply_url        TEXT        NOT NULL,

    -- new -> generating -> draft_ready -> approved -> sending -> sent
    --                  \-> gen_failed   \-> rejected            \-> send_failed
    status           TEXT        NOT NULL DEFAULT 'new',

    draft            TEXT,
    draft_sources    JSONB       NOT NULL DEFAULT '[]',
    llm_mode         TEXT,                             -- 'real' | 'stub'
    final_message    TEXT,                             -- what the operator approved (may be edited)

    gen_attempts     INT         NOT NULL DEFAULT 0,
    send_attempts    INT         NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_id         UUID,                             -- which worker owns the row right now
    lease_until      TIMESTAMPTZ,                      -- expired lease => row is reclaimed
    idempotency_key  UUID        NOT NULL DEFAULT gen_random_uuid(),  -- sent to helpdesk on every attempt
    helpdesk_reply_id TEXT,
    last_error       TEXT,
    received_count   INT         NOT NULL DEFAULT 1,   -- how many times the webhook delivered this event

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    draft_ready_at   TIMESTAMPTZ,
    approved_at      TIMESTAMPTZ,
    sent_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS tickets_queue_idx ON tickets (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS tickets_created_idx ON tickets (created_at DESC);

-- Sliding-window log of LLM requests: one row per request, shared by all processes.
CREATE TABLE IF NOT EXISTS llm_calls (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS llm_calls_at_idx ON llm_calls (at);

-- Storage of the helpdesk mock: replies "delivered to the customer".
CREATE TABLE IF NOT EXISTS mock_replies (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    ticket_id       TEXT        NOT NULL,
    message         TEXT        NOT NULL,
    attempts        INT         NOT NULL DEFAULT 1,  -- requests received for this key
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
