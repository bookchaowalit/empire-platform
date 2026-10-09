-- Platform schema: identity, workspace, membership, roles, policies,
-- delegation, execution, approval, and append-only audit.
-- ADR: solo-empire docs/superpowers/specs/2026-10-09-platform-foundation-identity-workflow-design.md §5

CREATE SCHEMA platform;

-- UUIDv7 (time-ordered) without requiring PostgreSQL 18's built-in uuidv7().
CREATE FUNCTION platform.uuid_v7() RETURNS uuid
LANGUAGE sql VOLATILE AS $$
    SELECT encode(
        set_bit(
            set_bit(
                overlay(uuid_send(gen_random_uuid())
                        PLACING substring(int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
                        FROM 1 FOR 6),
                52, 1),
            53, 1),
        'hex')::uuid
$$;

-- Workspace of the current request. The service sets it with SET LOCAL from a
-- verified token; unset or empty means no tenant rows are visible.
CREATE FUNCTION platform.current_workspace() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.workspace_id', true), '')::uuid
$$;

CREATE TABLE platform.identities (
    id               uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    kind             text NOT NULL CHECK (kind IN ('human', 'agent', 'service_account', 'automation')),
    display_name     text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    external_subject text UNIQUE,  -- subject from the human auth provider; never a secret
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE platform.workspaces (
    id                uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    slug              text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name              text NOT NULL,
    owner_identity_id uuid NOT NULL REFERENCES platform.identities (id),
    status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'closed')),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE platform.memberships (
    workspace_id uuid NOT NULL REFERENCES platform.workspaces (id),
    identity_id  uuid NOT NULL REFERENCES platform.identities (id),
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, identity_id)
);

-- Permission strings are <domain>.<resource>:<action>, e.g. tasks.task:write.
CREATE DOMAIN platform.permission AS text
    CHECK (VALUE ~ '^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)$');

CREATE TABLE platform.roles (
    id           uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id uuid NOT NULL REFERENCES platform.workspaces (id),
    name         text NOT NULL,
    UNIQUE (workspace_id, name),
    UNIQUE (workspace_id, id)
);

CREATE TABLE platform.role_permissions (
    workspace_id uuid NOT NULL,
    role_id      uuid NOT NULL,
    permission   platform.permission NOT NULL,
    PRIMARY KEY (role_id, permission),
    FOREIGN KEY (workspace_id, role_id) REFERENCES platform.roles (workspace_id, id)
);

CREATE TABLE platform.membership_roles (
    workspace_id uuid NOT NULL,
    identity_id  uuid NOT NULL,
    role_id      uuid NOT NULL,
    PRIMARY KEY (workspace_id, identity_id, role_id),
    FOREIGN KEY (workspace_id, identity_id) REFERENCES platform.memberships (workspace_id, identity_id),
    FOREIGN KEY (workspace_id, role_id) REFERENCES platform.roles (workspace_id, id)
);

CREATE TABLE platform.policies (
    id                uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id      uuid NOT NULL REFERENCES platform.workspaces (id),
    permission        platform.permission NOT NULL,
    requires_approval boolean NOT NULL DEFAULT false,
    limits            jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (workspace_id, permission)
);

CREATE TABLE platform.delegations (
    id                  uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id        uuid NOT NULL REFERENCES platform.workspaces (id),
    grantor_identity_id uuid NOT NULL REFERENCES platform.identities (id),
    grantee_identity_id uuid NOT NULL REFERENCES platform.identities (id),
    scopes              platform.permission[] NOT NULL CHECK (cardinality(scopes) > 0),
    expires_at          timestamptz NOT NULL,
    revoked_at          timestamptz,
    budget_tokens       bigint CHECK (budget_tokens >= 0),
    budget_thb          numeric(12, 2) CHECK (budget_thb >= 0),
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (grantor_identity_id <> grantee_identity_id),
    UNIQUE (workspace_id, id)
);

CREATE TABLE platform.approvals (
    id                   uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id         uuid NOT NULL REFERENCES platform.workspaces (id),
    requested_action     platform.permission NOT NULL,
    approver_identity_id uuid REFERENCES platform.identities (id),
    decision             text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'approved', 'rejected')),
    reason               text,
    decided_at           timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    CHECK ((decision = 'pending') = (decided_at IS NULL))
);

CREATE TABLE platform.executions (
    id              uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id    uuid NOT NULL REFERENCES platform.workspaces (id),
    initiated_by    uuid NOT NULL REFERENCES platform.identities (id),
    executed_by     uuid NOT NULL REFERENCES platform.identities (id),
    on_behalf_of    uuid NOT NULL REFERENCES platform.identities (id),
    delegation_id   uuid,
    approval_id     uuid,
    channel         text NOT NULL CHECK (channel IN ('api', 'mcp', 'workflow', 'cli', 'event')),
    idempotency_key text,
    status          text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'awaiting_approval')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (workspace_id, delegation_id) REFERENCES platform.delegations (workspace_id, id),
    FOREIGN KEY (workspace_id, approval_id) REFERENCES platform.approvals (workspace_id, id)
);

CREATE TABLE platform.audit_events (
    id            uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id  uuid NOT NULL REFERENCES platform.workspaces (id),
    execution_id  uuid NOT NULL REFERENCES platform.executions (id),
    service       text NOT NULL,
    action        text NOT NULL,
    resource_type text NOT NULL,
    resource_id   text NOT NULL,
    before_hash   text,
    after_hash    text,
    occurred_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON platform.memberships (identity_id);
CREATE INDEX ON platform.delegations (grantee_identity_id) WHERE revoked_at IS NULL;
CREATE INDEX ON platform.executions (workspace_id, started_at);
CREATE INDEX ON platform.audit_events (workspace_id, occurred_at);
