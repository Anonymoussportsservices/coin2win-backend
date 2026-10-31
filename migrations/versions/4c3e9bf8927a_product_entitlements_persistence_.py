"""Product entitlements persistence foundation.

Revision ID: 4c3e9bf8927a
Revises: 8952b95bbb5e
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '4c3e9bf8927a'
down_revision: Union[str, Sequence[str], None] = '8952b95bbb5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ELIGIBLE = "'superadmin', 'super_admin', 'admin', 'master_agent', 'agent', 'sub_agent'"


def upgrade() -> None:
    op.create_table(
        'platform_products',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('product_key', sa.String(64), nullable=False),
        sa.Column('display_name', sa.String(128), nullable=False),
        sa.Column('status', sa.String(16), server_default='inactive', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('product_key', name='uq_platform_products_key'),
        sa.CheckConstraint("product_key ~ '^[a-z][a-z0-9_]*$'", name='ck_platform_products_key_format'),
        sa.CheckConstraint("status IN ('active','inactive')", name='ck_platform_products_status'),
    )
    op.create_table(
        'hierarchy_product_entitlements',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('hierarchy_id', sa.String(64), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('product_id', sa.BigInteger(), sa.ForeignKey('platform_products.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('delegation_allowed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('granted_by', sa.String(64), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('hierarchy_id', 'product_id', name='uq_hpe_hierarchy_product'),
        sa.CheckConstraint('NOT delegation_allowed OR enabled', name='ck_hpe_delegation_requires_enabled'),
    )
    op.create_index('ix_hpe_product_id', 'hierarchy_product_entitlements', ['product_id'])
    op.create_index('ix_hpe_granted_by', 'hierarchy_product_entitlements', ['granted_by'])
    op.create_table(
        'product_entitlement_audit',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('entitlement_id', sa.BigInteger(), nullable=True),
        sa.Column('hierarchy_id', sa.String(64), nullable=False),
        sa.Column('product_id', sa.BigInteger(), sa.ForeignKey('platform_products.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('action', sa.String(16), nullable=False),
        sa.Column('previous_enabled', sa.Boolean(), nullable=True),
        sa.Column('new_enabled', sa.Boolean(), nullable=True),
        sa.Column('previous_delegation_allowed', sa.Boolean(), nullable=True),
        sa.Column('new_delegation_allowed', sa.Boolean(), nullable=True),
        sa.Column('actor_id', sa.String(64), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("action IN ('grant','update','revoke')", name='ck_pea_action'),
    )
    op.create_index('ix_pea_hierarchy_created', 'product_entitlement_audit', ['hierarchy_id', 'created_at'])
    op.create_index('ix_pea_product_created', 'product_entitlement_audit', ['product_id', 'created_at'])
    op.create_index('ix_pea_actor_created', 'product_entitlement_audit', ['actor_id', 'created_at'])

    # Serialize entitlement writes with role changes on the same users row.
    # Actor context must be supplied by a future authenticated application writer.
    # This migration creates no writer, product or initial grant.
    op.execute(f"""
    CREATE FUNCTION public.c03_check_entitlement_role() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public AS $$
    DECLARE v_commercial_role varchar(32);
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id OR
           NEW.granted_by IS DISTINCT FROM OLD.granted_by OR
           NEW.created_at IS DISTINCT FROM OLD.created_at THEN
          RAISE EXCEPTION 'entitlement identity, original grantor and creation time are immutable' USING ERRCODE = '23514';
        END IF;
      END IF;
      SELECT role INTO v_commercial_role FROM public.users WHERE id = NEW.hierarchy_id FOR UPDATE;
      IF NOT FOUND OR v_commercial_role NOT IN ({ELIGIBLE}) THEN
        RAISE EXCEPTION 'hierarchy is not eligible for product entitlements' USING ERRCODE = '23514';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER c03_entitlement_role_guard
    BEFORE INSERT OR UPDATE ON public.hierarchy_product_entitlements
    FOR EACH ROW EXECUTE FUNCTION public.c03_check_entitlement_role();
    """)
    op.execute(f"""
    CREATE FUNCTION public.c03_check_user_role_transition() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public AS $$
    BEGIN
      IF NEW.role NOT IN ({ELIGIBLE}) AND EXISTS (
        SELECT 1 FROM public.hierarchy_product_entitlements WHERE hierarchy_id = NEW.id
      ) THEN
        RAISE EXCEPTION 'cannot transition role while product entitlements exist' USING ERRCODE = '23514';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER c03_user_role_guard
    BEFORE UPDATE OF role ON public.users
    FOR EACH ROW WHEN (OLD.role IS DISTINCT FROM NEW.role)
    EXECUTE FUNCTION public.c03_check_user_role_transition();
    """)

    # Historical audit IDs intentionally have no FK: deletes must not rewrite history.
    # This trigger function is created by the privileged migration role (postgres).
    # The application cannot directly write to the audit table.
    op.execute("""
    CREATE FUNCTION public.c03_write_entitlement_audit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    DECLARE
      audit_actor varchar(64);
      audit_reason text;
    BEGIN
      audit_actor := NULLIF(current_setting('coin2win.entitlement_actor', true), '');
      audit_reason := NULLIF(current_setting('coin2win.entitlement_reason', true), '');
      IF audit_actor IS NULL OR length(audit_actor) > 64 THEN
        RAISE EXCEPTION 'entitlement write requires actor context (max 64 chars)' USING ERRCODE = '23514';
      END IF;
      -- Session context is supplied by the future authorized writer; it is not
      -- cryptographic proof of identity and must be set with SET LOCAL.
      IF TG_OP = 'DELETE' THEN
        INSERT INTO public.product_entitlement_audit
          (entitlement_id, hierarchy_id, product_id, action,
           previous_enabled, new_enabled, previous_delegation_allowed,
           new_delegation_allowed, actor_id, reason)
        VALUES (OLD.id, OLD.hierarchy_id, OLD.product_id, 'revoke',
                OLD.enabled, NULL, OLD.delegation_allowed, NULL,
                audit_actor, audit_reason);
        RETURN OLD;
      END IF;
      IF TG_OP = 'INSERT' AND audit_actor IS DISTINCT FROM NEW.granted_by THEN
        RAISE EXCEPTION 'grantor must match audit actor' USING ERRCODE = '23514';
      END IF;
      IF TG_OP = 'UPDATE' AND
         (OLD.hierarchy_id, OLD.product_id, OLD.enabled, OLD.delegation_allowed,
          OLD.granted_by) IS NOT DISTINCT FROM
         (NEW.hierarchy_id, NEW.product_id, NEW.enabled, NEW.delegation_allowed,
          NEW.granted_by) THEN
        RETURN NEW;
      END IF;
      INSERT INTO public.product_entitlement_audit
        (entitlement_id, hierarchy_id, product_id, action,
         previous_enabled, new_enabled, previous_delegation_allowed,
         new_delegation_allowed, actor_id, reason)
      VALUES (NEW.id, NEW.hierarchy_id, NEW.product_id,
              CASE WHEN TG_OP = 'INSERT' THEN 'grant' ELSE 'update' END,
              CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.enabled END,
              NEW.enabled,
              CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.delegation_allowed END,
              NEW.delegation_allowed, audit_actor, audit_reason);
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER c03_entitlement_audit_writer
    AFTER INSERT OR UPDATE OR DELETE ON public.hierarchy_product_entitlements
    FOR EACH ROW EXECUTE FUNCTION public.c03_write_entitlement_audit();
    """)

    # PostgreSQL grants EXECUTE on functions to PUBLIC by default.
    # Trigger execution does not require the invoking user to hold EXECUTE.
    op.execute("REVOKE ALL ON FUNCTION public.c03_check_entitlement_role() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.c03_check_user_role_transition() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.c03_write_entitlement_audit() FROM PUBLIC")
    # Audit rows are written by a privileged, trigger-owned function, not arbitrary clients.
    op.execute("""
    CREATE FUNCTION public.c03_reject_audit_mutation() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public AS $$
    BEGIN
      RAISE EXCEPTION 'product entitlement audit is append-only' USING ERRCODE = '55000';
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER c03_audit_no_update_delete
    BEFORE UPDATE OR DELETE ON public.product_entitlement_audit
    FOR EACH ROW EXECUTE FUNCTION public.c03_reject_audit_mutation();
    """)
    op.execute("""
    CREATE TRIGGER c03_audit_no_truncate
    BEFORE TRUNCATE ON public.product_entitlement_audit
    FOR EACH STATEMENT EXECUTE FUNCTION public.c03_reject_audit_mutation();
    """)
    op.execute("REVOKE ALL ON FUNCTION public.c03_reject_audit_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.product_entitlement_audit FROM PUBLIC")
    op.execute("REVOKE ALL ON public.product_entitlement_audit FROM coin2win")
    op.execute("REVOKE ALL ON SEQUENCE public.product_entitlement_audit_id_seq FROM PUBLIC")
    op.execute("REVOKE ALL ON SEQUENCE public.product_entitlement_audit_id_seq FROM coin2win")
    # The application must not call privileged functions directly.
    op.execute("REVOKE ALL ON FUNCTION public.c03_write_entitlement_audit() FROM coin2win")
    # Runtime may read product configuration and entitlement state, not write them.
    # WP-C03 security boundary: no runtime entitlement/catalog writer.
    # Product status is part of effective entitlement configuration.
    op.execute("REVOKE ALL ON public.platform_products FROM coin2win")
    op.execute("REVOKE ALL ON public.hierarchy_product_entitlements FROM coin2win")
    op.execute("GRANT SELECT ON public.platform_products TO coin2win")
    op.execute("GRANT SELECT ON public.hierarchy_product_entitlements TO coin2win")
    op.execute("REVOKE ALL ON SEQUENCE public.platform_products_id_seq FROM coin2win")
    op.execute("REVOKE ALL ON SEQUENCE public.hierarchy_product_entitlements_id_seq FROM coin2win")
    op.execute("GRANT SELECT ON public.product_entitlement_audit TO coin2win")
    # Explicitly remove PUBLIC access to newly created tables and sequences.
    op.execute("REVOKE ALL ON public.platform_products, public.hierarchy_product_entitlements FROM PUBLIC")
    op.execute("REVOKE ALL ON SEQUENCE public.platform_products_id_seq, public.hierarchy_product_entitlements_id_seq FROM PUBLIC")

    # public.users was owned by the runtime role before C03.
    # Keep the operational DML contract while protecting its role guard trigger.
    # Execute as the approved privileged migration role (postgres).
    op.execute("ALTER TABLE public.users OWNER TO postgres")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.users TO coin2win")


def downgrade() -> None:
    # Destructive downgrade: execute only on explicitly approved disposable environments.
    op.execute('DROP TRIGGER IF EXISTS c03_entitlement_audit_writer ON public.hierarchy_product_entitlements')
    op.execute('DROP FUNCTION IF EXISTS public.c03_write_entitlement_audit()')
    op.execute('DROP TRIGGER IF EXISTS c03_user_role_guard ON public.users')
    op.execute('DROP FUNCTION IF EXISTS public.c03_check_user_role_transition()')
    # Restore pre-C03 ownership only in an explicitly approved disposable DB.
    op.execute("ALTER TABLE public.users OWNER TO coin2win")
    op.execute('DROP TRIGGER IF EXISTS c03_entitlement_role_guard ON public.hierarchy_product_entitlements')
    op.execute('DROP FUNCTION IF EXISTS public.c03_check_entitlement_role()')
    op.execute('DROP TRIGGER IF EXISTS c03_audit_no_truncate ON public.product_entitlement_audit')
    op.execute('DROP TRIGGER IF EXISTS c03_audit_no_update_delete ON public.product_entitlement_audit')
    op.execute('DROP FUNCTION IF EXISTS public.c03_reject_audit_mutation()')
    op.drop_index('ix_pea_actor_created', table_name='product_entitlement_audit')
    op.drop_index('ix_pea_product_created', table_name='product_entitlement_audit')
    op.drop_index('ix_pea_hierarchy_created', table_name='product_entitlement_audit')
    op.drop_table('product_entitlement_audit')
    op.drop_index('ix_hpe_granted_by', table_name='hierarchy_product_entitlements')
    op.drop_index('ix_hpe_product_id', table_name='hierarchy_product_entitlements')
    op.drop_table('hierarchy_product_entitlements')
    op.drop_table('platform_products')
