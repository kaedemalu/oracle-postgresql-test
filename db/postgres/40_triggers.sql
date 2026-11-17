CREATE FUNCTION app.trg_orders_audit_fn() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO app.audit_log (id, order_id, action, old_status, new_status, logged_at)
  VALUES (nextval('app.audit_seq'), NEW.id,
          CASE WHEN TG_OP = 'INSERT' THEN 'INSERT' ELSE 'UPDATE' END,
          CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END,
          NEW.status, app_clock.now());
  RETURN NULL;
END;
$$;

CREATE TRIGGER trg_orders_audit
AFTER INSERT OR UPDATE OF status ON app.orders
FOR EACH ROW EXECUTE FUNCTION app.trg_orders_audit_fn();
